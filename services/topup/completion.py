"""Top-up application orchestration and atomic ledger completion (T014;
FR-003-FR-011, FR-016).

Scope, matching the ownership split already established by T012/T013's
handoffs: this module is the single place a caller (T015's webhook
processor, or any other verified-event source) reaches to turn a *verified*
provider outcome into a state change. It does not verify webhook
authenticity or provider-event replay itself (FR-007/FR-008 receiving side
-- provider_events uniqueness -- is T015's responsibility); it assumes the
caller has already established the event is authentic and not a duplicate
at the transport layer, and focuses on what must happen atomically once
that is true:

- complete_verified_topup(): the CRITICAL path (FR-009, FR-010, FR-011).
  Locks the top-up row, and only for a verified amount/currency match posts
  one balanced, immutable ledger transaction (services/topup/ledger.py),
  updates the top-up's state via the pinned state machine
  (services/topup/state_machine.py), and credits the wallet balance
  projection -- all inside one database transaction/commit (Constitution
  III). A provider-reported amount/currency mismatch never reaches the
  ledger at all; it routes to UnderReview instead (FR-009's explicit
  amendment, and the state machine only allows Processing -> UnderReview,
  never a ledger-posting transition). Duplicate/late events against an
  already-terminal top-up are a no-op (FR-008, SC-001), consistent with the
  spec's "Concurrent completion/cancellation" edge case: whichever event
  observes the row lock in a non-terminal state wins.

- apply_verified_failure(): the non-ledger sibling for a verified provider
  failure. No money ever moved for a Failed top-up (SC-003), so this never
  touches services/topup/ledger.py or the wallet at all -- only the state
  transition and an audit event.

Ledger account codes reuse the exact names T004/T007's tests and
services/topup/reversal.py already pinned ("customer_wallet",
"provider_clearing", "fee_income") rather than inventing new ones --
reversal.py's reverse_topup() looks up the wallet entry by
`e.account == "customer_wallet"` specifically, so drifting from that name
here would silently break T019's reversal path.

Notification queuing (the FR-016 "outbox" half of this task) is
deliberately minimal: one in-app Notification row (per T002's spec.md
assumption that the first implementation uses only the existing in-app
Notification model), written and committed strictly *after* the financial
commit has already succeeded, in its own try/except so a notification
failure can never roll back or retry the money movement. Richer delivery
guarantees, retries, and channels beyond this are T024's explicit scope
("Implement post-commit notification events and safe failure handling.
FR-016") -- not duplicated here.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select

from models.notification import Notification
from models.topup import LedgerEntryRecord, LedgerTransactionRecord, TopUp
from models.wallet import Wallet
from services.topup.ledger import LedgerEntry, post_ledger_transaction
from services.topup.money import Money
from services.topup.state_machine import transition
from services.wallet_policy import credit as credit_wallet
from utils.audit import log_audit

logger = logging.getLogger(__name__)

CUSTOMER_WALLET_ACCOUNT = "customer_wallet"
PROVIDER_CLEARING_ACCOUNT = "provider_clearing"
FEE_INCOME_ACCOUNT = "fee_income"

TERMINAL_STATUSES = {"Completed", "Failed", "Expired", "Cancelled", "Reversed", "UnderReview"}
COMPLETABLE_FROM = {"Processing"}
FAILABLE_FROM = {"Processing", "RequiresAction"}


class TopUpNotFoundError(LookupError):
    pass


class WalletNotFoundError(LookupError):
    pass


class InvalidCompletionStateError(ValueError):
    pass


@dataclass(frozen=True)
class CompletionOutcome:
    top_up: TopUp
    action: str  # "completed" | "under_review" | "failed" | "no_op"
    ledger_transaction: Optional[LedgerTransactionRecord] = None


def _ledger_entries_for_completion(top_up: TopUp) -> Tuple[LedgerEntry, ...]:
    gross = Money(top_up.gross_amount, top_up.currency)
    fee = Money(top_up.fee_amount, top_up.currency)
    net = Money(top_up.net_amount, top_up.currency)
    entries = [
        LedgerEntry(account=PROVIDER_CLEARING_ACCOUNT, direction="debit", amount=gross),
        LedgerEntry(account=CUSTOMER_WALLET_ACCOUNT, direction="credit", amount=net),
    ]
    if fee.amount > 0:
        entries.append(LedgerEntry(account=FEE_INCOME_ACCOUNT, direction="credit", amount=fee))
    return tuple(entries)


async def _queue_completion_notification(db, top_up: TopUp, wallet: Wallet) -> None:
    try:
        db.add(
            Notification(
                id=uuid.uuid4(),
                user_id=wallet.user_id,
                type="topup_completed",
                title="Top-up completed",
                message=f"Your top-up of {top_up.net_amount} {top_up.currency} has been credited.",
                data={
                    "top_up_reference": top_up.internal_reference,
                    "amount": str(top_up.net_amount),
                    "currency": top_up.currency,
                    "status": top_up.status,
                },
            )
        )
        await db.commit()
    except Exception:
        logger.exception(
            "Failed to queue top-up completion notification for top-up %s; "
            "money movement already committed and is not affected",
            top_up.id,
        )
        try:
            await db.rollback()
        except Exception:
            logger.exception(
                "Failed to roll back the failed notification transaction for top-up %s",
                top_up.id,
            )


async def complete_verified_topup(
    db,
    top_up_id: uuid.UUID,
    reported_amount: Money,
    *,
    provider_transaction_reference: Optional[str] = None,
) -> CompletionOutcome:
    top_up = await db.scalar(select(TopUp).where(TopUp.id == top_up_id).with_for_update())
    if top_up is None:
        raise TopUpNotFoundError(f"no top-up with id {top_up_id}")

    if top_up.status in TERMINAL_STATUSES:
        if (
            top_up.status == "Completed"
            and (top_up.currency != reported_amount.currency or top_up.gross_amount != reported_amount.amount)
        ):
            try:
                await log_audit(
                    db,
                    action="topup_duplicate_event_amount_mismatch",
                    resource_type="top_up",
                    resource_id=str(top_up.id),
                    details={
                        "completed_amount": str(top_up.gross_amount),
                        "completed_currency": top_up.currency,
                        "reported_amount": str(reported_amount.amount),
                        "reported_currency": reported_amount.currency,
                    },
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return CompletionOutcome(top_up=top_up, action="no_op")

    if top_up.status not in COMPLETABLE_FROM:
        raise InvalidCompletionStateError(
            f"cannot complete verified top-up from state {top_up.status!r}; expected 'Processing'"
        )

    now = datetime.now(timezone.utc)
    gross = Money(top_up.gross_amount, top_up.currency)

    if reported_amount.amount != gross.amount or reported_amount.currency != gross.currency:
        try:
            transition(top_up.status, "UnderReview")
            top_up.status = "UnderReview"
            top_up.updated_at = now
            await log_audit(
                db,
                action="topup_amount_mismatch",
                resource_type="top_up",
                resource_id=str(top_up.id),
                details={
                    "expected_amount": str(gross.amount),
                    "expected_currency": gross.currency,
                    "reported_amount": str(reported_amount.amount),
                    "reported_currency": reported_amount.currency,
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return CompletionOutcome(top_up=top_up, action="under_review")

    wallet = None
    ledger_record = None
    try:
        # Lock and validate the wallet BEFORE constructing/flushing any ledger
        # record. Nothing upstream re-checks that top_up.currency still
        # matches the wallet it is about to credit -- the provider-amount
        # match above only compares against the top-up's OWN stored
        # currency, which is not the same guarantee. Without this check a
        # top-up whose currency had drifted from its wallet's (e.g. after a
        # data-integrity issue elsewhere) would post ledger entries in one
        # currency while crediting a wallet balance denominated in another.
        wallet = await db.scalar(select(Wallet).where(Wallet.id == top_up.wallet_id).with_for_update())
        if wallet is None:
            raise WalletNotFoundError(f"no wallet with id {top_up.wallet_id} for top-up {top_up.id}")

        if wallet.currency != top_up.currency:
            transition(top_up.status, "UnderReview")
            top_up.status = "UnderReview"
            top_up.updated_at = now
            await log_audit(
                db,
                action="topup_wallet_currency_mismatch",
                resource_type="top_up",
                resource_id=str(top_up.id),
                details={
                    "top_up_currency": top_up.currency,
                    "wallet_currency": wallet.currency,
                    "wallet_id": str(wallet.id),
                },
            )
            await db.commit()
            return CompletionOutcome(top_up=top_up, action="under_review")

        entries = _ledger_entries_for_completion(top_up)
        ledger_txn = post_ledger_transaction(
            source_reference=f"ledger_{top_up.internal_reference}", entries=entries
        )

        # Insert the parent transaction UNPOSTED, flushed on its own, before
        # its entries are ever added. Migration 0018's
        # trg_guard_ledger_entry_mutation is a BEFORE INSERT trigger on
        # ledger_entries that rejects any row whose parent is already posted --
        # constructing the parent with is_posted=True from the start would make
        # every real completion fail at the very first ledger_entries insert.
        #
        # The parent's own flush() is deliberately separate from the entries'
        # flush() below, not merged into one call. There is no ORM
        # relationship() between LedgerTransactionRecord and LedgerEntryRecord
        # (only a plain FK column) -- against real PostgreSQL, adding both the
        # parent and its children in one batch and flushing once does not
        # reliably guarantee the parent's INSERT is sent before the children's
        # batched multi-row INSERT, and was independently reproduced to raise
        # a real ForeignKeyViolationError ("Key (ledger_transaction_id)=(...)
        # is not present in table ledger_transactions") when run against a
        # real database -- every prior test of this path used a FakeDB that
        # can't detect ordering bugs like this at all. Flushing the parent by
        # itself first removes any dependency on SQLAlchemy's flush-ordering
        # heuristics for unrelated mappers.
        ledger_record = LedgerTransactionRecord(
            id=uuid.uuid4(),
            source_reference=ledger_txn.source_reference,
            top_up_id=top_up.id,
            currency=gross.currency,
            is_posted=False,
            posted_at=None,
        )
        db.add(ledger_record)
        await db.flush()

        for entry in ledger_txn.entries:
            db.add(
                LedgerEntryRecord(
                    id=uuid.uuid4(),
                    ledger_transaction_id=ledger_record.id,
                    account_code=entry.account,
                    direction=entry.direction,
                    amount=entry.amount.amount,
                )
            )
        await db.flush()

        # Only now flip the parent to posted -- an UPDATE from is_posted=False
        # is the one legitimate transition trg_guard_ledger_transaction_mutation
        # allows, and by this point every entry already exists underneath it.
        ledger_record.is_posted = True
        ledger_record.posted_at = now

        transition(top_up.status, "Completed")
        top_up.status = "Completed"
        top_up.completed_at = now
        top_up.updated_at = now
        if provider_transaction_reference:
            top_up.provider_transaction_reference = provider_transaction_reference

        net = Money(top_up.net_amount, top_up.currency)
        credit_wallet(wallet, net.amount)

        await log_audit(
            db,
            action="topup_completed",
            resource_type="top_up",
            resource_id=str(top_up.id),
            details={
                "gross_amount": str(gross.amount),
                "fee_amount": str(top_up.fee_amount),
                "net_amount": str(net.amount),
                "currency": top_up.currency,
                "ledger_source_reference": ledger_record.source_reference,
            },
        )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await _queue_completion_notification(db, top_up, wallet)

    return CompletionOutcome(top_up=top_up, action="completed", ledger_transaction=ledger_record)


async def apply_verified_failure(
    db,
    top_up_id: uuid.UUID,
    *,
    failure_code: str,
    failure_message: str,
) -> CompletionOutcome:
    top_up = await db.scalar(select(TopUp).where(TopUp.id == top_up_id).with_for_update())
    if top_up is None:
        raise TopUpNotFoundError(f"no top-up with id {top_up_id}")

    if top_up.status in TERMINAL_STATUSES:
        return CompletionOutcome(top_up=top_up, action="no_op")

    if top_up.status not in FAILABLE_FROM:
        raise InvalidCompletionStateError(
            f"cannot apply a verified failure to top-up in state {top_up.status!r}"
        )

    now = datetime.now(timezone.utc)
    try:
        transition(top_up.status, "Failed")
        top_up.status = "Failed"
        top_up.failure_code = failure_code
        top_up.failure_message = failure_message
        top_up.updated_at = now

        await log_audit(
            db,
            action="topup_failed",
            resource_type="top_up",
            resource_id=str(top_up.id),
            details={"failure_code": failure_code, "failure_message": failure_message},
        )

        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return CompletionOutcome(top_up=top_up, action="failed")
