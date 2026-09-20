"""Failing tests for T014's application orchestration / atomic ledger
completion (FR-003-FR-011, FR-016), written before services/topup/completion.py
exists (Constitution VI: test-first for financial controls).

Uses the same hand-written async-session double (_FakeDB) T013's
tests/test_topup_endpoints.py used and Devin approved in review, rather than
a real database connection -- the T010 security incident (hardcoded live
Supabase credential, migration applied to the real database without
rotation) is still open as of this task, so nothing here opens a database
connection of any kind.
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from services.topup.ledger import UnbalancedLedgerError
from services.topup.money import Money
from services.topup.state_machine import InvalidTransitionError


class _FakeDB:
    def __init__(self, scalar_values):
        self.scalar_values = list(scalar_values)
        self.statements = []
        self.added = []
        self.commit_count = 0
        self.flush_count = 0
        self.rollback_count = 0

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_values.pop(0)

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        self.added.append(value)

    async def commit(self):
        self.commit_count += 1

    async def flush(self):
        self.flush_count += 1

    async def rollback(self):
        self.rollback_count += 1


class _EventLoggingFakeDB(_FakeDB):
    """Records the exact add/flush/commit sequence so the ledger-posting
    order (unposted insert -> flush -> flip to posted) can be asserted
    without a real PostgreSQL connection -- see "Review: T014" finding 1
    in review-log.md. This cannot exercise the actual trigger functions in
    migration 0018 (that requires a live/disposable PostgreSQL instance,
    which the still-open T010 incident makes unsafe to reach for here); it
    proves the code never constructs/adds a LedgerTransactionRecord with
    is_posted=True before its entries exist, which is the actual bug.
    """

    def __init__(self, scalar_values):
        super().__init__(scalar_values)
        self.events = []

    def add(self, value):
        type_name = type(value).__name__
        if type_name == "LedgerTransactionRecord":
            self.events.append(("add_ledger_transaction", value, value.is_posted))
        elif type_name == "LedgerEntryRecord":
            self.events.append(("add_ledger_entry", value))
        super().add(value)

    async def flush(self):
        self.events.append(("flush",))
        await super().flush()

    async def commit(self):
        self.events.append(("commit",))
        await super().commit()

    async def rollback(self):
        self.events.append(("rollback",))
        await super().rollback()


class _RaisingFakeDB(_FakeDB):
    """A _FakeDB whose flush()/commit() can be told to raise on a given
    call number, to prove exception paths roll back instead of leaving
    dirty pending state (see "Review: T014" finding 2)."""

    def __init__(self, scalar_values, *, fail_flush_on=None, fail_commit_on=None):
        super().__init__(scalar_values)
        self._fail_flush_on = fail_flush_on
        self._fail_commit_on = fail_commit_on
        self._flush_calls = 0
        self._commit_calls = 0

    async def flush(self):
        self._flush_calls += 1
        if self._fail_flush_on == self._flush_calls:
            raise RuntimeError("simulated flush failure")
        await super().flush()

    async def commit(self):
        self._commit_calls += 1
        if self._fail_commit_on == self._commit_calls:
            raise RuntimeError("simulated commit failure")
        await super().commit()


def _top_up(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(),
        internal_reference=f"tu_{uuid.uuid4().hex}",
        wallet_id=uuid.uuid4(),
        agent_id=None,
        funding_method="card",
        provider_name=None,
        provider_transaction_reference=None,
        gross_amount=Decimal("100.00"),
        fee_amount=Decimal("1.50"),
        net_amount=Decimal("98.50"),
        currency="XOF",
        status="Processing",
        failure_code=None,
        failure_message=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _wallet(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        balance=Decimal("500.00"),
        currency="XOF",
        status="active",
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class LedgerEntryBuilderTests(unittest.TestCase):
    """Direct, DB-free coverage of the Constitution-I-critical balancing logic."""

    def test_entries_use_the_established_account_names_and_balance(self):
        from services.topup.completion import (
            CUSTOMER_WALLET_ACCOUNT,
            FEE_INCOME_ACCOUNT,
            PROVIDER_CLEARING_ACCOUNT,
            _ledger_entries_for_completion,
        )
        from services.topup.ledger import post_ledger_transaction

        top_up = _top_up(gross_amount=Decimal("100.00"), fee_amount=Decimal("1.50"), net_amount=Decimal("98.50"))
        entries = _ledger_entries_for_completion(top_up)
        by_account = {e.account: e for e in entries}

        self.assertEqual(CUSTOMER_WALLET_ACCOUNT, "customer_wallet")
        self.assertEqual(PROVIDER_CLEARING_ACCOUNT, "provider_clearing")
        self.assertEqual(FEE_INCOME_ACCOUNT, "fee_income")
        self.assertEqual(by_account["provider_clearing"].direction, "debit")
        self.assertEqual(by_account["provider_clearing"].amount, Money("100.00", "XOF"))
        self.assertEqual(by_account["customer_wallet"].direction, "credit")
        self.assertEqual(by_account["customer_wallet"].amount, Money("98.50", "XOF"))
        self.assertEqual(by_account["fee_income"].direction, "credit")
        self.assertEqual(by_account["fee_income"].amount, Money("1.50", "XOF"))

        # Must not raise UnbalancedLedgerError -- debits == credits.
        post_ledger_transaction(source_reference=top_up.internal_reference, entries=entries)

    def test_zero_fee_omits_the_fee_entry_but_stays_balanced(self):
        from services.topup.completion import _ledger_entries_for_completion
        from services.topup.ledger import post_ledger_transaction

        top_up = _top_up(gross_amount=Decimal("50.00"), fee_amount=Decimal("0.00"), net_amount=Decimal("50.00"))
        entries = _ledger_entries_for_completion(top_up)

        self.assertEqual(len(entries), 2)
        self.assertNotIn("fee_income", {e.account for e in entries})
        post_ledger_transaction(source_reference=top_up.internal_reference, entries=entries)

    def test_entries_never_produce_an_unbalanced_transaction_even_with_bad_stored_amounts(self):
        # A defensive property: if net+fee != gross was somehow stored (DB CHECK
        # constraint amounts_reconcile normally prevents this), the ledger layer's
        # own balance validator must still be the one to catch it, not silently post.
        from services.topup.completion import _ledger_entries_for_completion
        from services.topup.ledger import post_ledger_transaction

        top_up = _top_up(gross_amount=Decimal("100.00"), fee_amount=Decimal("1.50"), net_amount=Decimal("90.00"))
        entries = _ledger_entries_for_completion(top_up)
        with self.assertRaises(UnbalancedLedgerError):
            post_ledger_transaction(source_reference=top_up.internal_reference, entries=entries)


class LedgerPostingOrderTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for "Review: T014" finding 1: migration 0018's
    trg_guard_ledger_entry_mutation is a BEFORE INSERT trigger that rejects
    any ledger_entries row whose parent ledger_transactions row already has
    is_posted=True. The original implementation constructed the parent
    with is_posted=True from the start, so every real PostgreSQL completion
    would have been rejected even though the _FakeDB-based tests passed.

    Round 2 (T017 E2E testing): even after fixing that, a real PostgreSQL
    run (via T017's genuine async end-to-end tests) surfaced a second,
    independent ordering bug these FakeDB-based tests still could not have
    caught on their own: with no ORM relationship() between
    LedgerTransactionRecord and LedgerEntryRecord (only a plain FK column),
    adding the parent and all its entries together and flushing once did
    not reliably guarantee the parent's INSERT reached PostgreSQL before
    the entries' batched multi-row INSERT -- reproduced as a genuine
    ForeignKeyViolationError against a real local database. The fix
    flushes the parent by itself, in its own flush() call, before any
    entry is even added -- these tests now assert that stronger two-flush
    sequence. A live PostgreSQL instance was used to both discover and
    confirm the fix this round (see the T017 handoff in review-log.md for
    the reproduction and verification); this class's own assertions remain
    a FakeDB-based structural proof for fast, DB-free regression coverage
    going forward.
    """

    async def test_ledger_transaction_is_flushed_alone_before_any_entry_is_added(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing")
        wallet = _wallet(id=top_up.wallet_id, currency=top_up.currency)
        db = _EventLoggingFakeDB([top_up, wallet])

        await complete_verified_topup(db, top_up.id, Money(top_up.gross_amount, top_up.currency))

        kinds = [e[0] for e in db.events]
        txn_add_index = kinds.index("add_ledger_transaction")
        entry_add_indices = [i for i, k in enumerate(kinds) if k == "add_ledger_entry"]
        flush_indices = [i for i, k in enumerate(kinds) if k == "flush"]

        self.assertTrue(entry_add_indices, "expected at least one ledger entry to be added")
        # Two distinct flushes: one for the parent alone, one for the entries.
        self.assertGreaterEqual(len(flush_indices), 2, "expected the parent's own flush plus the entries' flush")

        # The parent transaction row must be constructed/added as UNPOSTED --
        # a real INSERT with is_posted=True from the start is exactly what
        # trips the immutability trigger on the very first write.
        self.assertFalse(db.events[txn_add_index][2])

        # The parent must be flushed BY ITSELF -- no entry may be added
        # before that first flush completes, so the parent's real INSERT is
        # guaranteed to reach PostgreSQL before any entry references it via FK.
        first_flush = flush_indices[0]
        self.assertTrue(txn_add_index < first_flush)
        self.assertTrue(all(i > first_flush for i in entry_add_indices))

        # Every entry must be added, then flushed, before the parent is ever
        # flipped to posted.
        second_flush = flush_indices[1]
        self.assertTrue(all(i < second_flush for i in entry_add_indices))

        ledger_record = db.events[txn_add_index][1]
        self.assertTrue(ledger_record.is_posted)
        self.assertIsNotNone(ledger_record.posted_at)
        # The flip to posted must happen strictly after both flushes, never before.
        self.assertGreater(kinds.index("commit"), second_flush)

    async def test_zero_fee_completion_also_inserts_unposted_before_flipping(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(
            status="Processing", gross_amount=Decimal("50.00"),
            fee_amount=Decimal("0.00"), net_amount=Decimal("50.00"), currency="XOF",
        )
        wallet = _wallet(id=top_up.wallet_id, currency="XOF")
        db = _EventLoggingFakeDB([top_up, wallet])

        await complete_verified_topup(db, top_up.id, Money("50.00", "XOF"))

        kinds = [e[0] for e in db.events]
        txn_add_index = kinds.index("add_ledger_transaction")
        self.assertFalse(db.events[txn_add_index][2])
        self.assertTrue(db.events[txn_add_index][1].is_posted)


class CompleteVerifiedTopUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_top_up_raises_not_found(self):
        from services.topup.completion import TopUpNotFoundError, complete_verified_topup

        db = _FakeDB([None])
        with self.assertRaises(TopUpNotFoundError):
            await complete_verified_topup(db, uuid.uuid4(), Money("100.00", "XOF"))
        self.assertEqual(db.commit_count, 0)

    async def test_terminal_states_are_a_no_op_and_never_reach_wallet_or_ledger(self):
        from services.topup.completion import complete_verified_topup

        for status in ("Completed", "Failed", "Expired", "Cancelled", "Reversed", "UnderReview"):
            with self.subTest(status=status):
                top_up = _top_up(status=status, gross_amount=Decimal("100.00"), currency="XOF")
                db = _FakeDB([top_up])
                outcome = await complete_verified_topup(db, top_up.id, Money("100.00", "XOF"))
                self.assertEqual(outcome.action, "no_op")
                self.assertEqual(outcome.top_up.status, status)
                if status in ("Failed", "Expired", "Cancelled"):
                    # T034g: money arriving for a top-up that can no longer be credited leaves
                    # exactly one audit record for operators, and nothing else (no ledger row).
                    self.assertEqual(
                        [getattr(a, "action", None) for a in db.added],
                        ["topup_late_success_on_terminal_top_up"],
                    )
                else:
                    self.assertEqual(db.added, [])
                # Only the top-up lookup consumed a scalar -- the wallet was never queried.
                self.assertEqual(db.scalar_values, [])

    async def test_duplicate_completed_event_with_mismatched_amount_is_audited_but_not_reposted(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Completed", gross_amount=Decimal("100.00"), currency="XOF")
        db = _FakeDB([top_up])
        outcome = await complete_verified_topup(db, top_up.id, Money("999.00", "XOF"))

        self.assertEqual(outcome.action, "no_op")
        self.assertEqual(top_up.status, "Completed")
        audit_entries = [a for a in db.added if getattr(a, "action", None) == "topup_duplicate_event_amount_mismatch"]
        self.assertEqual(len(audit_entries), 1)
        self.assertEqual(db.commit_count, 1)

    async def test_created_top_up_cannot_be_completed(self):
        # T034b: Created (never reached Pending) stays non-completable. Pending and
        # RequiresAction are completable via the implied Processing hop; see
        # ImpliedIntermediateTransitionTests.
        from services.topup.completion import InvalidCompletionStateError, complete_verified_topup

        top_up = _top_up(status="Created")
        db = _FakeDB([top_up])
        with self.assertRaises(InvalidCompletionStateError):
            await complete_verified_topup(db, top_up.id, Money("100.00", "XOF"))
        self.assertEqual(db.commit_count, 0)
        self.assertEqual(top_up.status, "Created")

    async def test_amount_mismatch_routes_to_under_review_without_posting_ledger_or_touching_wallet(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing", gross_amount=Decimal("100.00"), currency="XOF")
        db = _FakeDB([top_up])  # only one scalar value: wallet must never be looked up
        outcome = await complete_verified_topup(db, top_up.id, Money("100.01", "XOF"))

        self.assertEqual(outcome.action, "under_review")
        self.assertEqual(top_up.status, "UnderReview")
        self.assertIsNone(top_up.completed_at)
        self.assertEqual(db.scalar_values, [])
        ledger_rows = [a for a in db.added if type(a).__name__ == "LedgerTransactionRecord"]
        self.assertEqual(ledger_rows, [])
        audit_entries = [a for a in db.added if getattr(a, "action", None) == "topup_amount_mismatch"]
        self.assertEqual(len(audit_entries), 1)
        self.assertEqual(db.commit_count, 1)

    async def test_currency_mismatch_also_routes_to_under_review(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing", gross_amount=Decimal("100.00"), currency="XOF")
        db = _FakeDB([top_up])
        outcome = await complete_verified_topup(db, top_up.id, Money("100.00", "USD"))

        self.assertEqual(outcome.action, "under_review")
        self.assertEqual(top_up.status, "UnderReview")

    async def test_wallet_currency_mismatch_routes_to_under_review_without_crediting_or_posting_ledger(self):
        # Regression for "Re-review: T012 and T014" CRITICAL finding: a USD
        # top-up must never post USD ledger entries and credit an EUR wallet
        # just because the provider-reported amount matched the top-up's own
        # stored currency -- the top-up's currency must also be checked
        # against the wallet it is about to credit.
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(
            status="Processing", gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"), net_amount=Decimal("98.50"), currency="USD",
        )
        wallet = _wallet(id=top_up.wallet_id, balance=Decimal("10.00"), currency="EUR")
        db = _FakeDB([top_up, wallet])

        outcome = await complete_verified_topup(db, top_up.id, Money("100.00", "USD"))

        self.assertEqual(outcome.action, "under_review")
        self.assertEqual(top_up.status, "UnderReview")
        self.assertIsNone(top_up.completed_at)
        # The wallet balance projection must be completely untouched.
        self.assertEqual(wallet.balance, Decimal("10.00"))
        ledger_rows = [a for a in db.added if type(a).__name__.startswith("Ledger")]
        self.assertEqual(ledger_rows, [])
        audit_entries = [a for a in db.added if getattr(a, "action", None) == "topup_wallet_currency_mismatch"]
        self.assertEqual(len(audit_entries), 1)
        self.assertEqual(db.commit_count, 1)

    async def test_verified_completion_posts_balanced_ledger_credits_wallet_and_notifies(self):
        from models.topup import LedgerEntryRecord, LedgerTransactionRecord
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(
            status="Processing", gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"), net_amount=Decimal("98.50"), currency="XOF",
        )
        wallet = _wallet(id=top_up.wallet_id, balance=Decimal("500.00"), currency="XOF")
        db = _FakeDB([top_up, wallet])

        outcome = await complete_verified_topup(
            db, top_up.id, Money("100.00", "XOF"), provider_transaction_reference="ptx_123"
        )

        self.assertEqual(outcome.action, "completed")
        self.assertEqual(top_up.status, "Completed")
        self.assertIsNotNone(top_up.completed_at)
        self.assertEqual(top_up.provider_transaction_reference, "ptx_123")
        self.assertEqual(wallet.balance, Decimal("598.50"))

        ledger_txns = [a for a in db.added if isinstance(a, LedgerTransactionRecord)]
        self.assertEqual(len(ledger_txns), 1)
        self.assertTrue(ledger_txns[0].is_posted)
        self.assertIsNotNone(ledger_txns[0].id)

        entries = [a for a in db.added if isinstance(a, LedgerEntryRecord)]
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry.ledger_transaction_id, ledger_txns[0].id)
        debit_total = sum(e.amount for e in entries if e.direction == "debit")
        credit_total = sum(e.amount for e in entries if e.direction == "credit")
        self.assertEqual(debit_total, credit_total)

        audit_entries = [a for a in db.added if getattr(a, "action", None) == "topup_completed"]
        self.assertEqual(len(audit_entries), 1)

        notifications = [a for a in db.added if type(a).__name__ == "Notification"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].user_id, wallet.user_id)
        rendered = str(notifications[0].data)
        self.assertNotIn("funding_token", rendered)
        self.assertNotIn("card", rendered.lower())

        # Financial commit, then a separate commit for the post-commit notification.
        self.assertEqual(db.commit_count, 2)

    async def test_zero_fee_completion_posts_two_balanced_entries(self):
        from models.topup import LedgerEntryRecord
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(
            status="Processing", gross_amount=Decimal("50.00"),
            fee_amount=Decimal("0.00"), net_amount=Decimal("50.00"), currency="XOF",
        )
        wallet = _wallet(id=top_up.wallet_id, balance=Decimal("0.00"), currency="XOF")
        db = _FakeDB([top_up, wallet])

        outcome = await complete_verified_topup(db, top_up.id, Money("50.00", "XOF"))

        self.assertEqual(outcome.action, "completed")
        entries = [a for a in db.added if isinstance(a, LedgerEntryRecord)]
        self.assertEqual(len(entries), 2)
        self.assertEqual(wallet.balance, Decimal("50.00"))

    async def test_missing_wallet_raises_and_rolls_back_the_pending_ledger_mutations(self):
        from services.topup.completion import WalletNotFoundError, complete_verified_topup

        top_up = _top_up(status="Processing")
        db = _FakeDB([top_up, None])
        with self.assertRaises(WalletNotFoundError):
            await complete_verified_topup(db, top_up.id, Money(top_up.gross_amount, top_up.currency))
        # No partial commit -- the missing wallet is discovered before commit() is ever called.
        self.assertEqual(db.commit_count, 0)
        # The already-flushed ledger transaction/entries and the state-machine
        # transition to Completed must not be left pending against the session.
        self.assertEqual(db.rollback_count, 1)

    async def test_flush_failure_while_posting_ledger_entries_rolls_back_before_any_state_changes(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing")
        wallet = _wallet(id=top_up.wallet_id, currency=top_up.currency)
        db = _RaisingFakeDB([top_up, wallet], fail_flush_on=1)

        with self.assertRaises(RuntimeError):
            await complete_verified_topup(db, top_up.id, Money(top_up.gross_amount, top_up.currency))

        self.assertEqual(db.commit_count, 0)
        self.assertEqual(db.rollback_count, 1)
        # The flush (which would carry the unposted ledger insert to PostgreSQL)
        # failed before the top-up was ever transitioned to Completed.
        self.assertEqual(top_up.status, "Processing")

    async def test_commit_failure_during_completion_rolls_back(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing")
        wallet = _wallet(id=top_up.wallet_id, currency=top_up.currency)
        db = _RaisingFakeDB([top_up, wallet], fail_commit_on=1)

        with self.assertRaises(RuntimeError):
            await complete_verified_topup(db, top_up.id, Money(top_up.gross_amount, top_up.currency))

        self.assertEqual(db.rollback_count, 1)

    async def test_commit_failure_during_under_review_routing_rolls_back(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing", gross_amount=Decimal("100.00"), currency="XOF")
        db = _RaisingFakeDB([top_up], fail_commit_on=1)

        with self.assertRaises(RuntimeError):
            await complete_verified_topup(db, top_up.id, Money("999.00", "XOF"))

        self.assertEqual(db.rollback_count, 1)

    async def test_notification_failure_rolls_back_only_the_notification_transaction(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Processing")
        wallet = _wallet(id=top_up.wallet_id, currency=top_up.currency)
        db = _RaisingFakeDB([top_up, wallet], fail_commit_on=2)

        outcome = await complete_verified_topup(db, top_up.id, Money(top_up.gross_amount, top_up.currency))

        self.assertEqual(outcome.action, "completed")
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(db.commit_count, 1)
        # Exactly one rollback -- for the failed second (notification) transaction
        # only. The already-committed financial transaction is never rolled back.
        self.assertEqual(db.rollback_count, 1)

    async def test_notification_failure_does_not_undo_or_retry_the_already_committed_money_movement(self):
        from services.topup.completion import complete_verified_topup

        class _FailingNotifyDB(_FakeDB):
            async def commit(self):
                self.commit_count += 1
                if self.commit_count == 2:
                    raise RuntimeError("notification outbox unavailable")

        top_up = _top_up(status="Processing")
        wallet = _wallet(id=top_up.wallet_id, currency=top_up.currency)
        db = _FailingNotifyDB([top_up, wallet])

        outcome = await complete_verified_topup(db, top_up.id, Money(top_up.gross_amount, top_up.currency))

        self.assertEqual(outcome.action, "completed")
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(db.commit_count, 2)


class ApplyVerifiedFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_top_up_can_fail_without_touching_the_ledger(self):
        from services.topup.completion import apply_verified_failure

        top_up = _top_up(status="Processing")
        db = _FakeDB([top_up])
        outcome = await apply_verified_failure(db, top_up.id, failure_code="card_declined", failure_message="Declined")

        self.assertEqual(outcome.action, "failed")
        self.assertEqual(top_up.status, "Failed")
        self.assertEqual(top_up.failure_code, "card_declined")
        ledger_rows = [a for a in db.added if type(a).__name__.startswith("Ledger")]
        self.assertEqual(ledger_rows, [])
        self.assertEqual(db.commit_count, 1)

    async def test_requires_action_top_up_can_also_fail(self):
        from services.topup.completion import apply_verified_failure

        top_up = _top_up(status="RequiresAction")
        db = _FakeDB([top_up])
        outcome = await apply_verified_failure(db, top_up.id, failure_code="timeout", failure_message="Timed out")
        self.assertEqual(outcome.action, "failed")
        self.assertEqual(top_up.status, "Failed")

    async def test_already_terminal_top_up_failure_event_is_a_no_op(self):
        from services.topup.completion import apply_verified_failure

        top_up = _top_up(status="Completed")
        db = _FakeDB([top_up])
        outcome = await apply_verified_failure(db, top_up.id, failure_code="ignored", failure_message="ignored")
        self.assertEqual(outcome.action, "no_op")
        self.assertEqual(top_up.status, "Completed")

    async def test_pending_top_up_can_receive_a_verified_failure(self):
        # T034b: spec.md's State Transitions table allows Pending -> Failed ("provider
        # rejected"). A card declined at confirmation never produces a Processing
        # event, so rejecting this left the top-up stuck in Pending.
        from services.topup.completion import apply_verified_failure

        top_up = _top_up(status="Pending")
        db = _FakeDB([top_up])
        outcome = await apply_verified_failure(db, top_up.id, failure_code="card_declined", failure_message="Declined")
        self.assertEqual(outcome.action, "failed")
        self.assertEqual(top_up.status, "Failed")
        self.assertEqual(db.commit_count, 1)

    async def test_created_top_up_cannot_receive_a_verified_failure(self):
        from services.topup.completion import InvalidCompletionStateError, apply_verified_failure

        top_up = _top_up(status="Created")
        db = _FakeDB([top_up])
        with self.assertRaises(InvalidCompletionStateError):
            await apply_verified_failure(db, top_up.id, failure_code="x", failure_message="x")
        self.assertEqual(db.commit_count, 0)

    async def test_commit_failure_rolls_back(self):
        from services.topup.completion import apply_verified_failure

        top_up = _top_up(status="Processing")
        db = _RaisingFakeDB([top_up], fail_commit_on=1)

        with self.assertRaises(RuntimeError):
            await apply_verified_failure(db, top_up.id, failure_code="x", failure_message="x")

        self.assertEqual(db.rollback_count, 1)


class ImpliedIntermediateTransitionTests(unittest.IsolatedAsyncioTestCase):
    """T034b. A real provider does not have to emit a Processing event before a
    terminal one: a card payment can go straight to succeeded, and Stripe does not
    guarantee event order. A verified completion for a Pending or RequiresAction
    top-up therefore applies the intermediate Processing transition first, in the
    same atomic transaction, instead of failing with an illegal transition (which
    would leave a paid-for top-up permanently uncredited). The pinned transition
    table is unchanged: both Pending and RequiresAction may already go to Processing."""

    async def _complete(self, status):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(
            status=status, gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"), net_amount=Decimal("98.50"), currency="XOF",
        )
        wallet = _wallet(id=top_up.wallet_id, balance=Decimal("500.00"), currency="XOF")
        db = _FakeDB([top_up, wallet])
        outcome = await complete_verified_topup(
            db, top_up.id, Money("100.00", "XOF"), provider_transaction_reference="ptx_1"
        )
        return top_up, wallet, db, outcome

    async def _assert_completes_with_balanced_ledger(self, status):
        from models.topup import LedgerEntryRecord, LedgerTransactionRecord

        top_up, wallet, db, outcome = await self._complete(status)
        self.assertEqual(outcome.action, "completed")
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(wallet.balance, Decimal("598.50"))
        txns = [a for a in db.added if isinstance(a, LedgerTransactionRecord)]
        entries = [a for a in db.added if isinstance(a, LedgerEntryRecord)]
        self.assertEqual(len(txns), 1)
        self.assertTrue(txns[0].is_posted)
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            sum(e.amount for e in entries if e.direction == "debit"),
            sum(e.amount for e in entries if e.direction == "credit"),
        )

    async def test_pending_top_up_completes_and_credits_exactly_once(self):
        await self._assert_completes_with_balanced_ledger("Pending")

    async def test_requires_action_top_up_completes_and_credits_exactly_once(self):
        await self._assert_completes_with_balanced_ledger("RequiresAction")

    async def test_pending_top_up_with_amount_mismatch_still_routes_to_under_review(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Pending", gross_amount=Decimal("100.00"), currency="XOF")
        db = _FakeDB([top_up])  # the wallet must never be looked up
        outcome = await complete_verified_topup(db, top_up.id, Money("100.01", "XOF"))
        self.assertEqual(outcome.action, "under_review")
        self.assertEqual(top_up.status, "UnderReview")
        self.assertEqual([a for a in db.added if type(a).__name__ == "LedgerTransactionRecord"], [])

    async def test_a_failure_after_the_implied_hop_rolls_back(self):
        from services.topup.completion import WalletNotFoundError, complete_verified_topup

        top_up = _top_up(status="Pending", currency="XOF")
        db = _FakeDB([top_up, None])  # wallet lookup returns nothing
        with self.assertRaises(WalletNotFoundError):
            await complete_verified_topup(db, top_up.id, Money("100.00", "XOF"))
        self.assertGreaterEqual(db.rollback_count, 1)
        self.assertEqual(db.commit_count, 0)


class VerifiedProgressTests(unittest.IsolatedAsyncioTestCase):
    """T034b. apply_verified_progress() records a verified NON-terminal provider
    event (Processing / RequiresAction). It moves no money. Providers repeat and
    reorder events, so anything that would be a backwards or sideways move is a
    quiet no-op, never an error and never a state regression."""

    async def _apply(self, current, new_status):
        from services.topup.completion import apply_verified_progress

        top_up = _top_up(status=current)
        db = _FakeDB([top_up])
        outcome = await apply_verified_progress(db, top_up.id, new_status)
        return top_up, db, outcome

    async def test_pending_moves_to_processing(self):
        top_up, db, outcome = await self._apply("Pending", "Processing")
        self.assertEqual((outcome.action, top_up.status, db.commit_count), ("progressed", "Processing", 1))
        audit = [a for a in db.added if getattr(a, "action", None) == "topup_status_progressed"]
        self.assertEqual(len(audit), 1)

    async def test_requires_action_moves_to_processing(self):
        top_up, db, outcome = await self._apply("RequiresAction", "Processing")
        self.assertEqual((outcome.action, top_up.status), ("progressed", "Processing"))

    async def test_pending_moves_to_requires_action(self):
        top_up, db, outcome = await self._apply("Pending", "RequiresAction")
        self.assertEqual((outcome.action, top_up.status), ("progressed", "RequiresAction"))

    async def test_late_requires_action_after_processing_is_a_no_op(self):
        top_up, db, outcome = await self._apply("Processing", "RequiresAction")
        self.assertEqual((outcome.action, top_up.status, db.commit_count), ("no_op", "Processing", 0))

    async def test_repeated_processing_is_a_no_op(self):
        top_up, db, outcome = await self._apply("Processing", "Processing")
        self.assertEqual((outcome.action, top_up.status, db.commit_count), ("no_op", "Processing", 0))

    async def test_progress_never_touches_a_terminal_top_up(self):
        for terminal in ("Completed", "Failed", "Expired", "Cancelled", "Reversed", "UnderReview"):
            with self.subTest(terminal=terminal):
                top_up, db, outcome = await self._apply(terminal, "Processing")
                self.assertEqual((outcome.action, top_up.status, db.commit_count), ("no_op", terminal, 0))

    async def test_created_top_up_is_rejected(self):
        from services.topup.completion import InvalidCompletionStateError, apply_verified_progress

        top_up = _top_up(status="Created")
        db = _FakeDB([top_up])
        with self.assertRaises(InvalidCompletionStateError):
            await apply_verified_progress(db, top_up.id, "Processing")
        self.assertEqual(db.commit_count, 0)

    async def test_only_non_terminal_statuses_are_accepted(self):
        from services.topup.completion import apply_verified_progress

        for bad in ("Completed", "Failed", "Cancelled", "bogus"):
            with self.subTest(bad=bad):
                db = _FakeDB([_top_up(status="Pending")])
                with self.assertRaises(ValueError):
                    await apply_verified_progress(db, uuid.uuid4(), bad)
                self.assertEqual(db.commit_count, 0)

    async def test_unknown_top_up_raises(self):
        from services.topup.completion import TopUpNotFoundError, apply_verified_progress

        with self.assertRaises(TopUpNotFoundError):
            await apply_verified_progress(_FakeDB([None]), uuid.uuid4(), "Processing")

    async def test_commit_failure_rolls_back(self):
        from services.topup.completion import apply_verified_progress

        top_up = _top_up(status="Pending")
        db = _RaisingFakeDB([top_up], fail_commit_on=1)
        with self.assertRaises(RuntimeError):
            await apply_verified_progress(db, top_up.id, "Processing")
        self.assertEqual(db.rollback_count, 1)


class LateSuccessOnTerminalTopUpTests(unittest.IsolatedAsyncioTestCase):
    """Review finding (T034g). A verified provider success can still arrive for a top-up
    that is already Cancelled, Failed or Expired (cancelled locally before the fix, or a race).
    Money was taken for something this system cannot credit. It must never be credited
    silently as if nothing happened, and it must never be quietly dropped either: the state
    stays terminal (no refund or reconciliation behavior is specified yet, see spec.md), but a
    high-visibility audit record is written so an operator can find it."""

    async def _late_success(self, status):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status=status, gross_amount=Decimal("100.00"), currency="XOF")
        db = _FakeDB([top_up])
        outcome = await complete_verified_topup(
            db, top_up.id, Money("100.00", "XOF"), provider_transaction_reference="ptx_late"
        )
        return top_up, db, outcome

    async def test_success_for_a_cancelled_failed_or_expired_top_up_is_recorded_and_changes_nothing(self):
        for status in ("Cancelled", "Failed", "Expired"):
            with self.subTest(status=status):
                top_up, db, outcome = await self._late_success(status)
                self.assertEqual((outcome.action, top_up.status), ("no_op", status))
                self.assertEqual(db.scalar_values, [])  # wallet never looked up
                self.assertEqual([a for a in db.added if type(a).__name__.startswith("Ledger")], [])
                audit = [a for a in db.added if getattr(a, "action", None) == "topup_late_success_on_terminal_top_up"]
                self.assertEqual(len(audit), 1)
                self.assertEqual(db.commit_count, 1)
                rendered = str(audit[0].details)
                self.assertIn(status, rendered)
                self.assertIn("100.00", rendered)
                self.assertIn("ptx_late", rendered)

    async def test_a_duplicate_success_for_a_completed_top_up_is_still_a_silent_no_op(self):
        top_up, db, outcome = await self._late_success("Completed")
        self.assertEqual((outcome.action, top_up.status, db.commit_count), ("no_op", "Completed", 0))

    async def test_success_for_an_under_review_or_reversed_top_up_is_not_flagged_as_late(self):
        for status in ("UnderReview", "Reversed"):
            with self.subTest(status=status):
                top_up, db, outcome = await self._late_success(status)
                self.assertEqual((outcome.action, db.commit_count), ("no_op", 0))

    async def test_a_failure_writing_the_record_rolls_back_and_propagates(self):
        from services.topup.completion import complete_verified_topup

        top_up = _top_up(status="Cancelled", currency="XOF")
        db = _RaisingFakeDB([top_up], fail_commit_on=1)
        with self.assertRaises(RuntimeError):
            await complete_verified_topup(db, top_up.id, Money("100.00", "XOF"))
        self.assertEqual(db.rollback_count, 1)


class PaymentAttemptFailureTests(unittest.IsolatedAsyncioTestCase):
    """T034i (human decision 2026-09-19, option A). A provider reports every failed attempt
    (a declined card) while leaving the payment payable, so a failed attempt is RECORDED but
    never ends the top-up: the customer retries on the same payment, and a later verified
    success must still credit it. Reproduced in the first real end-to-end run: treating the
    attempt as terminal `Failed` left a retried payment charged and never credited."""

    async def _record(self, status, code="card_declined"):
        from services.topup.completion import record_payment_attempt_failure

        top_up = _top_up(status=status)
        db = _FakeDB([top_up])
        outcome = await record_payment_attempt_failure(db, top_up.id, code)
        return top_up, db, outcome

    async def test_an_open_top_up_stays_open_and_the_attempt_is_audited(self):
        for status in ("Pending", "RequiresAction", "Processing"):
            with self.subTest(status=status):
                top_up, db, outcome = await self._record(status)
                self.assertEqual((outcome.action, top_up.status), ("attempt_failed_recorded", status))
                audit = [a for a in db.added if getattr(a, "action", None) == "topup_payment_attempt_failed"]
                self.assertEqual(len(audit), 1)
                self.assertEqual(audit[0].details["failure_code"], "card_declined")
                self.assertEqual(audit[0].details["top_up_status"], status)
                self.assertEqual(db.commit_count, 1)
                # Not a terminal failure: no failure fields written, no wallet, no ledger.
                self.assertIsNone(top_up.failure_code)
                self.assertEqual(db.scalar_values, [])
                self.assertEqual([a for a in db.added if type(a).__name__.startswith("Ledger")], [])

    async def test_a_missing_error_code_is_recorded_as_unknown(self):
        _, db, _ = await self._record("Pending", code=None)
        audit = [a for a in db.added if getattr(a, "action", None) == "topup_payment_attempt_failed"]
        self.assertEqual(audit[0].details["failure_code"], "unknown")

    async def test_a_terminal_top_up_is_left_alone(self):
        for status in ("Completed", "Failed", "Expired", "Cancelled", "Reversed", "UnderReview"):
            with self.subTest(status=status):
                top_up, db, outcome = await self._record(status)
                self.assertEqual((outcome.action, top_up.status, db.commit_count, db.added), ("no_op", status, 0, []))

    async def test_a_created_top_up_is_rejected(self):
        from services.topup.completion import InvalidCompletionStateError, record_payment_attempt_failure

        top_up = _top_up(status="Created")
        db = _FakeDB([top_up])
        with self.assertRaises(InvalidCompletionStateError):
            await record_payment_attempt_failure(db, top_up.id, "card_declined")
        self.assertEqual(db.commit_count, 0)

    async def test_an_unknown_top_up_raises(self):
        from services.topup.completion import TopUpNotFoundError, record_payment_attempt_failure

        with self.assertRaises(TopUpNotFoundError):
            await record_payment_attempt_failure(_FakeDB([None]), uuid.uuid4(), "card_declined")

    async def test_a_commit_failure_rolls_back_and_propagates(self):
        from services.topup.completion import record_payment_attempt_failure

        top_up = _top_up(status="Pending")
        db = _RaisingFakeDB([top_up], fail_commit_on=1)
        with self.assertRaises(RuntimeError):
            await record_payment_attempt_failure(db, top_up.id, "card_declined")
        self.assertEqual(db.rollback_count, 1)

    async def test_a_success_after_a_failed_attempt_still_credits_the_top_up(self):
        # The regression: decline, then a successful retry on the same payment.
        from services.topup.completion import complete_verified_topup, record_payment_attempt_failure

        top_up = _top_up(
            status="Pending", gross_amount=Decimal("100.00"), fee_amount=Decimal("1.50"),
            net_amount=Decimal("98.50"), currency="XOF",
        )
        wallet = _wallet(id=top_up.wallet_id, balance=Decimal("500.00"), currency="XOF")
        await record_payment_attempt_failure(_FakeDB([top_up]), top_up.id, "card_declined")
        self.assertEqual(top_up.status, "Pending")

        db = _FakeDB([top_up, wallet])
        outcome = await complete_verified_topup(db, top_up.id, Money("100.00", "XOF"))
        self.assertEqual((outcome.action, top_up.status, wallet.balance), ("completed", "Completed", Decimal("598.50")))


if __name__ == "__main__":
    unittest.main()
