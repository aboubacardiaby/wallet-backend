import unittest
import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers.payment as payment
import handlers.user as user_handler
from models.user import User
from services.ach import ACHResult, AchClientConfig



class FakeDB:
    """Minimal AsyncSession stand-in: records every statement passed to
    scalar() and always returns a preset object, regardless of the query."""

    def __init__(self, scalar_result):
        self._scalar_result = scalar_result
        self.scalar_calls = []
        self.added = []
        self.committed = False

    async def scalar(self, stmt):
        self.scalar_calls.append(stmt)
        return self._scalar_result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class AchCreditLockingTests(unittest.IsolatedAsyncioTestCase):
    """ach_credit debits a wallet before pushing money out via ACH — it must
    lock the row (SELECT ... FOR UPDATE) or two concurrent payouts can both
    pass the balance check and overdraw the wallet."""

    def make_wallet(self):
        return SimpleNamespace(
            balance=Decimal("1000.00"),
            currency="USD",
            status="active",
            daily_limit=Decimal("5000.00"),
            monthly_limit=Decimal("20000.00"),
            daily_spent=Decimal("0.00"),
            monthly_spent=Decimal("0.00"),
            last_reset_date=None,
            updated_at=None,
        )

    async def test_wallet_select_uses_row_locking(self):
        wallet = self.make_wallet()
        db = FakeDB(wallet)
        body = payment.ACHCreditRequest(
            routing_number="021000021",
            account_number="123456789",
            account_type="CHECKING",
            account_name="Jane Doe",
            amount=100.0,
        )
        token = {"user_id": str(uuid.uuid4()), "phone_number": "+15551234567"}

        cfg = AchClientConfig(
            base_url="https://example.test",
            api_key="k",
            platform_account_number="1",
            platform_routing_number="2",
            platform_account_type="CHECKING",
            platform_account_name="Platform",
            enabled=True,
        )
        result = ACHResult(payment_id="p1", trace_number="t1", status="PENDING")

        with patch.object(payment, "_load_ach_config", AsyncMock(return_value=cfg)), \
             patch.object(payment, "initiate_credit", AsyncMock(return_value=result)):
            await payment.ach_credit(body, token, db)

        self.assertTrue(db.scalar_calls, "wallet lookup never happened")
        wallet_select = db.scalar_calls[0]
        self.assertIn("FOR UPDATE", str(wallet_select))

        # sanity: the payout actually debited the (locked) wallet
        self.assertEqual(wallet.balance, Decimal("900.00"))
        self.assertTrue(db.committed)


class ProfileAllowlistTests(unittest.IsolatedAsyncioTestCase):
    """update_profile must only ever touch fields on an explicit allowlist —
    security-sensitive columns (lockout counters, KYC status, role) must
    never be settable by the user themselves."""

    def test_allowlist_excludes_sensitive_fields(self):
        sensitive = {
            "id", "phone_number", "pin", "pin_attempts", "is_locked",
            "is_verified", "kyc_status", "user_type", "created_at",
        }
        self.assertTrue(sensitive.isdisjoint(user_handler.ALLOWED_PROFILE_FIELDS))

    def test_allowlist_is_a_subset_of_real_user_columns(self):
        real_columns = {c.name for c in User.__table__.columns}
        self.assertTrue(user_handler.ALLOWED_PROFILE_FIELDS.issubset(real_columns))

    async def test_update_profile_ignores_disallowed_fields(self):
        fake_user = SimpleNamespace(
            id=uuid.uuid4(),
            full_name="Old Name",
            is_locked=True,
            pin_attempts=3,
            kyc_status="pending",
            user_type="receiver",
            phone_number="+15550000000",
            is_verified=False,
            pin="hashed-pin",
            updated_at=None,
        )
        db = FakeDB(fake_user)
        token = {"user_id": str(fake_user.id)}
        update_data = {
            "full_name": "New Name",       # allowed
            "is_locked": False,            # attempted lockout bypass
            "pin_attempts": 0,             # attempted lockout bypass
            "kyc_status": "verified",      # attempted self-KYC-approval
            "user_type": "admin",          # attempted privilege escalation
            "phone_number": "+15559999999",
            "is_verified": True,
        }

        await user_handler.update_profile(update_data, token, db)

        self.assertEqual(fake_user.full_name, "New Name")
        self.assertTrue(fake_user.is_locked)
        self.assertEqual(fake_user.pin_attempts, 3)
        self.assertEqual(fake_user.kyc_status, "pending")
        self.assertEqual(fake_user.user_type, "receiver")
        self.assertEqual(fake_user.phone_number, "+15550000000")
        self.assertFalse(fake_user.is_verified)
        self.assertTrue(db.committed)


if __name__ == "__main__":
    unittest.main()
