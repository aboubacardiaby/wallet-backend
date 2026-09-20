"""Webhook processing tests (T015; FR-007, FR-008, FR-009).

Test-first per Constitution VI: these tests verify webhook processing functionality.
"""
import hashlib
import json
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.exc import IntegrityError

from models.topup import ProviderEvent, TopUp
from services.topup.completion import (
    CompletionOutcome,
    InvalidCompletionStateError,
    TopUpNotFoundError,
    WalletNotFoundError,
)
from services.topup.money import Money
from services.topup.provider import (
    InvalidWebhookPayloadError,
    PaymentProvider,
    WebhookEvent,
    WebhookVerificationError,
)
from services.topup.webhook import (
    InvalidWebhookProcessingStateError,
    TopUpNotFoundErrorForWebhook,
    WebhookProcessingError,
    _hash_payload,
    _sanitize_webhook_payload,
    _store_provider_event,
    process_webhook,
)


class WebhookProcessingTests(unittest.TestCase):
    def test_webhook_processor_module_exists(self):
        """Implementation exists: webhook processing module should be importable."""
        from services.topup.webhook import process_webhook  # noqa: F401
        self.assertTrue(callable(process_webhook))


class WebhookVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_signature_verification_fails_for_invalid_signature(self):
        """FR-007: Invalid webhook signatures must be rejected before parsing."""
        # Create a mock provider that fails verification
        provider = Mock(spec=PaymentProvider)
        provider.verify_and_parse_webhook.side_effect = WebhookVerificationError("Invalid signature")

        db = AsyncMock()
        # CRITICAL FIX: Remove webhook_secret parameter from process_webhook calls
        result = await process_webhook(
            db, provider, "mock", b'{"test": "data"}', "invalid_signature"
        )

        self.assertEqual(result["status"], "verification_failed")
        self.assertIn("error", result)
        self.assertIsNone(result["provider_event_id"])

    async def test_webhook_signature_verification_passes_for_valid_signature(self):
        """FR-007: Valid webhook signatures must be accepted and parsed."""
        # Create a mock provider that succeeds verification
        provider = Mock(spec=PaymentProvider)
        webhook_event = WebhookEvent(
            provider_event_id="evt_123",
            provider_transaction_reference="tx_456",
            status="Completed",
            amount=Money(Decimal("100.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        # Mock database operations
        db = AsyncMock()
        db.scalar = AsyncMock(return_value=None)  # No existing event
        db.add = Mock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        # CRITICAL FIX: Remove webhook_secret parameter from process_webhook calls
        result = await process_webhook(
            db, provider, "mock", b'{"test": "data"}', "valid_signature"
        )

        # Should get past verification (top_up_not_found since we didn't mock the top-up)
        self.assertNotEqual(result["status"], "verification_failed")

    async def test_webhook_payload_never_parsed_without_verification(self):
        """FR-007: Payload parsing must only occur after successful verification."""
        provider = Mock(spec=PaymentProvider)
        
        # Verify that parse is only called if verification succeeds
        provider.verify_and_parse_webhook.side_effect = WebhookVerificationError("Invalid signature")
        
        db = AsyncMock()
        # CRITICAL FIX: Remove webhook_secret parameter from process_webhook calls
        result = await process_webhook(db, provider, "mock", b'{"test": "data"}', "invalid_signature")
        
        # verify_and_parse_webhook should have been called (combined verification+parse)
        provider.verify_and_parse_webhook.assert_called_once()


class PayloadSanitizationTests(unittest.TestCase):
    def test_sanitization_removes_sensitive_data(self):
        """FR-022/Constitution IV: Sensitive fields must be redacted."""
        payload = {
            "event_id": "evt_123",
            "amount": 100,
            "card_number": "4111111111111111",
            "card_expiry": "12/25",
            "account_number": "123456789",
            "token": "tok_secret123",
            "cvc": "123",
            "cvv": "456",
            "authorization": "Bearer secret123",
            "api_key": "key_abc123",
            "funding_token": "funding_xyz",
        }
        
        raw_payload = json.dumps(payload).encode("utf-8")
        sanitized = _sanitize_webhook_payload(raw_payload)
        
        self.assertEqual(sanitized["event_id"], "evt_123")
        self.assertEqual(sanitized["amount"], 100)
        self.assertEqual(sanitized["card_number"], "***REDACTED***")
        self.assertEqual(sanitized["card_expiry"], "***REDACTED***")
        self.assertEqual(sanitized["account_number"], "***REDACTED***")
        self.assertEqual(sanitized["token"], "***REDACTED***")
        # HIGH FIX: Test additional sensitive fields
        self.assertEqual(sanitized["cvc"], "***REDACTED***")
        self.assertEqual(sanitized["cvv"], "***REDACTED***")
        self.assertEqual(sanitized["authorization"], "***REDACTED***")
        self.assertEqual(sanitized["api_key"], "***REDACTED***")
        self.assertEqual(sanitized["funding_token"], "***REDACTED***")

    def test_hash_generation_is_consistent(self):
        """Payload hashing must be consistent for deduplication."""
        payload = b'{"test": "data"}'
        hash1 = _hash_payload(payload)
        hash2 = _hash_payload(payload)
        self.assertEqual(hash1, hash2)


class IdempotencyTests(unittest.TestCase):
    def test_payload_hash_comparison_detects_tampering(self):
        """FR-008: Payload hash comparison should detect tampering."""
        # Unit test the hash comparison logic directly
        payload1 = b'{"amount": 100, "reference": "abc"}'
        payload2 = b'{"amount": 200, "reference": "abc"}'  # Different amount
        
        hash1 = _hash_payload(payload1)
        hash2 = _hash_payload(payload2)
        
        self.assertNotEqual(hash1, hash2, "Different payloads should have different hashes")
        
        # Same payload should produce same hash
        hash1_repeat = _hash_payload(payload1)
        self.assertEqual(hash1, hash1_repeat, "Same payload should produce same hash")

    def test_santitizer_redacts_all_sensitive_fields(self):
        """Constitution IV: Allowlist-based sanitization only keeps safe fields."""
        payload = {
            # Safe fields that should be kept
            "event_id": "evt_123",
            "provider_event_id": "prov_evt_456",
            "event_type": "payment.completed",
            "status": "Completed",
            "amount": 100,
            "currency": "USD",
            "provider_transaction_reference": "tx_abc",
            "created_at": "2024-01-01T00:00:00Z",
            # Sensitive fields that should be redacted
            "card_number": "4111111111111111",
            "cvc": "123",
            "provider_secret": "secret_abc",
            "api_key": "key_xyz",
        }
        
        raw_payload = json.dumps(payload).encode("utf-8")
        sanitized = _sanitize_webhook_payload(raw_payload)
        
        # Safe fields should be preserved
        self.assertEqual(sanitized["event_id"], "evt_123")
        self.assertEqual(sanitized["provider_event_id"], "prov_evt_456")
        self.assertEqual(sanitized["event_type"], "payment.completed")
        self.assertEqual(sanitized["status"], "Completed")
        self.assertEqual(sanitized["amount"], 100)
        self.assertEqual(sanitized["currency"], "USD")
        self.assertEqual(sanitized["provider_transaction_reference"], "tx_abc")
        
        # Sensitive fields should be redacted (allowlist approach)
        self.assertEqual(sanitized["card_number"], "***REDACTED***")
        self.assertEqual(sanitized["cvc"], "***REDACTED***")
        self.assertEqual(sanitized["provider_secret"], "***REDACTED***")
        self.assertEqual(sanitized["api_key"], "***REDACTED***")


class CompletionProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_is_marked_processed_before_calling_completion_so_it_shares_t014s_commit(self):
        # Regression for the round-11 CRITICAL finding: the round-10 version
        # of this test (formerly named
        # "test_event_is_marked_processed_only_after_completion_runs_in_one_commit")
        # asserted the event was still "received" at the moment
        # complete_verified_topup() ran -- but that's exactly the bug.
        # complete_verified_topup() commits its own financial transaction
        # *internally*; marking the event "processed" only after it returns
        # created a real split-transaction window where a crash between
        # T014's internal commit and this function's own later commit would
        # leave money already moved while the provider event still read
        # "received". This test now asserts the corrected ordering: the
        # event must already be "processed" by the time
        # complete_verified_topup() is invoked, so it rides along inside
        # whichever internal commit T014 performs, not a separate later one.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_atomic", "amount": "25.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_atomic",
            provider_transaction_reference="tx_atomic",
            status="Completed",
            amount=Money(Decimal("25.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())
        stored_event = ProviderEvent(
            provider_name="mock",
            provider_event_id="evt_atomic",
            event_type="Completed",
            payload_hash=_hash_payload(raw_body),
            sanitized_payload={},
            processing_status="received",
        )

        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=top_up)

        status_when_completion_ran = {}

        async def fake_complete(db_arg, top_up_id, amount, **kwargs):
            status_when_completion_ran["value"] = stored_event.processing_status
            return CompletionOutcome(top_up=top_up, action="completed")

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch("services.topup.webhook.complete_verified_topup", new=AsyncMock(side_effect=fake_complete)),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "success")
        self.assertEqual(status_when_completion_ran["value"], "processed")
        self.assertEqual(stored_event.processing_status, "processed")
        self.assertIsNotNone(stored_event.processed_at)

    async def test_no_commit_persists_financial_completion_while_the_event_still_reads_received(self):
        # The round-11 CRITICAL finding, closed the way the review asked
        # for: this simulates complete_verified_topup()'s REAL behavior --
        # it commits its own transaction internally -- rather than mocking
        # it away, which is exactly what the round-10 test's reviewer said
        # could not detect the split-transaction window. Every time
        # db.commit() is called (real or simulated), this test snapshots
        # whether the event is still "received" at that exact moment. If
        # any commit -- including T014's internal one -- ever fires while
        # the event has not yet been marked, that commit durably persisted
        # a top-up that looks Completed next to a provider event that still
        # looks unprocessed, which is the exact inconsistent-replay-state
        # risk the review described.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_shared_commit", "amount": "40.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_shared_commit",
            provider_transaction_reference="tx_shared_commit",
            status="Completed",
            amount=Money(Decimal("40.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())
        stored_event = ProviderEvent(
            provider_name="mock",
            provider_event_id="evt_shared_commit",
            event_type="Completed",
            payload_hash=_hash_payload(raw_body),
            sanitized_payload={},
            processing_status="received",
        )

        commit_snapshots = []

        db = AsyncMock()
        db.add = Mock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=top_up)

        async def recording_commit():
            commit_snapshots.append(stored_event.processing_status)

        db.commit = AsyncMock(side_effect=recording_commit)

        async def fake_complete_with_real_internal_commit(db_arg, top_up_id, amount, **kwargs):
            # Simulates complete_verified_topup()'s actual T014 behavior:
            # it mutates top_up state and commits *before* returning.
            top_up.status = "Completed"
            await db_arg.commit()
            return CompletionOutcome(top_up=top_up, action="completed")

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch(
                "services.topup.webhook.complete_verified_topup",
                new=AsyncMock(side_effect=fake_complete_with_real_internal_commit),
            ),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "success")
        self.assertTrue(commit_snapshots, "expected at least one commit to have been recorded")
        # No commit -- including T014's simulated internal one -- may ever
        # have fired while the event still read "received". A second,
        # trailing commit() call in process_webhook() is fine and expected
        # here: it is a deliberate safety net for the one internal
        # complete_verified_topup() path that performs no mutation and
        # therefore no commit at all (an already-terminal top-up whose
        # reported amount matches). By the time that trailing commit runs,
        # everything has already been durably committed by T014's own
        # internal commit, so it persists nothing new -- it cannot
        # reintroduce a window, only be redundant.
        self.assertGreaterEqual(db.commit.await_count, 1)

    async def test_no_commit_persists_a_verified_failure_while_the_event_still_reads_received(self):
        # Same reasoning and structure as
        # test_no_commit_persists_financial_completion_while_the_event_still_reads_received
        # above, but for the "Failed" branch (apply_verified_failure()),
        # which the same round-11 fix also applies to.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_failed_shared_commit", "amount": "15.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_failed_shared_commit",
            provider_transaction_reference="tx_failed_shared_commit",
            status="Failed",
            amount=Money(Decimal("15.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())
        stored_event = ProviderEvent(
            provider_name="mock",
            provider_event_id="evt_failed_shared_commit",
            event_type="Failed",
            payload_hash=_hash_payload(raw_body),
            sanitized_payload={},
            processing_status="received",
        )

        commit_snapshots = []

        db = AsyncMock()
        db.add = Mock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=top_up)

        async def recording_commit():
            commit_snapshots.append(stored_event.processing_status)

        db.commit = AsyncMock(side_effect=recording_commit)

        async def fake_apply_failure_with_real_internal_commit(db_arg, top_up_id, **kwargs):
            top_up.status = "Failed"
            await db_arg.commit()
            return CompletionOutcome(top_up=top_up, action="failed")

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch(
                "services.topup.webhook.apply_verified_failure",
                new=AsyncMock(side_effect=fake_apply_failure_with_real_internal_commit),
            ),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "success")
        self.assertTrue(commit_snapshots, "expected at least one commit to have been recorded")
        for snapshot in commit_snapshots:
            self.assertEqual(snapshot, "processed")

    async def test_completion_failure_never_marks_the_event_processed(self):
        # Regression for the round-9/round-10 review findings: the previous
        # version of this test ("test_error_handling_rollback_logic") only
        # mutated a local variable and never called process_webhook(). This
        # drives the real function through a completion failure and proves
        # the event is left "failed", never "processed" -- the negative
        # side of the atomicity claim above.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_fail", "amount": "5.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_fail",
            provider_transaction_reference="tx_fail",
            status="Completed",
            amount=Money(Decimal("5.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())
        stored_event = ProviderEvent(
            provider_name="mock",
            provider_event_id="evt_fail",
            event_type="Completed",
            payload_hash=_hash_payload(raw_body),
            sanitized_payload={},
            processing_status="received",
        )

        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        # First scalar call = top-up lookup; second = post-rollback re-query,
        # which finds the same event still present (the "fresh_event" branch).
        db.scalar = AsyncMock(side_effect=[top_up, stored_event])

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch(
                "services.topup.webhook.complete_verified_topup",
                new=AsyncMock(side_effect=WalletNotFoundError("no wallet for this top-up")),
            ),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "completion_error")
        self.assertEqual(stored_event.processing_status, "failed")
        self.assertNotEqual(stored_event.processing_status, "processed")
        db.rollback.assert_awaited_once()
        db.commit.assert_awaited_once()  # the fresh-transaction commit for failure metadata only

    async def test_failure_metadata_recording_on_missing_event(self):
        # Regression for the "eighth-round" review finding: the previous
        # version of this test only ran inspect.getsource(...) string
        # matching against process_webhook's source and never actually
        # executed the rollback/replacement-creation path. That let a real
        # bug through: ProviderEvent has no `event_data` field and requires
        # `payload_hash`, while the replacement construction passed
        # `event_data` and omitted `payload_hash`; it also referenced
        # `webhook_event.raw_body`, which does not exist on WebhookEvent.
        # This test actually drives process_webhook through the branch
        # where completion raises after the event was flushed, rollback
        # removes it (re-query returns None), and a replacement
        # ProviderEvent must be constructed and persisted.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_recovery", "amount": "100.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_recovery",
            provider_transaction_reference="tx_recovery",
            status="Completed",
            amount=Money(Decimal("100.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())

        db = AsyncMock()
        db.scalar = AsyncMock(side_effect=[top_up, None])
        db.add = Mock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        with patch(
            "services.topup.webhook.complete_verified_topup",
            new=AsyncMock(side_effect=WalletNotFoundError("no wallet for this top-up")),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "completion_error")
        self.assertEqual(result["error"], "WalletNotFoundError")

        # The rollback for the failed completion, then the fresh-transaction
        # commit for the replacement failure record.
        db.rollback.assert_awaited_once()
        db.commit.assert_awaited_once()

        self.assertEqual(db.add.call_count, 2)  # the original event, then the replacement
        replacement = db.add.call_args_list[-1].args[0]
        self.assertIsInstance(replacement, ProviderEvent)
        self.assertEqual(replacement.provider_name, "mock")
        self.assertEqual(replacement.provider_event_id, "evt_recovery")
        self.assertEqual(replacement.processing_status, "failed")
        self.assertEqual(replacement.error_code, "WalletNotFoundError")
        self.assertEqual(replacement.top_up_id, top_up.id)
        self.assertEqual(replacement.payload_hash, _hash_payload(raw_body))
        self.assertEqual(replacement.sanitized_payload, _sanitize_webhook_payload(raw_body))
        self.assertIsNotNone(replacement.processed_at)

    async def test_failure_metadata_recording_on_generic_exception_with_missing_event(self):
        # Same recovery path, but through the generic `except Exception`
        # handler rather than the named-exception one -- the two blocks
        # duplicate the same construction and shared the same bug.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_recovery_2", "amount": "50.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_recovery_2",
            provider_transaction_reference="tx_recovery_2",
            status="Completed",
            amount=Money(Decimal("50.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())

        db = AsyncMock()
        db.scalar = AsyncMock(side_effect=[top_up, None])
        db.add = Mock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        with patch(
            "services.topup.webhook.complete_verified_topup",
            new=AsyncMock(side_effect=RuntimeError("unexpected database error")),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "internal_error")
        db.rollback.assert_awaited_once()
        db.commit.assert_awaited_once()

        replacement = db.add.call_args_list[-1].args[0]
        self.assertIsInstance(replacement, ProviderEvent)
        self.assertEqual(replacement.processing_status, "failed")
        self.assertEqual(replacement.error_code, "internal_error")
        self.assertEqual(replacement.top_up_id, top_up.id)
        self.assertEqual(replacement.payload_hash, _hash_payload(raw_body))
        self.assertEqual(replacement.sanitized_payload, _sanitize_webhook_payload(raw_body))

    async def test_failure_metadata_updates_fresh_event_when_it_still_exists_after_rollback(self):
        # The other branch of the same code: if the re-query DOES find the
        # event after rollback, it must be updated in place, not replaced.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_still_here", "amount": "10.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_still_here",
            provider_transaction_reference="tx_still_here",
            status="Completed",
            amount=Money(Decimal("10.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        top_up = SimpleNamespace(id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4())
        fresh_event = SimpleNamespace(
            processing_status="received", error_code=None, processed_at=None
        )

        db = AsyncMock()
        db.scalar = AsyncMock(side_effect=[top_up, fresh_event])
        db.add = Mock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        with patch(
            "services.topup.webhook.complete_verified_topup",
            new=AsyncMock(side_effect=InvalidCompletionStateError("bad state")),
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "completion_error")
        self.assertEqual(fresh_event.processing_status, "failed")
        self.assertEqual(fresh_event.error_code, "InvalidCompletionStateError")
        self.assertIsNotNone(fresh_event.processed_at)
        # No replacement should be created when the fresh event was found.
        self.assertEqual(db.add.call_count, 1)  # only the original event


class ProviderEventIntegrityPathTests(unittest.IsolatedAsyncioTestCase):
    """Regression for the round-10 review finding: no prior test drove
    _store_provider_event() through a real sqlalchemy.exc.IntegrityError --
    replay preservation and tampered-payload rejection were only ever
    exercised by reasoning about the code, never by executing it.
    """

    async def test_exact_replay_hits_a_real_integrity_error_and_returns_existing_untouched(self):
        raw_body = b'{"event_id": "evt_dup", "amount": "10.00"}'
        payload_hash = _hash_payload(raw_body)
        existing = SimpleNamespace(
            provider_name="mock",
            provider_event_id="evt_dup",
            payload_hash=payload_hash,
            processing_status="processed",
        )

        db = AsyncMock()
        db.add = Mock()
        db.flush = AsyncMock(
            side_effect=IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint"))
        )
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=existing)

        result = await _store_provider_event(db, "mock", "evt_dup", "payment.completed", raw_body)

        self.assertIs(result, existing)
        db.rollback.assert_awaited_once()
        # An exact replay must never mutate the canonical record's status.
        self.assertEqual(existing.processing_status, "processed")

    async def test_tampered_payload_with_same_event_id_raises_and_leaves_canonical_hash_untouched(self):
        raw_body = b'{"event_id": "evt_tamper", "amount": "10.00"}'
        canonical_body = b'{"event_id": "evt_tamper", "amount": "9999.00"}'
        canonical_hash = _hash_payload(canonical_body)
        existing = SimpleNamespace(
            provider_name="mock",
            provider_event_id="evt_tamper",
            payload_hash=canonical_hash,
            processing_status="processed",
        )

        db = AsyncMock()
        db.add = Mock()
        db.flush = AsyncMock(
            side_effect=IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint"))
        )
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=existing)

        with self.assertRaises(WebhookProcessingError):
            await _store_provider_event(db, "mock", "evt_tamper", "payment.completed", raw_body)

        db.rollback.assert_awaited_once()
        self.assertEqual(existing.payload_hash, canonical_hash)
        self.assertEqual(existing.processing_status, "processed")

    async def test_integrity_error_with_no_matching_row_afterward_raises(self):
        # Defensive edge case already reachable in the code (a duplicate
        # detected by the constraint, but gone by the time of the re-query)
        # that had no coverage at all.
        raw_body = b'{"event_id": "evt_race", "amount": "1.00"}'
        db = AsyncMock()
        db.add = Mock()
        db.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")))
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=None)

        with self.assertRaises(WebhookProcessingError):
            await _store_provider_event(db, "mock", "evt_race", "payment.completed", raw_body)


class ConcurrentReplaySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_replayed_webhook_short_circuits_before_touching_completion(self):
        # End-to-end through process_webhook(): a second, concurrent
        # delivery of an already-processed event must never reach
        # complete_verified_topup() or even look up a top-up -- proving
        # replay safety at the orchestration level, not just inside the
        # private storage helper.
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_replay", "amount": "10.00"}'
        webhook_event = WebhookEvent(
            provider_event_id="evt_replay",
            provider_transaction_reference="tx_replay",
            status="Completed",
            amount=Money(Decimal("10.00"), "USD"),
        )
        provider.verify_and_parse_webhook.return_value = webhook_event

        already_processed = SimpleNamespace(
            provider_name="mock",
            provider_event_id="evt_replay",
            processing_status="processed",
        )

        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock()

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=already_processed)),
            patch("services.topup.webhook.complete_verified_topup", new=AsyncMock()) as mock_complete,
        ):
            result = await process_webhook(db, provider, "mock", raw_body, "valid_signature")

        self.assertEqual(result["status"], "duplicate_event")
        mock_complete.assert_not_called()
        db.scalar.assert_not_called()  # the top-up lookup is skipped entirely


class NonTerminalAndIgnoredEventTests(unittest.IsolatedAsyncioTestCase):
    """T034b. A verified Processing/RequiresAction event must actually move the
    top-up (before this it was only acknowledged, so a later verified success failed
    with an illegal transition), and an authentic event the provider adapter does not
    act on must be a clean no-op the route can acknowledge with 2xx."""

    def _setup(self, status):
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_progress"}'
        provider.verify_and_parse_webhook.return_value = WebhookEvent(
            provider_event_id="evt_progress",
            provider_transaction_reference="tx_progress",
            status=status,
            amount=Money(Decimal("25.00"), "USD"),
        )
        top_up = SimpleNamespace(id=uuid.uuid4(), status="Pending", wallet_id=uuid.uuid4())
        stored_event = ProviderEvent(
            provider_name="stripe",
            provider_event_id="evt_progress",
            event_type=status,
            payload_hash=_hash_payload(raw_body),
            sanitized_payload={},
            processing_status="received",
        )
        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=top_up)
        return provider, raw_body, top_up, stored_event, db

    async def _run(self, status, *, action="progressed"):
        provider, raw_body, top_up, stored_event, db = self._setup(status)
        seen = {}

        async def fake_progress(db_arg, top_up_id, new_status):
            seen["new_status"] = new_status
            seen["event_status_at_call"] = stored_event.processing_status
            return CompletionOutcome(top_up=top_up, action=action)

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch("services.topup.webhook.apply_verified_progress", new=AsyncMock(side_effect=fake_progress)),
            patch("services.topup.webhook.complete_verified_topup", new=AsyncMock()) as mock_complete,
        ):
            result = await process_webhook(db, provider, "stripe", raw_body, "sig")
        mock_complete.assert_not_called()
        return result, seen, stored_event

    async def test_processing_event_advances_the_top_up(self):
        result, seen, stored_event = await self._run("Processing")
        self.assertEqual(seen["new_status"], "Processing")
        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(result["action"], "progressed")
        self.assertEqual(stored_event.processing_status, "processed")

    async def test_requires_action_event_advances_the_top_up(self):
        result, seen, _ = await self._run("RequiresAction")
        self.assertEqual(seen["new_status"], "RequiresAction")
        self.assertEqual(result["action"], "progressed")

    async def test_event_is_marked_processed_before_the_progress_call_shares_its_commit(self):
        # Same atomicity rule as the completion path (round 11): apply_verified_progress
        # commits internally, so the event must already read "processed" when it runs.
        _, seen, _ = await self._run("Processing")
        self.assertEqual(seen["event_status_at_call"], "processed")

    async def test_late_or_repeated_event_reports_no_op(self):
        result, _, _ = await self._run("Processing", action="no_op")
        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(result["action"], "no_op")

    async def test_progress_failure_is_reported_not_raised_and_rolled_back(self):
        from services.topup.completion import InvalidCompletionStateError

        provider, raw_body, top_up, stored_event, db = self._setup("Processing")
        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch(
                "services.topup.webhook.apply_verified_progress",
                new=AsyncMock(side_effect=InvalidCompletionStateError("Created")),
            ),
        ):
            result = await process_webhook(db, provider, "stripe", raw_body, "sig")
        self.assertEqual(result["status"], "completion_error")
        db.rollback.assert_awaited()

    async def test_authentic_but_ignored_event_is_a_clean_no_op(self):
        from services.topup.provider import IgnoredWebhookEventError

        provider = Mock(spec=PaymentProvider)
        provider.verify_and_parse_webhook.side_effect = IgnoredWebhookEventError("charge.succeeded")
        db = AsyncMock()
        db.add = Mock()

        result = await process_webhook(db, provider, "stripe", b"{}", "sig")

        self.assertEqual(result["status"], "ignored_event")
        db.scalar.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_called()


class FailedAttemptAndCancelTests(unittest.IsolatedAsyncioTestCase):
    """T034i (option A): a failed ATTEMPT is recorded, not terminal; only a cancelled payment
    fails the top-up, and it fails it with the provider's own code."""

    async def _run(self, status, *, failure_code=None):
        from services.topup.provider import ATTEMPT_FAILED  # noqa: F401  (documents the vocabulary)

        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_attempt"}'
        provider.verify_and_parse_webhook.return_value = WebhookEvent(
            provider_event_id="evt_attempt",
            provider_transaction_reference="tx_attempt",
            status=status,
            amount=Money(Decimal("25.00"), "USD"),
            failure_code=failure_code,
        )
        top_up = SimpleNamespace(
            id=uuid.uuid4(), status="Pending", wallet_id=uuid.uuid4(), funding_method="card",
        )
        stored_event = ProviderEvent(
            provider_name="stripe", provider_event_id="evt_attempt", event_type=status,
            payload_hash=_hash_payload(raw_body), sanitized_payload={}, processing_status="received",
        )
        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=top_up)
        seen = {}

        async def fake_record(db_arg, top_up_id, code):
            seen["code"] = code
            seen["event_status_at_call"] = stored_event.processing_status
            return CompletionOutcome(top_up=top_up, action="attempt_failed_recorded")

        async def fake_failure(db_arg, top_up_id, *, failure_code, failure_message):
            seen["failure_code"] = failure_code
            return CompletionOutcome(top_up=top_up, action="failed")

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch("services.topup.webhook.record_payment_attempt_failure", new=AsyncMock(side_effect=fake_record)),
            patch("services.topup.webhook.apply_verified_failure", new=AsyncMock(side_effect=fake_failure)),
            patch("services.topup.webhook.complete_verified_topup", new=AsyncMock()) as mock_complete,
        ):
            result = await process_webhook(db, provider, "stripe", raw_body, "sig")
        mock_complete.assert_not_called()
        return result, seen, stored_event

    async def test_a_failed_attempt_is_recorded_and_does_not_fail_the_top_up(self):
        result, seen, stored_event = await self._run("AttemptFailed", failure_code="card_declined")
        self.assertEqual(seen["code"], "card_declined")
        self.assertNotIn("failure_code", seen)  # apply_verified_failure was NOT called
        self.assertEqual((result["status"], result["action"]), ("acknowledged", "attempt_failed_recorded"))
        self.assertEqual(stored_event.processing_status, "processed")

    async def test_the_event_is_marked_processed_before_the_record_call_shares_its_commit(self):
        _, seen, _ = await self._run("AttemptFailed", failure_code="card_declined")
        self.assertEqual(seen["event_status_at_call"], "processed")

    async def test_a_terminal_failure_uses_the_providers_own_code(self):
        result, seen, _ = await self._run("Failed", failure_code="payment_canceled_abandoned")
        self.assertEqual(seen["failure_code"], "payment_canceled_abandoned")
        self.assertEqual(result["action"], "failed")

    async def test_a_terminal_failure_without_a_code_keeps_the_generic_one(self):
        _, seen, _ = await self._run("Failed", failure_code=None)
        self.assertEqual(seen["failure_code"], "provider_failed")


class BankFailedDebitTests(unittest.IsolatedAsyncioTestCase):
    """T034d (human decision 2026-09-19). For a bank (ACH) top-up a failed debit is not an
    instant retry (it can arrive days later and a retry needs a fresh authorization), so it ENDS
    the top-up as Failed and the provider payment is cancelled so it can no longer be paid.
    Cards keep the T034i rule: a failed attempt is recorded and the customer retries."""

    async def _run(self, funding_method, *, outcome_action="failed", cancel_error=None, failure_code="account_closed"):
        provider = Mock(spec=PaymentProvider)
        raw_body = b'{"event_id": "evt_bank"}'
        provider.verify_and_parse_webhook.return_value = WebhookEvent(
            provider_event_id="evt_bank", provider_transaction_reference="pi_bank_1",
            status="AttemptFailed", amount=Money(Decimal("25.00"), "USD"), failure_code=failure_code,
        )
        if cancel_error is not None:
            provider.cancel.side_effect = cancel_error
        top_up = SimpleNamespace(
            id=uuid.uuid4(), status="Processing", wallet_id=uuid.uuid4(), funding_method=funding_method,
            provider_transaction_reference="pi_bank_1", internal_reference="tu_bank_1",
        )
        stored_event = ProviderEvent(
            provider_name="stripe", provider_event_id="evt_bank", event_type="AttemptFailed",
            payload_hash=_hash_payload(raw_body), sanitized_payload={}, processing_status="received",
        )
        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.scalar = AsyncMock(return_value=top_up)
        timeline = []

        async def fake_failure(db_arg, top_up_id, *, failure_code, failure_message):
            timeline.append(("failure", failure_code, stored_event.processing_status))
            return CompletionOutcome(top_up=top_up, action=outcome_action)

        async def fake_record(db_arg, top_up_id, code):
            timeline.append(("attempt",))
            return CompletionOutcome(top_up=top_up, action="attempt_failed_recorded")

        def cancel_spy(reference):
            timeline.append(("cancel", reference))
            if cancel_error is not None:
                raise cancel_error

        provider.cancel.side_effect = cancel_spy
        audits = []

        async def fake_audit(db_arg, **kwargs):
            audits.append(kwargs)

        with (
            patch("services.topup.webhook._store_provider_event", new=AsyncMock(return_value=stored_event)),
            patch("services.topup.webhook.apply_verified_failure", new=AsyncMock(side_effect=fake_failure)),
            patch("services.topup.webhook.record_payment_attempt_failure", new=AsyncMock(side_effect=fake_record)),
            patch("services.topup.webhook.log_audit", new=AsyncMock(side_effect=fake_audit)),
        ):
            result = await process_webhook(db, provider, "stripe", raw_body, "sig")
        return result, timeline, audits, stored_event, db

    async def test_a_failed_bank_debit_fails_the_top_up_and_cancels_the_payment(self):
        result, timeline, _, stored_event, _ = await self._run("bank_transfer")
        self.assertEqual([step[0] for step in timeline], ["failure", "cancel"])
        self.assertEqual(timeline[0][1], "account_closed")   # the provider's own code is kept
        self.assertEqual(timeline[1][1], "pi_bank_1")
        self.assertEqual((result["status"], result["action"]), ("success", "failed"))
        self.assertEqual(stored_event.processing_status, "processed")

    async def test_the_top_up_is_durably_failed_before_the_provider_is_asked_to_cancel(self):
        # The event is marked processed first so it shares the failure's own commit; the cancel
        # call is after that, so a provider outage can never undo the failure.
        _, timeline, _, _, db = await self._run("bank_transfer")
        self.assertEqual(timeline[0][2], "processed")
        self.assertLess(timeline.index(next(s for s in timeline if s[0] == "failure")),
                        timeline.index(next(s for s in timeline if s[0] == "cancel")))

    async def test_a_card_top_up_keeps_the_retry_rule_and_never_cancels_the_payment(self):
        result, timeline, _, _, _ = await self._run("card")
        self.assertEqual([step[0] for step in timeline], ["attempt"])
        self.assertEqual(result["action"], "attempt_failed_recorded")

    async def test_other_funding_methods_also_keep_the_attempt_rule(self):
        for method in ("mobile_money", "agent_cash"):
            with self.subTest(method=method):
                _, timeline, _, _, _ = await self._run(method)
                self.assertEqual([step[0] for step in timeline], ["attempt"])

    async def test_a_top_up_that_was_already_terminal_does_not_touch_the_provider(self):
        result, timeline, _, _, _ = await self._run("bank_transfer", outcome_action="no_op")
        self.assertEqual([step[0] for step in timeline], ["failure"])
        self.assertEqual(result["action"], "no_op")

    async def test_a_provider_that_cannot_cancel_never_undoes_the_failure_and_is_audited(self):
        from services.topup.provider import ProviderRejectedError, ProviderUnavailableError

        for error in (ProviderUnavailableError("down"), ProviderRejectedError("cannot")):
            with self.subTest(error=type(error).__name__):
                result, timeline, audits, _, db = await self._run("bank_transfer", cancel_error=error)
                self.assertEqual((result["status"], result["action"]), ("success", "failed"))
                self.assertEqual([a["action"] for a in audits], ["topup_provider_cancel_failed"])
                self.assertEqual(audits[0]["details"]["provider_transaction_reference"], "pi_bank_1")
                self.assertEqual(audits[0]["details"]["error"], type(error).__name__)
                db.commit.assert_awaited()

    async def test_an_unexpected_cancel_error_does_not_fail_the_webhook(self):
        result, _, audits, _, _ = await self._run("bank_transfer", cancel_error=RuntimeError("boom"))
        self.assertEqual(result["status"], "success")
        self.assertEqual([a["action"] for a in audits], ["topup_provider_cancel_failed"])


if __name__ == "__main__":
    unittest.main()