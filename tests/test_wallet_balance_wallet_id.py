"""GET /wallet/balance must expose the caller's own wallet id as ``wallet_id`` (T033).

The top-up routes are addressed as /wallets/{walletId}/top-ups, so the mobile
client needs the id of the authenticated user's own wallet. The endpoint already
scopes the lookup to the JWT's user, so exposing it discloses nothing new.
"""
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import handlers.wallet as wallet_handler
from models.wallet import Wallet


class FakeDB:
    def __init__(self, wallet):
        self._wallet = wallet

    async def scalar(self, stmt):
        return self._wallet


class WalletBalanceWalletIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_response_includes_wallet_id(self):
        user_id = uuid.uuid4()
        wallet = Wallet(
            id=uuid.uuid4(),
            user_id=user_id,
            balance=Decimal("10.00"),
            currency="USD",
            status="active",
            created_at=datetime.now(timezone.utc),
        )

        body = await wallet_handler.get_balance(
            token={"user_id": str(user_id)}, db=FakeDB(wallet)
        )

        self.assertEqual(body["wallet_id"], str(wallet.id))
        self.assertNotIn("id", body)
        self.assertEqual(body["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
