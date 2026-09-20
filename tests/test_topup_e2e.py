"""End-to-end customer top-up tests (T017; FR-007, FR-008, FR-009).

Test-first per Constitution VI: these tests verify the complete customer top-up flow
from initiation through webhook completion and ensure failure paths have no financial effects.

Now with genuine PostgreSQL integration using the infrastructure from T010.
"""
import json
import pytest
import unittest
import uuid
from decimal import Decimal
from datetime import datetime

from fastapi import Response
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

# Database URL from environment
import os
from dotenv import load_dotenv
# Relative to this file's own location, not a machine-specific absolute
# path -- the round-1 version hardcoded `C:\projects\repos\wallet-backend\.env`,
# which would break in CI or on any other checkout path.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

if not os.environ.get('DATABASE_URL'):
    pytest.skip("DATABASE_URL environment variable must be set for E2E tests", allow_module_level=True)

# Use synchronous SQLAlchemy for E2E testing
SYNC_DATABASE_URL = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')

# Import the models
from models.topup import LedgerEntryRecord, LedgerTransactionRecord, ProviderEvent, TopUp
from models.user import User
from models.wallet import Wallet
from services.topup.money import Money
from services.topup.mock_provider import MockPaymentProvider
from services.topup.provider import InitiationRequest
from services.topup.webhook import process_webhook


@pytest.fixture(scope="function")
def db_session():
    """Fully isolated test session, regardless of what the test does.

    The round-1 version of this fixture only called session.rollback() at
    teardown, which is a no-op for any test that already called
    session.commit() itself -- several tests in this file do exactly that
    (and only clean up the top_up/provider_event rows manually, never the
    user/wallet rows created before the first commit), so every run left
    permanent rows behind. This is the exact same bug T010's own
    db_session fixture had; fixed the same way here: open the outer
    transaction on the connection ourselves and bind the session to it
    with join_transaction_mode="create_savepoint" (SQLAlchemy 2.0's
    standard nested-transaction test-isolation pattern), so a test's own
    commit() only releases an inner SAVEPOINT and the outer transaction --
    everything the test did, however many times it "committed" -- is
    always rolled back here.
    """
    engine = create_engine(SYNC_DATABASE_URL)
    connection = engine.connect()
    outer_transaction = connection.begin()
    Session = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    session = Session()
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


class TestCustomerTopUpE2E:
    """End-to-end tests for the complete customer top-up flow with PostgreSQL integration."""
    
    def create_test_user_and_wallet(self, db_session):
        """Helper method to create a test user and wallet."""
        test_user = User(
            phone_number=f"+1555{uuid.uuid4().hex[:10]}",
            email=f"test_{uuid.uuid4().hex[:12]}@example.com",
        )
        db_session.add(test_user)
        db_session.flush()
        
        test_wallet = Wallet(
            user_id=test_user.id,
            currency="USD",
            status="active",
            balance=Decimal("100.00"),
            daily_limit=Decimal("1000.00"),
            monthly_limit=Decimal("5000.00"),
            daily_spent=Decimal("0.00"),
            monthly_spent=Decimal("0.00"),
        )
        db_session.add(test_wallet)
        db_session.flush()
        
        return test_user, test_wallet
    
    def test_complete_topup_persistence_flow(self, db_session):
        """FR-007, FR-008, FR-009: Verify complete top-up persistence flow with actual database operations.
        
        This test verifies that the complete persistence flow works:
        1. User and wallet can be created
        2. Top-up can be created with proper constraints
        3. Provider events can be stored with uniqueness
        4. Ledger operations can be performed
        """
        test_user, test_wallet = self.create_test_user_and_wallet(db_session)
        
        # Step 1: Create a top-up (simulating initiation)
        top_up = TopUp(
            internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
            wallet_id=test_wallet.id,
            agent_id=None,
            idempotency_key=f"test_idemp_{uuid.uuid4().hex[:12]}",
            request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
            funding_method="card",
            provider_name="mock",
            provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
            gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"),
            net_amount=Decimal("98.50"),
            currency="USD",
            status="Pending",
            version_id=1,
        )
        db_session.add(top_up)
        db_session.commit()
        db_session.refresh(top_up)
        
        # Step 2: Create provider event (simulating webhook reception)
        provider_event = ProviderEvent(
            provider_name="mock",
            provider_event_id=f"test_evt_{uuid.uuid4().hex[:12]}",
            top_up_id=top_up.id,
            event_type="payment.completed",
            payload_hash=f"hash_{uuid.uuid4().hex[:12]}",
            processing_status="received",
            error_code=None,
        )
        db_session.add(provider_event)
        db_session.commit()
        db_session.refresh(provider_event)
        
        # Verify the complete flow worked
        assert top_up.id is not None
        assert top_up.wallet_id == test_wallet.id
        assert provider_event.top_up_id == top_up.id
        assert provider_event.processing_status == "received"
        
        # Clean up for isolation
        db_session.delete(provider_event)
        db_session.delete(top_up)
        db_session.commit()
    
    def test_idempotency_prevents_duplicate_topups(self, db_session):
        """FR-008: Verify idempotency prevents duplicate top-ups with same idempotency key.
        
        This test verifies that the database constraints prevent duplicate top-ups
        with the same idempotency key for the same wallet.
        """
        test_user, test_wallet = self.create_test_user_and_wallet(db_session)
        
        idempotency_key = f"test_idemp_{uuid.uuid4().hex[:12]}"
        
        # Create first top-up
        top_up1 = TopUp(
            internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
            wallet_id=test_wallet.id,
            agent_id=None,
            idempotency_key=idempotency_key,
            request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
            funding_method="card",
            provider_name="mock",
            provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
            gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"),
            net_amount=Decimal("98.50"),
            currency="USD",
            status="Pending",
            version_id=1,
        )
        db_session.add(top_up1)
        db_session.commit()
        
        # Try to create duplicate top-up with same idempotency key for same wallet
        top_up2 = TopUp(
            internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
            wallet_id=test_wallet.id,
            agent_id=None,
            idempotency_key=idempotency_key,  # Same idempotency key
            request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
            funding_method="card",
            provider_name="mock",
            provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
            gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"),
            net_amount=Decimal("98.50"),
            currency="USD",
            status="Pending",
            version_id=1,
        )
        db_session.add(top_up2)
        
        # Should fail due to uniqueness constraint on (wallet_id, idempotency_key)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            db_session.commit()
        
        db_session.rollback()
        
        # Clean up
        db_session.delete(top_up1)
        db_session.commit()
    
    def test_provider_event_uniqueness_prevents_duplicate_webhooks(self, db_session):
        """FR-008: Verify provider event uniqueness prevents duplicate webhook processing.
        
        This test verifies that duplicate webhooks with the same provider event ID
        are rejected by the database constraints.
        """
        test_user, test_wallet = self.create_test_user_and_wallet(db_session)
        
        # Create a top-up first
        top_up = TopUp(
            internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
            wallet_id=test_wallet.id,
            agent_id=None,
            idempotency_key=f"test_idemp_{uuid.uuid4().hex[:12]}",
            request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
            funding_method="card",
            provider_name="mock",
            provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
            gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"),
            net_amount=Decimal("98.50"),
            currency="USD",
            status="Pending",
            version_id=1,
        )
        db_session.add(top_up)
        db_session.commit()
        
        provider_event_id = f"test_evt_{uuid.uuid4().hex[:12]}"
        
        # Create first provider event
        provider_event1 = ProviderEvent(
            provider_name="mock",
            provider_event_id=provider_event_id,
            top_up_id=top_up.id,
            event_type="payment.completed",
            payload_hash=f"hash1_{uuid.uuid4().hex[:12]}",
            processing_status="processed",
            error_code=None,
        )
        db_session.add(provider_event1)
        db_session.commit()
        
        # Try to create duplicate provider event with same provider_event_id
        provider_event2 = ProviderEvent(
            provider_name="mock",
            provider_event_id=provider_event_id,  # Same event ID
            top_up_id=top_up.id,
            event_type="payment.completed",
            payload_hash=f"hash2_{uuid.uuid4().hex[:12]}",  # Different payload
            processing_status="received",
            error_code=None,
        )
        db_session.add(provider_event2)
        
        # Should fail due to uniqueness constraint on (provider_name, provider_event_id)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            db_session.commit()
        
        db_session.rollback()
        
        # Clean up
        db_session.delete(provider_event1)
        db_session.delete(top_up)
        db_session.commit()
    
    def test_amount_validation_prevents_invalid_amounts(self, db_session):
        """FR-009: Verify amount validation prevents invalid financial amounts.
        
        This test verifies that database constraints prevent invalid amounts
        (negative, mismatched amounts, etc.) from being persisted.
        """
        test_user, test_wallet = self.create_test_user_and_wallet(db_session)
        
        # Try to create top-up with negative amount
        top_up_invalid = TopUp(
            internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
            wallet_id=test_wallet.id,
            agent_id=None,
            idempotency_key=f"test_idemp_{uuid.uuid4().hex[:12]}",
            request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
            funding_method="card",
            provider_name="mock",
            provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
            gross_amount=Decimal("-100.00"),  # Invalid: negative amount
            fee_amount=Decimal("1.50"),
            net_amount=Decimal("-98.50"),
            currency="USD",
            status="Pending",
            version_id=1,
        )
        db_session.add(top_up_invalid)
        
        # Should fail due to check constraint (gross_amount > 0)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            db_session.commit()
        
        db_session.rollback()
    
    def test_currency_format_validation_prevents_invalid_currency(self, db_session):
        """FR-009: Verify currency format validation prevents invalid currency codes.
        
        This test verifies that database constraints prevent invalid currency formats.
        """
        test_user, test_wallet = self.create_test_user_and_wallet(db_session)
        
        # Try to create top-up with invalid currency format
        top_up_invalid = TopUp(
            internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
            wallet_id=test_wallet.id,
            agent_id=None,
            idempotency_key=f"test_idemp_{uuid.uuid4().hex[:12]}",
            request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
            funding_method="card",
            provider_name="mock",
            provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
            gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"),
            net_amount=Decimal("98.50"),
            currency="abc",  # Invalid: lowercase (should be [A-Z]{3})
            status="Pending",
            version_id=1,
        )
        db_session.add(top_up_invalid)
        
        # Should fail due to check constraint (currency format)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            db_session.commit()
        
        db_session.rollback()
    
    def test_transaction_rollback_ensures_consistency(self, db_session):
        """FR-009: Verify transaction rollback ensures consistency on failures.
        
        This test verifies that database transactions roll back properly on failures,
        preventing partial state changes.
        """
        test_user, test_wallet = self.create_test_user_and_wallet(db_session)
        
        try:
            # Create a top-up
            top_up = TopUp(
                internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
                wallet_id=test_wallet.id,
                agent_id=None,
                idempotency_key=f"test_idemp_{uuid.uuid4().hex[:12]}",
                request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
                funding_method="card",
                provider_name="mock",
                provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
                gross_amount=Decimal("100.00"),
                fee_amount=Decimal("1.50"),
                net_amount=Decimal("98.50"),
                currency="USD",
                status="Pending",
                version_id=1,
            )
            db_session.add(top_up)
            db_session.flush()
            
            # Simulate a failure by trying to create invalid data
            invalid_top_up = TopUp(
                internal_reference=f"test_tu_{uuid.uuid4().hex[:12]}",
                wallet_id=test_wallet.id,
                agent_id=None,
                idempotency_key=f"test_idemp_{uuid.uuid4().hex[:12]}",
                request_fingerprint=f"test_fp_{uuid.uuid4().hex[:12]}",
                funding_method="card",
                provider_name="mock",
                provider_transaction_reference=f"test_tx_{uuid.uuid4().hex[:12]}",
                gross_amount=Decimal("-100.00"),  # Invalid
                fee_amount=Decimal("1.50"),
                net_amount=Decimal("-98.50"),
                currency="USD",
                status="Pending",
                version_id=1,
            )
            db_session.add(invalid_top_up)
            db_session.flush()
            
            # Commit should fail
            db_session.commit()
            assert False, "Should have failed due to invalid amount"
            
        except Exception:
            # Rollback should clean up everything
            db_session.rollback()
        
        # Verify the top_up was not persisted due to rollback
        result = db_session.execute(
            select(TopUp).where(TopUp.internal_reference == top_up.internal_reference)
        )
        assert result.scalar_one_or_none() is None, "Top-up should not exist after rollback"


class RealServiceEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Genuine end-to-end coverage through the REAL service layer (T012
    provider, T013 endpoint, T014 completion, T015 webhook) against real
    PostgreSQL. TestCustomerTopUpE2E above (round 1) only exercises the
    persistence layer directly and duplicates most of T010's own coverage;
    its own "Known limitations" section says as much: "Tests verify
    database-level persistence and constraint enforcement but do not
    integrate with the async service layer (webhook processor, completion
    service, HTTP endpoints)." That is exactly what T017 asks for --
    "verify failure paths have no financial effects" only means something
    once money can actually move through the real pipeline. These tests
    close that gap.

    Async flavor of T010's join_transaction_mode="create_savepoint" fixture
    fix: every commit the real handler/service code performs (T013's
    initiate_top_up, T014's complete_verified_topup/apply_verified_failure,
    T015's process_webhook all call db.commit() internally) only releases
    an inner SAVEPOINT here; the outer transaction is always rolled back in
    asyncTearDown, so these tests leave zero permanent rows regardless of
    how many times the real code under test commits.

    IMPORTANT FINDING, not a defect in this task's own tests: no production
    code anywhere in this codebase currently transitions a top-up from
    Pending to Processing. Grepping the whole repo for `.initiate(` finds
    only test files and mock_provider.py itself -- handlers/topup.py (T013)
    deliberately stops at Pending (see its own "Requested reviewer" note:
    "correctly scoped to endpoint responsibilities without overreaching
    into T014/T015 domains"), and neither T014 nor T015 calls
    provider.initiate() either. complete_verified_topup() only accepts a
    top-up already in "Processing" (COMPLETABLE_FROM = {"Processing"}), so
    in the currently-implemented system a freshly initiated top-up can
    never actually reach a provider-verified completion at all -- something
    would need to call the provider and record its Processing transition,
    and nothing does. _advance_to_processing() below performs that missing
    step by hand, entirely inside the test, specifically so T014/T015's own
    already-implemented logic (this task's actual subject) can be exercised
    end-to-end; it is not a substitute for the missing production code, and
    this handoff does not add that code, since inventing new orchestration
    behavior is out of scope for a testing task (Constitution V). Flagging
    for the human owner / T028's traceability pass.
    """

    async def asyncSetUp(self):
        self.engine = create_async_engine(os.environ["DATABASE_URL"])

        # Real finding, not a defect in this test: on a database that was
        # only ever `alembic upgrade head`-ed and never had the actual app
        # started against it, `fee_rules` does not exist -- handlers/topup.py
        # queries it unconditionally, and there is no Alembic migration for
        # it anywhere in this project; it only ever gets created by
        # config/database.py's own startup fallback ("Auto-create any
        # missing tables (safe -- does not drop or alter existing ones)").
        # Mirroring that exact same safe, additive-only step here so these
        # tests work regardless of which of those two ways this local
        # database happened to be set up, rather than silently skipping or
        # working around the real gap.
        from models.base import Base
        import models.fee_rule  # noqa: F401
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.connection = await self.engine.connect()
        self.outer_transaction = await self.connection.begin()
        SessionLocal = async_sessionmaker(
            bind=self.connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        self.db = SessionLocal()

    async def asyncTearDown(self):
        await self.db.close()
        await self.outer_transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def _create_user_and_wallet(self, *, balance=Decimal("0.00")):
        user = User(
            phone_number=f"+1555{uuid.uuid4().hex[:10]}",
            email=f"test_{uuid.uuid4().hex[:12]}@example.com",
        )
        self.db.add(user)
        await self.db.flush()

        wallet = Wallet(
            user_id=user.id,
            currency="USD",
            status="active",
            balance=balance,
            daily_limit=Decimal("100000.00"),
            monthly_limit=Decimal("1000000.00"),
            daily_spent=Decimal("0.00"),
            monthly_spent=Decimal("0.00"),
        )
        self.db.add(wallet)
        await self.db.flush()
        return user, wallet

    async def _advance_to_processing(self, user, wallet, *, amount="100.00"):
        """Drives the REAL T013 handler to create a durable Pending top-up,
        then performs the provider-initiation step no production code
        currently performs (see the class docstring)."""
        from handlers.topup import initiate_top_up
        from handlers.topup_contracts import FundingMethod, InitiateTopUpRequest

        request = InitiateTopUpRequest(
            amount=Decimal(amount), currency="USD", funding_method=FundingMethod.CARD,
            funding_token="tok_e2e_test",
        )
        result = await initiate_top_up(
            wallet.id, request, Response(), f"e2e-{uuid.uuid4().hex[:16]}",
            {"sub": str(user.id)}, self.db,
        )
        top_up = await self.db.get(TopUp, result.top_up.id)

        provider = MockPaymentProvider()
        init_result = provider.initiate(
            InitiationRequest(
                internal_reference=top_up.internal_reference,
                wallet_id=str(wallet.id),
                gross_amount=Money(top_up.gross_amount, top_up.currency),
                funding_method=top_up.funding_method,
                idempotency_key=top_up.idempotency_key,
                funding_token="tok_e2e_test",
            )
        )
        top_up.status = "Processing"
        top_up.provider_name = "mock"
        top_up.provider_transaction_reference = init_result.provider_transaction_reference
        await self.db.flush()
        return provider, top_up

    def _signed_completed_webhook(self, provider, top_up, *, amount=None):
        body = json.dumps(
            {
                "event_id": f"evt_{uuid.uuid4().hex[:12]}",
                "transaction_id": top_up.provider_transaction_reference,
                "status": "Completed",
                "amount": str(amount if amount is not None else top_up.gross_amount),
                "currency": top_up.currency,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode()
        return body, provider.sign(body)

    async def test_happy_path_completes_and_credits_wallet_with_balanced_posted_ledger(self):
        user, wallet = await self._create_user_and_wallet(balance=Decimal("0.00"))
        provider, top_up = await self._advance_to_processing(user, wallet, amount="100.00")
        provider.mark_completed(top_up.provider_transaction_reference)
        body, signature = self._signed_completed_webhook(provider, top_up)

        result = await process_webhook(self.db, provider, "mock", body, signature)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "completed")

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(wallet.balance, top_up.net_amount)

        ledger_txn = (
            await self.db.execute(
                select(LedgerTransactionRecord).where(LedgerTransactionRecord.top_up_id == top_up.id)
            )
        ).scalars().first()
        self.assertIsNotNone(ledger_txn)
        self.assertTrue(ledger_txn.is_posted)

        entries = (
            await self.db.execute(
                select(LedgerEntryRecord).where(LedgerEntryRecord.ledger_transaction_id == ledger_txn.id)
            )
        ).scalars().all()
        debit_total = sum(e.amount for e in entries if e.direction == "debit")
        credit_total = sum(e.amount for e in entries if e.direction == "credit")
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, top_up.gross_amount)

    async def test_verified_failure_has_no_financial_effect(self):
        user, wallet = await self._create_user_and_wallet(balance=Decimal("50.00"))
        provider, top_up = await self._advance_to_processing(user, wallet, amount="100.00")

        body = json.dumps(
            {
                "event_id": f"evt_{uuid.uuid4().hex[:12]}",
                "transaction_id": top_up.provider_transaction_reference,
                "status": "Failed",
                "amount": str(top_up.gross_amount),
                "currency": top_up.currency,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode()
        signature = provider.sign(body)

        result = await process_webhook(self.db, provider, "mock", body, signature)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "failed")

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual(top_up.status, "Failed")
        self.assertEqual(wallet.balance, Decimal("50.00"))  # completely untouched

        ledger_txn = (
            await self.db.execute(
                select(LedgerTransactionRecord).where(LedgerTransactionRecord.top_up_id == top_up.id)
            )
        ).scalars().first()
        self.assertIsNone(ledger_txn)

    async def test_provider_amount_mismatch_routes_to_under_review_with_no_financial_effect(self):
        user, wallet = await self._create_user_and_wallet(balance=Decimal("0.00"))
        provider, top_up = await self._advance_to_processing(user, wallet, amount="100.00")
        provider.mark_completed(top_up.provider_transaction_reference)
        # Provider reports a DIFFERENT amount than the top-up's own stored gross_amount.
        body, signature = self._signed_completed_webhook(provider, top_up, amount=Decimal("999.00"))

        result = await process_webhook(self.db, provider, "mock", body, signature)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "under_review")

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual(top_up.status, "UnderReview")
        self.assertEqual(wallet.balance, Decimal("0.00"))  # completely untouched

        ledger_txn = (
            await self.db.execute(
                select(LedgerTransactionRecord).where(LedgerTransactionRecord.top_up_id == top_up.id)
            )
        ).scalars().first()
        self.assertIsNone(ledger_txn)

    async def test_duplicate_webhook_delivery_credits_wallet_exactly_once(self):
        user, wallet = await self._create_user_and_wallet(balance=Decimal("0.00"))
        provider, top_up = await self._advance_to_processing(user, wallet, amount="100.00")
        provider.mark_completed(top_up.provider_transaction_reference)
        body, signature = self._signed_completed_webhook(provider, top_up)

        first = await process_webhook(self.db, provider, "mock", body, signature)
        second = await process_webhook(self.db, provider, "mock", body, signature)

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "duplicate_event")

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(wallet.balance, top_up.net_amount)  # credited exactly once, not twice

        ledger_txns = (
            await self.db.execute(
                select(LedgerTransactionRecord).where(LedgerTransactionRecord.top_up_id == top_up.id)
            )
        ).scalars().all()
        self.assertEqual(len(ledger_txns), 1)  # exactly one ledger transaction posted, not two

    # ------------------------------------------------------------------ Stripe (T034b)
    #
    # These do NOT use _advance_to_processing(): the top-up stays in the state the
    # real T013 handler leaves it (Pending), which is exactly what a real provider
    # event meets. Before T034b a verified success for a Pending top-up failed with an
    # illegal transition and the customer was never credited. Events are signed with
    # this file's own HMAC per Stripe's documented scheme; the provider under test is
    # the real StripePaymentProvider and the processor is the real process_webhook.

    STRIPE_TEST_SECRET = "whsec_test_endpoint_secret_not_a_real_credential"

    async def _pending_stripe_top_up(self, *, amount="100.00", funding_method="card"):
        """The REAL T013/T034c handler creates the top-up and its provider payment.

        Only the Stripe network client is faked (no network from tests); the handler,
        the database, the provider adapter and everything after it are real.
        """
        from types import SimpleNamespace
        from unittest.mock import patch

        from handlers.topup import initiate_top_up
        from handlers.topup_contracts import FundingMethod, InitiateTopUpRequest
        from services.topup.stripe_provider import StripePaymentProvider

        created = []

        class _Intents:
            def create(self_inner, params, options=None):
                created.append((params, options))
                return SimpleNamespace(
                    id=f"pi_e2e_{uuid.uuid4().hex[:16]}", client_secret="cs_e2e_secret",
                    status="requires_payment_method",
                )

        provider = StripePaymentProvider(
            webhook_secret=self.STRIPE_TEST_SECRET,
            client=SimpleNamespace(v1=SimpleNamespace(payment_intents=_Intents())),
        )

        user, wallet = await self._create_user_and_wallet(balance=Decimal("0.00"))
        request = InitiateTopUpRequest(
            amount=Decimal(amount), currency="USD", funding_method=FundingMethod(funding_method),
            funding_token="tok_e2e_test",
        )
        with patch("handlers.topup._initiation_provider", return_value=provider):
            result = await initiate_top_up(
                wallet.id, request, Response(), f"e2e-{uuid.uuid4().hex[:16]}",
                {"sub": str(user.id)}, self.db,
            )

        top_up = await self.db.get(TopUp, result.top_up.id)
        # Initiation, not the test, recorded the provider's id, and still credited nothing.
        self.assertEqual(top_up.status, "Pending")
        self.assertEqual(top_up.provider_name, "stripe")
        self.assertTrue(top_up.provider_transaction_reference.startswith("pi_e2e_"))
        self.assertEqual(result.top_up.next_action.type, "confirm_with_provider")
        self.assertEqual(result.top_up.next_action.client_secret, "cs_e2e_secret")
        (params, options), = created
        self.assertEqual(params["amount"], int(Decimal(amount) * 100))
        self.assertEqual(options["idempotency_key"], top_up.internal_reference)
        await self.db.refresh(wallet)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        return wallet, top_up

    def _stripe_event(self, top_up, event_type, *, received_cents=None, extra=None):
        import hashlib
        import hmac
        import time

        cents = int(top_up.gross_amount * 100)
        body = json.dumps({
            "id": f"evt_e2e_{uuid.uuid4().hex[:12]}", "object": "event", "type": event_type,
            "data": {"object": {
                "id": top_up.provider_transaction_reference, "object": "payment_intent",
                "amount": cents,
                "amount_received": cents if received_cents is None else received_cents,
                "currency": "usd",
                **(extra or {}),
            }},
        }).encode()
        ts = int(time.time())
        digest = hmac.new(self.STRIPE_TEST_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return body, f"t={ts},v1={digest}"

    def _stripe_provider(self):
        from services.topup.stripe_provider import StripePaymentProvider

        return StripePaymentProvider(webhook_secret=self.STRIPE_TEST_SECRET)

    async def _ledger_totals(self, top_up):
        txns = (
            await self.db.execute(
                select(LedgerTransactionRecord).where(LedgerTransactionRecord.top_up_id == top_up.id)
            )
        ).scalars().all()
        entries = []
        for txn in txns:
            entries += (
                await self.db.execute(
                    select(LedgerEntryRecord).where(LedgerEntryRecord.ledger_transaction_id == txn.id)
                )
            ).scalars().all()
        debit = sum(e.amount for e in entries if e.direction == "debit")
        credit = sum(e.amount for e in entries if e.direction == "credit")
        return len(txns), debit, credit, [t.is_posted for t in txns]

    async def test_stripe_success_credits_a_pending_top_up_exactly_once(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider = self._stripe_provider()
        body, header = self._stripe_event(top_up, "payment_intent.succeeded")

        first = await process_webhook(self.db, provider, "stripe", body, header)
        replay = await process_webhook(self.db, provider, "stripe", body, header)

        self.assertEqual((first["status"], first["action"]), ("success", "completed"))
        self.assertEqual(replay["status"], "duplicate_event")
        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(wallet.balance, top_up.net_amount)  # credited once, not twice
        count, debit, credit, posted = await self._ledger_totals(top_up)
        self.assertEqual((count, posted), (1, [True]))
        self.assertEqual(debit, credit)
        self.assertEqual(debit, top_up.gross_amount)

    async def test_stripe_processing_then_success_in_order(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider = self._stripe_provider()

        body, header = self._stripe_event(top_up, "payment_intent.processing")
        progressed = await process_webhook(self.db, provider, "stripe", body, header)
        await self.db.refresh(top_up)
        self.assertEqual((progressed["action"], top_up.status), ("progressed", "Processing"))

        body, header = self._stripe_event(top_up, "payment_intent.succeeded")
        done = await process_webhook(self.db, provider, "stripe", body, header)
        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((done["action"], top_up.status), ("completed", "Completed"))
        self.assertEqual(wallet.balance, top_up.net_amount)

    async def test_stripe_success_arriving_before_processing_is_not_regressed_by_the_late_event(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider = self._stripe_provider()

        body, header = self._stripe_event(top_up, "payment_intent.succeeded")
        await process_webhook(self.db, provider, "stripe", body, header)

        body, header = self._stripe_event(top_up, "payment_intent.processing")  # late
        late = await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual(late["action"], "no_op")
        self.assertEqual(top_up.status, "Completed")  # not dragged back to Processing
        self.assertEqual(wallet.balance, top_up.net_amount)
        count, *_ = await self._ledger_totals(top_up)
        self.assertEqual(count, 1)

    # ------------------------------------------------------------------ cancel (T034g)

    def _cancelling_provider(self, *, refuse_with_status=None):
        """A real StripePaymentProvider over a fake client whose cancel() either succeeds or is
        refused by Stripe because the payment already has ``refuse_with_status``."""
        import stripe
        from types import SimpleNamespace

        calls = []

        class _Intents:
            def cancel(self_inner, intent, params=None, options=None):
                calls.append(intent)
                if refuse_with_status is not None:
                    raise stripe.InvalidRequestError("cannot cancel", param="intent")
                return SimpleNamespace(id=intent, status="canceled")

            def retrieve(self_inner, intent, params=None, options=None):
                return SimpleNamespace(id=intent, status=refuse_with_status or "canceled", client_secret="x")

        from services.topup.stripe_provider import StripePaymentProvider

        provider = StripePaymentProvider(
            webhook_secret=self.STRIPE_TEST_SECRET,
            client=SimpleNamespace(v1=SimpleNamespace(payment_intents=_Intents())),
        )
        return provider, calls

    async def _cancel(self, wallet, top_up, provider):
        from unittest.mock import patch

        from handlers.topup import cancel_top_up

        with patch("handlers.topup._initiation_provider", return_value=provider):
            return await cancel_top_up(wallet.id, top_up.id, {"sub": str(wallet.user_id)}, self.db)

    async def test_cancel_cancels_the_payment_at_stripe_and_then_our_record(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider, calls = self._cancelling_provider()

        result = await self._cancel(wallet, top_up, provider)

        await self.db.refresh(top_up)
        self.assertEqual(calls, [top_up.provider_transaction_reference])
        self.assertEqual((result.top_up.status.value, top_up.status), ("Cancelled", "Cancelled"))

    async def test_cancel_is_refused_when_stripe_says_the_payment_can_no_longer_be_cancelled(self):
        from handlers.topup import TopUpApiError
        from handlers.topup_contracts import ErrorCode

        wallet, top_up = await self._pending_stripe_top_up()
        provider, _ = self._cancelling_provider(refuse_with_status="succeeded")

        with self.assertRaises(TopUpApiError) as raised:
            await self._cancel(wallet, top_up, provider)

        await self.db.refresh(top_up)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_STATE)
        self.assertEqual(top_up.status, "Pending")  # NOT cancelled: the customer may have been charged

    async def test_a_late_success_after_cancel_is_never_credited_and_leaves_an_audit_record(self):
        from models.audit_log import AuditLog

        wallet, top_up = await self._pending_stripe_top_up()
        provider, _ = self._cancelling_provider()
        await self._cancel(wallet, top_up, provider)
        body, header = self._stripe_event(top_up, "payment_intent.succeeded")

        result = await process_webhook(self.db, self._stripe_provider(), "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((result["action"], top_up.status), ("no_op", "Cancelled"))
        self.assertEqual(wallet.balance, Decimal("0.00"))  # never credited
        count, *_ = await self._ledger_totals(top_up)
        self.assertEqual(count, 0)
        audit = (
            await self.db.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == str(top_up.id),
                    AuditLog.action == "topup_late_success_on_terminal_top_up",
                )
            )
        ).scalars().all()
        self.assertEqual(len(audit), 1)  # findable, not silently dropped
        self.assertEqual(audit[0].details["top_up_status"], "Cancelled")

    # ------------------------------------------------------------------ failed attempts (T034i)
    #
    # Human decision 2026-09-19 (option A). Found in the first real end-to-end run: Stripe sends
    # payment_intent.payment_failed for EVERY failed attempt and keeps the payment payable, so
    # a decline followed by a successful retry on the same payment used to arrive for a terminal
    # Failed top-up: $3.00 taken, wallet not credited.

    _DECLINE = {"last_payment_error": {"code": "card_declined", "decline_code": "generic_decline"}}

    async def _audit_actions(self, top_up):
        from models.audit_log import AuditLog

        rows = (
            await self.db.execute(select(AuditLog.action).where(AuditLog.resource_id == str(top_up.id)))
        ).scalars().all()
        return sorted(rows)

    async def test_a_declined_attempt_keeps_the_top_up_open_with_no_financial_effect(self):
        wallet, top_up = await self._pending_stripe_top_up()
        body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._DECLINE)

        result = await process_webhook(self.db, self._stripe_provider(), "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((result["status"], result["action"]), ("acknowledged", "attempt_failed_recorded"))
        self.assertEqual(top_up.status, "Pending")  # NOT terminal: the customer may retry
        self.assertIsNone(top_up.failure_code)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        count, *_ = await self._ledger_totals(top_up)
        self.assertEqual(count, 0)
        self.assertEqual(await self._audit_actions(top_up), ["topup_payment_attempt_failed"])

    async def test_a_decline_followed_by_a_successful_retry_credits_the_wallet_exactly_once(self):
        # The exact sequence that lost money in the first real end-to-end run.
        wallet, top_up = await self._pending_stripe_top_up()
        provider = self._stripe_provider()

        body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._DECLINE)
        await process_webhook(self.db, provider, "stripe", body, header)
        body, header = self._stripe_event(top_up, "payment_intent.succeeded")
        done = await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((done["status"], done["action"]), ("success", "completed"))
        self.assertEqual(top_up.status, "Completed")
        self.assertEqual(wallet.balance, top_up.net_amount)
        count, debit, credit, posted = await self._ledger_totals(top_up)
        self.assertEqual((count, posted, debit), (1, [True], top_up.gross_amount))
        self.assertEqual(debit, credit)
        # The failed attempt is on record, and nothing was flagged as a late success.
        self.assertEqual(await self._audit_actions(top_up), ["topup_completed", "topup_payment_attempt_failed"])

    async def test_several_declines_then_a_success_still_credits_once(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider = self._stripe_provider()
        for _ in range(3):
            body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._DECLINE)
            await process_webhook(self.db, provider, "stripe", body, header)
        body, header = self._stripe_event(top_up, "payment_intent.succeeded")
        await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((top_up.status, wallet.balance), ("Completed", top_up.net_amount))
        actions = await self._audit_actions(top_up)
        self.assertEqual(actions.count("topup_payment_attempt_failed"), 3)
        self.assertNotIn("topup_late_success_on_terminal_top_up", actions)

    async def test_a_cancelled_payment_is_what_fails_the_top_up_with_no_financial_effect(self):
        wallet, top_up = await self._pending_stripe_top_up()
        body, header = self._stripe_event(
            top_up, "payment_intent.canceled", received_cents=0, extra={"cancellation_reason": "abandoned"}
        )

        result = await process_webhook(self.db, self._stripe_provider(), "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((result["action"], top_up.status), ("failed", "Failed"))
        self.assertEqual(top_up.failure_code, "payment_canceled_abandoned")
        self.assertEqual(wallet.balance, Decimal("0.00"))
        count, *_ = await self._ledger_totals(top_up)
        self.assertEqual(count, 0)

    async def test_the_cancel_event_for_a_payment_we_cancelled_ourselves_changes_nothing(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider, _ = self._cancelling_provider()
        await self._cancel(wallet, top_up, provider)  # our endpoint: cancels at Stripe, then locally
        body, header = self._stripe_event(top_up, "payment_intent.canceled", received_cents=0)

        result = await process_webhook(self.db, self._stripe_provider(), "stripe", body, header)

        await self.db.refresh(top_up)
        self.assertEqual((result["action"], top_up.status), ("no_op", "Cancelled"))
        self.assertNotIn("topup_late_success_on_terminal_top_up", await self._audit_actions(top_up))

    # ------------------------------------------------------------------ bank (ACH) failed debit (T034d)
    #
    # Human decision 2026-09-19: a failed bank debit ENDS the top-up as Failed and the Stripe
    # payment is cancelled (a retry is a new top-up with a new authorization). Cards keep the
    # T034i attempt rule, covered above.

    _ACH_FAILURE = {"last_payment_error": {"code": "account_closed"}}

    async def test_a_failed_bank_debit_fails_the_top_up_and_cancels_the_stripe_payment(self):
        wallet, top_up = await self._pending_stripe_top_up(funding_method="bank_transfer")
        provider, calls = self._cancelling_provider()
        body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._ACH_FAILURE)

        result = await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((result["status"], result["action"]), ("success", "failed"))
        self.assertEqual((top_up.status, top_up.failure_code), ("Failed", "account_closed"))
        self.assertEqual(calls, [top_up.provider_transaction_reference])  # cancelled at Stripe, after failing
        self.assertEqual(wallet.balance, Decimal("0.00"))
        count, *_ = await self._ledger_totals(top_up)
        self.assertEqual(count, 0)
        actions = await self._audit_actions(top_up)
        self.assertIn("topup_failed", actions)
        self.assertNotIn("topup_payment_attempt_failed", actions)  # the card rule was not applied
        self.assertNotIn("topup_provider_cancel_failed", actions)

    async def test_a_success_for_a_failed_bank_top_up_is_flagged_and_never_credited(self):
        wallet, top_up = await self._pending_stripe_top_up(funding_method="bank_transfer")
        provider, _ = self._cancelling_provider()
        body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._ACH_FAILURE)
        await process_webhook(self.db, provider, "stripe", body, header)
        body, header = self._stripe_event(top_up, "payment_intent.succeeded")

        result = await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((result["action"], top_up.status), ("no_op", "Failed"))
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIn("topup_late_success_on_terminal_top_up", await self._audit_actions(top_up))

    async def test_a_bank_payment_that_cannot_be_cancelled_still_leaves_the_top_up_failed_and_is_audited(self):
        wallet, top_up = await self._pending_stripe_top_up(funding_method="bank_transfer")
        provider, _ = self._cancelling_provider(refuse_with_status="processing")
        body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._ACH_FAILURE)

        result = await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        self.assertEqual((result["status"], top_up.status), ("success", "Failed"))
        self.assertIn("topup_provider_cancel_failed", await self._audit_actions(top_up))

    async def test_a_card_decline_is_unchanged_by_the_bank_rule(self):
        wallet, top_up = await self._pending_stripe_top_up(funding_method="card")
        provider, calls = self._cancelling_provider()
        body, header = self._stripe_event(top_up, "payment_intent.payment_failed", received_cents=0, extra=self._DECLINE)

        await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        self.assertEqual((top_up.status, calls), ("Pending", []))

    async def test_stripe_short_receipt_routes_to_under_review_and_credits_nothing(self):
        wallet, top_up = await self._pending_stripe_top_up()
        provider = self._stripe_provider()
        short = int(top_up.gross_amount * 100) - 1
        body, header = self._stripe_event(top_up, "payment_intent.succeeded", received_cents=short)

        result = await process_webhook(self.db, provider, "stripe", body, header)

        await self.db.refresh(top_up)
        await self.db.refresh(wallet)
        self.assertEqual((result["action"], top_up.status), ("under_review", "UnderReview"))
        self.assertEqual(wallet.balance, Decimal("0.00"))
        count, *_ = await self._ledger_totals(top_up)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])