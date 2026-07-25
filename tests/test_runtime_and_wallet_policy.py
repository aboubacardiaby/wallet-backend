import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from config.runtime import allow_simulated_funding, cors_origins, jwt_secret
from services.wallet_policy import credit, debit


class RuntimeConfigurationTests(unittest.TestCase):
    def test_production_requires_jwt_secret(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaises(RuntimeError):
                jwt_secret()

    def test_simulated_funding_never_enabled_in_production(self):
        with patch.dict(
            os.environ,
            {"APP_ENV": "production", "ALLOW_SIMULATED_FUNDING": "true"},
            clear=True,
        ):
            self.assertFalse(allow_simulated_funding())

    def test_production_has_no_default_cors_origins(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            self.assertEqual(cors_origins(), [])


class WalletPolicyTests(unittest.TestCase):
    def wallet(self, **overrides):
        values = {
            "balance": Decimal("100.00"),
            "daily_limit": Decimal("50.00"),
            "monthly_limit": Decimal("90.00"),
            "daily_spent": Decimal("10.00"),
            "monthly_spent": Decimal("20.00"),
            "last_reset_date": None,
            "updated_at": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_debit_updates_balance_and_counters_exactly(self):
        wallet = self.wallet()
        debit(wallet, "25.10")
        self.assertEqual(wallet.balance, Decimal("74.90"))
        self.assertEqual(wallet.daily_spent, Decimal("25.10"))
        self.assertEqual(wallet.monthly_spent, Decimal("25.10"))

    def test_debit_rejects_daily_limit(self):
        wallet = self.wallet(last_reset_date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        with self.assertRaises(HTTPException) as raised:
            debit(wallet, "45.00")
        self.assertEqual(raised.exception.detail, "Daily spending limit exceeded")

    def test_credit_uses_decimal_rounding(self):
        wallet = self.wallet(balance=Decimal("10.00"))
        credit(wallet, "0.105")
        self.assertEqual(wallet.balance, Decimal("10.11"))


if __name__ == "__main__":
    unittest.main()
