"""Top-up reversal application orchestration (T019; FR-013, FR-014).

services/topup/reversal.py (T007) already owns the pure domain rule --
flip every entry's direction, block on insufficient balance by default,
treat an already-reversed transaction as a no-op -- against hand-built
LedgerTransaction objects. This module is the DB-integrated layer T007
deliberately left undone: it locks the real TopUp/Wallet rows, loads the
original's persisted ledger entries, and persists what reverse_topup()
computes, the same shape completion.py (T014) already established for the
forward path.

Why a *new TopUp row*, not just a second ledger transaction hung off the
original: migrations/0018 makes LedgerTransactionRecord.top_up_id NOT NULL
and UNIQUE (one ledger transaction per top-up), so the original's row is
already spoken for. plan.md's own "Reversal relationship table" and the
top_up_reversals schema (original_top_up_id/reversal_top_up_id, both
unique) both assume the reversal is itself represented by a second TopUp
row, linked back to the first -- FR-013's "new linked transaction" is that
row, not a schema violation waiting to happen. Its amounts mirror the
original's exactly (gross/fee/net all carried over unchanged) because the
compensating entries reverse the *whole* original transaction -- provider
clearing, customer wallet, and fee income legs alike, per reverse_topup()'s
own "flip every entry" rule -- not just the customer-facing net amount.
Only the customer_wallet leg has a wallet-balance projection, so that is
the only leg that ever touches Wallet.balance.

Idempotency (FR-014) is enforced here via the persisted top_up_reversals
link, not via reverse_topup()'s own already_reversed parameter -- a
database-backed check survives process restarts and concurrent duplicate
requests (both rows are locked with with_for_update()) without requiring
the caller to still be holding the previous in-memory LedgerTransaction.

Insufficient-balance handling resolves a wording tension in spec.md: the
"Still needs clarification" note says a blocked reversal should "route the
transaction to UnderReview," but the State Transitions table two lines
above it is explicit that Completed MUST NOT transition to UnderReview (or
anything but Reversed) -- ledger entries already posted for a Completed
top-up are immutable per Constitution I, and ALLOWED_TRANSITIONS is this
project's own pinned single source of truth for that table (see T017/T018
review history). A blocked reversal therefore leaves the original's status
untouched -- it stays Completed, exactly as it was -- and instead writes an
audit-log entry an operations user can act on, satisfying the "manual
operator resolution" intent of that note without violating the transition
table its own author (spec.md) pinned three lines earlier. This is the
"interim safe default," implemented behind the same swappable policy_hook
reverse_topup() already exposes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select

from models.topup import LedgerEntryRecord, LedgerTransactionRecord, TopUp, TopUpReversal
from models.wallet import Wallet
from services.topup.completion import CUSTOMER_WALLET_ACCOUNT
from services.topup.ledger import LedgerEntry, LedgerTransaction
from services.topup.money import Money
from services.topup.reversal import ReversalBlockedError, block_on_insufficient_balance, reverse_topup
from services.topup.state_machine import transition
from services.wallet_policy import money
from utils.audit import log_audit

REVERSIBLE_FROM = {"Completed"}
ALREADY_REVERSED = {"Reversed"}


class TopUpNotFoundError(LookupError):
    pass


class WalletNotFoundError(LookupError):
    pass


class InvalidReversalStateError(ValueError):
    pass


class ReversalDataIntegrityError(RuntimeError):
    """The original top-up is Completed/Reversed but its ledger records
    are missing or inconsistent -- a data-integrity bug, not a business
    outcome, so this is never swallowed into a normal outcome."""


@dataclass(frozen=True)
class ReversalOutcome:
    original: TopUp
    action: str  # "reversed" | "no_op" | "blocked"
    reversal_top_up: Optional[TopUp] = None
    ledger_transaction: Optional[LedgerTransactionRecord] = None


async def reverse_completed_topup(
    db,
    top_up_id: uuid.UUID,
    *,
    initiated_by: Optional[uuid.UUID] = None,
    reason: Optional[str] = None,
    policy_hook: Callable[[Money, Money], bool] = block_on_insufficient_balance,
) -> ReversalOutcome:
    original = await db.scalar(select(TopUp).where(TopUp.id == top_up_id).with_for_update())
    if original is None:
        raise TopUpNotFoundError(f"no top-up with id {top_up_id}")

    existing_link = await db.scalar(
        select(TopUpReversal).where(TopUpReversal.original_top_up_id == original.id)
    )
    if existing_link is not None:
        reversal_top_up = await db.scalar(
            select(TopUp).where(TopUp.id == existing_link.reversal_top_up_id)
        )
        if reversal_top_up is None:
            raise ReversalDataIntegrityError(
                f"top_up_reversals row {existing_link.id} points at a missing top-up "
                f"{existing_link.reversal_top_up_id}"
            )
        return ReversalOutcome(original=original, action="no_op", reversal_top_up=reversal_top_up)

    if original.status in ALREADY_REVERSED:
        raise ReversalDataIntegrityError(
            f"top-up {original.id} is Reversed but has no top_up_reversals row"
        )

    if original.status not in REVERSIBLE_FROM:
        raise InvalidReversalStateError(
            f"cannot reverse top-up {original.id} from state {original.status!r}; "
            f"expected 'Completed'"
        )

    now = datetime.now(timezone.utc)
    try:
        ledger_record = await db.scalar(
            select(LedgerTransactionRecord).where(LedgerTransactionRecord.top_up_id == original.id)
        )
        if ledger_record is None:
            raise ReversalDataIntegrityError(
                f"Completed top-up {original.id} has no posted ledger transaction"
            )
        if not ledger_record.is_posted:
            raise ReversalDataIntegrityError(
                f"Completed top-up {original.id} has an unposted ledger transaction"
            )
        entry_rows = (
            await db.scalars(
                select(LedgerEntryRecord).where(
                    LedgerEntryRecord.ledger_transaction_id == ledger_record.id
                )
            )
        ).all()

        wallet = await db.scalar(select(Wallet).where(Wallet.id == original.wallet_id).with_for_update())
        if wallet is None:
            raise WalletNotFoundError(f"no wallet with id {original.wallet_id} for top-up {original.id}")

        domain_original = LedgerTransaction(
            source_reference=ledger_record.source_reference,
            entries=tuple(
                LedgerEntry(
                    account=row.account_code,
                    direction=row.direction,
                    amount=Money(row.amount, ledger_record.currency),
                )
                for row in entry_rows
            ),
        )
        current_balance = Money(wallet.balance, wallet.currency)

        try:
            compensating = reverse_topup(domain_original, current_balance, policy_hook=policy_hook)
        except ReversalBlockedError:
            reversal_amount = next(
                e.amount for e in domain_original.entries if e.account == CUSTOMER_WALLET_ACCOUNT
            )
            await log_audit(
                db,
                action="topup_reversal_blocked_insufficient_balance",
                resource_type="top_up",
                resource_id=str(original.id),
                details={
                    "balance": str(current_balance.amount),
                    "reversal_amount": str(reversal_amount.amount),
                    "currency": current_balance.currency,
                    "reason": reason,
                    "initiated_by": str(initiated_by) if initiated_by else None,
                },
            )
            await db.commit()
            return ReversalOutcome(original=original, action="blocked")

        new_top_up = TopUp(
            id=uuid.uuid4(),
            internal_reference=f"rev_{uuid.uuid4().hex}",
            wallet_id=original.wallet_id,
            agent_id=original.agent_id,
            idempotency_key=f"reversal:{original.id}",
            request_fingerprint=f"reversal:{original.id}",
            funding_method=original.funding_method,
            gross_amount=original.gross_amount,
            fee_amount=original.fee_amount,
            net_amount=original.net_amount,
            currency=original.currency,
            status="Created",
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        new_top_up.status = transition(new_top_up.status, "Pending")
        new_top_up.status = transition(new_top_up.status, "Processing")
        db.add(new_top_up)

        reversal_ledger_record = LedgerTransactionRecord(
            id=uuid.uuid4(),
            source_reference=compensating.source_reference,
            top_up_id=new_top_up.id,
            currency=ledger_record.currency,
            is_posted=False,
            posted_at=None,
        )
        db.add(reversal_ledger_record)
        await db.flush()

        for entry in compensating.entries:
            db.add(
                LedgerEntryRecord(
                    id=uuid.uuid4(),
                    ledger_transaction_id=reversal_ledger_record.id,
                    account_code=entry.account,
                    direction=entry.direction,
                    amount=entry.amount.amount,
                )
            )
        await db.flush()

        reversal_ledger_record.is_posted = True
        reversal_ledger_record.posted_at = now
        new_top_up.status = transition(new_top_up.status, "Completed")

        db.add(
            TopUpReversal(
                id=uuid.uuid4(),
                original_top_up_id=original.id,
                reversal_top_up_id=new_top_up.id,
            )
        )

        wallet_leg_amount = next(
            e.amount for e in compensating.entries if e.account == CUSTOMER_WALLET_ACCOUNT
        )
        wallet.balance = money(wallet.balance) - wallet_leg_amount.amount
        wallet.updated_at = now

        original.status = transition(original.status, "Reversed")
        original.updated_at = now

        await log_audit(
            db,
            action="topup_reversed",
            resource_type="top_up",
            resource_id=str(original.id),
            details={
                "reversal_top_up_id": str(new_top_up.id),
                "reversal_internal_reference": new_top_up.internal_reference,
                "wallet_debited": str(wallet_leg_amount.amount),
                "currency": wallet_leg_amount.currency,
                "reason": reason,
                "initiated_by": str(initiated_by) if initiated_by else None,
            },
        )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return ReversalOutcome(
        original=original,
        action="reversed",
        reversal_top_up=new_top_up,
        ledger_transaction=reversal_ledger_record,
    )
