import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import Response

from handlers.topup import (
    TopUpApiError,
    _build_request_fingerprint,
    _enforce_wallet_limits,
    _normalize_idempotency_key,
    _owned_wallet,
    _top_up_response,
    _user_id_from_token,
    initiate_top_up,
)
from handlers.topup_contracts import ErrorCode, FundingMethod, InitiateTopUpRequest
from main import app


class TopUpEndpointSecurityTests(unittest.TestCase):
    def test_all_customer_routes_are_registered(self):
        routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
        self.assertIn(("/api/v1/wallets/{wallet_id}/top-ups", "POST"), routes)
        self.assertIn(("/api/v1/wallets/{wallet_id}/top-ups", "GET"), routes)
        self.assertIn(("/api/v1/wallets/{wallet_id}/top-ups/{top_up_id}", "GET"), routes)
        self.assertIn(("/api/v1/wallets/{wallet_id}/top-ups/{top_up_id}/cancel", "POST"), routes)

    def test_token_subject_supports_canonical_sub_and_existing_user_id(self):
        user_id = uuid.uuid4()
        self.assertEqual(_user_id_from_token({"sub": str(user_id)}), user_id)
        self.assertEqual(_user_id_from_token({"user_id": str(user_id)}), user_id)

        for token in ({}, {"sub": "not-a-uuid"}):
            with self.subTest(token=token), self.assertRaises(TopUpApiError) as raised:
                _user_id_from_token(token)
            self.assertEqual(raised.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)

    def test_fingerprint_changes_for_any_material_request_field(self):
        base = InitiateTopUpRequest(
            amount="100.00", currency="USD", funding_method=FundingMethod.CARD,
            funding_token="tok_secret",
        )
        same = InitiateTopUpRequest(
            amount="100.0", currency="USD", funding_method=FundingMethod.CARD,
            funding_token="tok_secret",
        )
        changed = InitiateTopUpRequest(
            amount="100.00", currency="USD", funding_method=FundingMethod.CARD,
            funding_token="different",
        )
        self.assertEqual(_build_request_fingerprint(base), _build_request_fingerprint(same))
        self.assertNotEqual(_build_request_fingerprint(base), _build_request_fingerprint(changed))

    def test_limit_enforcement_uses_exact_decimals(self):
        wallet = SimpleNamespace(
            daily_limit=Decimal("100.00"), monthly_limit=Decimal("500.00"),
            daily_spent=Decimal("90.00"), monthly_spent=Decimal("490.00"),
        )
        _enforce_wallet_limits(wallet, Decimal("10.00"))
        with self.assertRaises(TopUpApiError) as raised:
            _enforce_wallet_limits(wallet, Decimal("10.01"))
        self.assertEqual(raised.exception.code, ErrorCode.LIMIT_EXCEEDED)

    def test_idempotency_key_cannot_be_whitespace(self):
        with self.assertRaises(TopUpApiError) as raised:
            _normalize_idempotency_key("        ")
        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertEqual(_normalize_idempotency_key("  valid-key  "), "valid-key")

    def test_response_is_masked_and_contains_no_raw_funding_value(self):
        top_up = SimpleNamespace(
            id=uuid.uuid4(), internal_reference="tu_safe", wallet_id=uuid.uuid4(),
            status="Pending", gross_amount=Decimal("100.00"),
            fee_amount=Decimal("1.50"), net_amount=Decimal("98.50"), currency="USD",
            funding_method="card", provider_name=None,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            completed_at=None,
        )
        response = _top_up_response(top_up).model_dump(mode="json")
        rendered = str(response)
        self.assertNotIn("funding_token", rendered)
        self.assertNotIn("funding_reference", rendered)
        self.assertEqual(response["funding"], {
            "method": "card", "provider": None, "display_name": None, "last4": None,
        })


class _ScalarRows:
    def all(self):
        return []


class _FakeDB:
    def __init__(self, scalar_values):
        self.scalar_values = list(scalar_values)
        self.statements = []
        self.added = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_values.pop(0)

    async def scalars(self, statement):
        self.statements.append(statement)
        return _ScalarRows()

    def add(self, value):
        value.id = value.id or uuid.uuid4()
        self.added.append(value)

    async def commit(self):
        pass

    async def refresh(self, value):
        pass


class TopUpEndpointAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_wallet_query_enforces_user_boundary_and_non_enumeration(self):
        db = _FakeDB([None])
        with self.assertRaises(TopUpApiError) as raised:
            await _owned_wallet(db, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(raised.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
        compiled = str(db.statements[0])
        self.assertIn("wallets.id", compiled)
        self.assertIn("wallets.user_id", compiled)

    async def test_initiation_persists_pending_record_without_funding_secret(self):
        user_id = uuid.uuid4()
        wallet_id = uuid.uuid4()
        wallet = SimpleNamespace(
            id=wallet_id, user_id=user_id, status="active", currency="USD",
            daily_limit=Decimal("1000"), monthly_limit=Decimal("5000"),
            daily_spent=Decimal("0"), monthly_spent=Decimal("0"),
        )
        db = _FakeDB([wallet, None])
        request = InitiateTopUpRequest(
            amount="100.00", currency="USD", funding_method="card",
            funding_token="tok_must_not_persist",
        )
        result = await initiate_top_up(
            wallet_id, request, Response(), "valid-key", {"sub": str(user_id)}, db
        )
        saved = db.added[0]
        self.assertEqual(saved.status, "Pending")
        self.assertEqual(saved.fee_amount, Decimal("1.50"))
        self.assertEqual(saved.net_amount, Decimal("98.50"))
        self.assertFalse(hasattr(saved, "funding_token"))
        self.assertFalse(hasattr(saved, "funding_reference"))
        self.assertFalse(result.idempotent_replay)


# =========================================================================== T034c
#
# Provider-backed initiation (Stripe PaymentIntent for card / bank_transfer). The
# provider is injected by patching handlers.topup._initiation_provider, so nothing
# here touches the network (tests/conftest.py also strips ambient Stripe env vars).

import os  # noqa: E402
from unittest.mock import patch  # noqa: E402

from handlers.topup import _initiation_provider  # noqa: E402
from handlers.topup_contracts import NextAction, error_response  # noqa: E402
from services.topup.money import Money  # noqa: E402
from services.topup.provider import (  # noqa: E402
    InitiationResult,
    InvalidProviderStateError,
    ProviderConfigurationError,
    ProviderRejectedError,
    ProviderUnavailableError,
)

CLIENT_SECRET = "cs_test_secret_that_must_only_appear_in_the_initiation_response"
# Stripe signing secrets begin with "whsec_"; the provider refuses anything else.
GOOD_WEBHOOK_SECRET = "whsec_test_endpoint_secret_not_real"


class _CountingDB(_FakeDB):
    def __init__(self, scalar_values):
        super().__init__(scalar_values)
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks = getattr(self, "rollbacks", 0) + 1


class _FakeProvider:
    name = "stripe"

    def __init__(self, db=None, *, initiate_error=None, retrieve_error=None, retrieve_secret="cs_replayed"):
        self.db = db
        self.initiate_error = initiate_error
        self.retrieve_error = retrieve_error
        self.retrieve_secret = retrieve_secret
        self.initiate_calls = []
        self.retrieve_calls = []
        self.commits_at_initiate = None

    def initiate(self, request):
        self.initiate_calls.append(request)
        self.commits_at_initiate = self.db.commits if self.db is not None else None
        if self.initiate_error is not None:
            raise self.initiate_error
        return InitiationResult(
            provider_transaction_reference="pi_fake_1", status="Pending",
            requires_action=True, client_secret=CLIENT_SECRET,
        )

    def retrieve_client_secret(self, reference):
        self.retrieve_calls.append(reference)
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return self.retrieve_secret


def _wallet_for(user_id, wallet_id):
    return SimpleNamespace(
        id=wallet_id, user_id=user_id, status="active", currency="USD",
        daily_limit=Decimal("1000"), monthly_limit=Decimal("5000"),
        daily_spent=Decimal("0"), monthly_spent=Decimal("0"),
    )


def _card_request(method="card"):
    return InitiateTopUpRequest(
        amount="100.00", currency="USD", funding_method=method, funding_token="tok_must_not_persist"
    )


def _existing_top_up(request, wallet_id, *, status="Pending", reference=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(), internal_reference="tu_existing_ref", wallet_id=wallet_id,
        idempotency_key="valid-key", request_fingerprint=_build_request_fingerprint(request),
        funding_method=request.funding_method.value, status=status,
        provider_name="stripe" if reference else None, provider_transaction_reference=reference,
        gross_amount=Decimal("100.00"), fee_amount=Decimal("1.50"), net_amount=Decimal("98.50"),
        currency="USD", failure_code=None, failure_message=None,
        created_at=now, updated_at=now, completed_at=None,
    )


class ProviderBackedInitiationTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_initiation_creates_the_payment_intent_after_the_pending_row_commits(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        db = _CountingDB([_wallet_for(user_id, wallet_id), None])
        provider = _FakeProvider(db)
        with patch("handlers.topup._initiation_provider", return_value=provider):
            result = await initiate_top_up(
                wallet_id, _card_request(), Response(), "valid-key", {"sub": str(user_id)}, db
            )

        saved = db.added[0]
        self.assertGreaterEqual(provider.commits_at_initiate, 1)  # Pending row already durable
        self.assertEqual((saved.status, saved.provider_name, saved.provider_transaction_reference),
                         ("Pending", "stripe", "pi_fake_1"))
        self.assertEqual(len(provider.initiate_calls), 1)
        call = provider.initiate_calls[0]
        self.assertEqual(call.gross_amount, Money("100.00", "USD"))
        self.assertEqual(call.funding_method, "card")
        # Stripe keys are account-wide; the client's per-wallet key must never be forwarded.
        self.assertEqual(call.idempotency_key, saved.internal_reference)
        self.assertNotEqual(call.idempotency_key, "valid-key")
        action = result.top_up.next_action
        self.assertEqual((action.type, action.provider, action.client_secret),
                         ("confirm_with_provider", "stripe", CLIENT_SECRET))
        self.assertFalse(result.idempotent_replay)
        self.assertFalse(hasattr(saved, "funding_token"))

    async def test_the_client_secret_is_not_persisted_on_the_top_up(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        db = _CountingDB([_wallet_for(user_id, wallet_id), None])
        with patch("handlers.topup._initiation_provider", return_value=_FakeProvider(db)):
            await initiate_top_up(wallet_id, _card_request(), Response(), "valid-key", {"sub": str(user_id)}, db)
        self.assertNotIn(CLIENT_SECRET, repr(vars(db.added[0])))

    async def test_a_definitive_rejection_fails_the_top_up_without_a_secret(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        db = _CountingDB([_wallet_for(user_id, wallet_id), None])
        provider = _FakeProvider(db, initiate_error=ProviderRejectedError("no"))
        with patch("handlers.topup._initiation_provider", return_value=provider):
            result = await initiate_top_up(
                wallet_id, _card_request(), Response(), "valid-key", {"sub": str(user_id)}, db
            )
        saved = db.added[0]
        self.assertEqual((saved.status, saved.failure_code), ("Failed", "provider_rejected"))
        self.assertIsNone(saved.provider_transaction_reference)
        self.assertEqual(result.top_up.status.value, "Failed")
        self.assertIsNone(result.top_up.next_action)

    async def test_an_unavailable_provider_leaves_the_top_up_pending_and_asks_the_client_to_retry(self):
        for error in (ProviderUnavailableError("down"), ProviderConfigurationError("bad key")):
            with self.subTest(error=type(error).__name__):
                user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
                db = _CountingDB([_wallet_for(user_id, wallet_id), None])
                provider = _FakeProvider(db, initiate_error=error)
                with patch("handlers.topup._initiation_provider", return_value=provider):
                    with self.assertRaises(TopUpApiError) as raised:
                        await initiate_top_up(
                            wallet_id, _card_request(), Response(), "valid-key", {"sub": str(user_id)}, db
                        )
                self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
                saved = db.added[0]
                self.assertEqual(saved.status, "Pending")
                self.assertIsNone(saved.provider_transaction_reference)

    async def test_replay_of_a_top_up_that_never_got_a_payment_intent_initiates_it_again(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        request = _card_request()
        existing = _existing_top_up(request, wallet_id, reference=None)
        db = _CountingDB([_wallet_for(user_id, wallet_id), existing])
        provider = _FakeProvider(db)
        with patch("handlers.topup._initiation_provider", return_value=provider):
            result = await initiate_top_up(wallet_id, request, Response(), "valid-key", {"sub": str(user_id)}, db)

        self.assertEqual(db.added, [])  # no second top-up row
        self.assertEqual(provider.initiate_calls[0].idempotency_key, "tu_existing_ref")
        self.assertEqual(existing.provider_transaction_reference, "pi_fake_1")
        self.assertTrue(result.idempotent_replay)
        self.assertEqual(result.top_up.next_action.client_secret, CLIENT_SECRET)

    async def test_replay_of_a_top_up_that_already_has_a_payment_intent_never_creates_another(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        request = _card_request()
        existing = _existing_top_up(request, wallet_id, reference="pi_already_1")
        db = _CountingDB([_wallet_for(user_id, wallet_id), existing])
        provider = _FakeProvider(db, retrieve_secret="cs_replayed_secret")
        with patch("handlers.topup._initiation_provider", return_value=provider):
            result = await initiate_top_up(wallet_id, request, Response(), "valid-key", {"sub": str(user_id)}, db)

        self.assertEqual(provider.initiate_calls, [])  # a second PaymentIntent could charge with nothing to credit
        self.assertEqual(provider.retrieve_calls, ["pi_already_1"])
        self.assertEqual(existing.provider_transaction_reference, "pi_already_1")
        self.assertTrue(result.idempotent_replay)
        self.assertEqual(result.top_up.next_action.client_secret, "cs_replayed_secret")

    async def test_replay_after_the_customer_has_already_confirmed_returns_no_secret(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        request = _card_request()
        existing = _existing_top_up(request, wallet_id, reference="pi_already_1")
        db = _CountingDB([_wallet_for(user_id, wallet_id), existing])
        provider = _FakeProvider(db, retrieve_error=InvalidProviderStateError("processing"))
        with patch("handlers.topup._initiation_provider", return_value=provider):
            result = await initiate_top_up(wallet_id, request, Response(), "valid-key", {"sub": str(user_id)}, db)
        self.assertEqual(result.top_up.next_action.type, "await_provider")
        self.assertIsNone(result.top_up.next_action.client_secret)

    async def test_replay_when_the_provider_is_unavailable_asks_the_client_to_retry(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        request = _card_request()
        existing = _existing_top_up(request, wallet_id, reference="pi_already_1")
        db = _CountingDB([_wallet_for(user_id, wallet_id), existing])
        provider = _FakeProvider(db, retrieve_error=ProviderUnavailableError("down"))
        with patch("handlers.topup._initiation_provider", return_value=provider):
            with self.assertRaises(TopUpApiError) as raised:
                await initiate_top_up(wallet_id, request, Response(), "valid-key", {"sub": str(user_id)}, db)
        self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    async def test_replay_of_a_top_up_that_is_no_longer_pending_does_not_touch_the_provider(self):
        for status in ("Processing", "Completed", "Failed", "Cancelled"):
            with self.subTest(status=status):
                user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
                request = _card_request()
                existing = _existing_top_up(request, wallet_id, status=status, reference="pi_already_1")
                db = _CountingDB([_wallet_for(user_id, wallet_id), existing])
                provider = _FakeProvider(db)
                with patch("handlers.topup._initiation_provider", return_value=provider):
                    result = await initiate_top_up(
                        wallet_id, request, Response(), "valid-key", {"sub": str(user_id)}, db
                    )
                self.assertEqual((provider.initiate_calls, provider.retrieve_calls), ([], []))
                self.assertTrue(result.idempotent_replay)
                self.assertIsNone(result.top_up.next_action)

    async def test_the_secret_is_never_part_of_a_detail_or_history_style_response(self):
        top_up = _existing_top_up(_card_request(), uuid.uuid4(), reference="pi_1")
        response = _top_up_response(top_up)
        self.assertIsNone(response.next_action.client_secret)
        self.assertNotIn("client_secret", response.model_dump_json(exclude_none=True))

    async def test_methods_that_do_not_use_the_provider_never_reach_it(self):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        db = _CountingDB([_wallet_for(user_id, wallet_id), None])
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": GOOD_WEBHOOK_SECRET}):
            with patch("handlers.topup.StripePaymentProvider") as stripe_cls:
                result = await initiate_top_up(
                    wallet_id, _card_request("mobile_money"), Response(), "valid-key", {"sub": str(user_id)}, db
                )
        stripe_cls.assert_not_called()
        self.assertEqual(db.added[0].status, "Pending")
        self.assertIsNone(db.added[0].provider_transaction_reference)
        self.assertEqual(result.top_up.next_action.type, "await_provider")


class InitiationProviderSelectionTests(unittest.TestCase):
    def test_only_card_and_bank_transfer_use_the_provider(self):
        env = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": GOOD_WEBHOOK_SECRET}
        with patch.dict(os.environ, env):
            for method in ("agent_cash", "mobile_money"):
                self.assertIsNone(_initiation_provider(method))
            for method in ("card", "bank_transfer"):
                provider = _initiation_provider(method)
                self.assertEqual(provider.name, "stripe")

    def test_without_a_key_development_keeps_the_legacy_pending_only_behavior(self):
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            self.assertIsNone(_initiation_provider("card"))

    def test_without_a_key_production_fails_closed(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            with self.assertRaises(TopUpApiError) as raised:
                _initiation_provider("card")
        self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_a_key_without_a_webhook_secret_fails_closed_in_every_environment(self):
        # Never create a payment this system could not later confirm.
        for app_env in ("development", "production"):
            with self.subTest(app_env=app_env):
                with patch.dict(os.environ, {"APP_ENV": app_env, "STRIPE_SECRET_KEY": "sk_test_x"}):
                    with self.assertRaises(TopUpApiError) as raised:
                        _initiation_provider("card")
                self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_a_live_key_is_refused_outside_production(self):
        env = {"APP_ENV": "development", "STRIPE_SECRET_KEY": "sk_live_x", "STRIPE_WEBHOOK_SECRET": GOOD_WEBHOOK_SECRET}
        with patch.dict(os.environ, env):
            with self.assertRaises(TopUpApiError) as raised:
                _initiation_provider("card")
        self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_a_webhook_secret_that_is_not_a_signing_secret_fails_closed(self):
        # Review finding: a non-empty but wrong value (a publishable key was already supplied
        # here once) let payments start although every webhook would fail authentication:
        # customers charged, wallets never credited.
        for bad in ("pk_test_51ExamplePublishableKeyValue", "sk_test_51ExampleSecretKeyValue",
                    "not-a-signing-secret-value", "whsec_", "whsec_short"):
            for app_env in ("development", "production"):
                with self.subTest(secret=bad[:12], app_env=app_env):
                    env = {"APP_ENV": app_env, "STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": bad}
                    with patch.dict(os.environ, env):
                        with self.assertRaises(TopUpApiError) as raised:
                            _initiation_provider("card")
                    self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_an_api_key_that_is_not_a_secret_key_fails_closed(self):
        env = {"APP_ENV": "development", "STRIPE_SECRET_KEY": "pk_test_51ExamplePublishableKeyValue",
               "STRIPE_WEBHOOK_SECRET": GOOD_WEBHOOK_SECRET}
        with patch.dict(os.environ, env):
            with self.assertRaises(TopUpApiError) as raised:
                _initiation_provider("card")
        self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_surrounding_whitespace_in_the_environment_is_tolerated_consistently(self):
        env = {"STRIPE_SECRET_KEY": " sk_test_x \n", "STRIPE_WEBHOOK_SECRET": f" {GOOD_WEBHOOK_SECRET}\n"}
        with patch.dict(os.environ, env):
            self.assertEqual(_initiation_provider("card").name, "stripe")


class ProviderContractTests(unittest.TestCase):
    def test_confirm_with_provider_next_action_is_valid_and_hides_the_secret_in_repr(self):
        action = NextAction(type="confirm_with_provider", provider="stripe", client_secret=CLIENT_SECRET)
        self.assertEqual(action.client_secret, CLIENT_SECRET)
        self.assertNotIn(CLIENT_SECRET, repr(action))

    def test_provider_unavailable_maps_to_503(self):
        status, body = error_response(ErrorCode.PROVIDER_UNAVAILABLE, "try again")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "provider_unavailable")


# =========================================================================== T034g: cancel
#
# Review finding: cancelling a provider-backed top-up only flipped our row. If the customer
# had already confirmed the payment (or still held the client secret), Stripe would charge
# them and the later verified success would hit a terminal Cancelled top-up and be ignored:
# charged, never credited. The payment must be cancelled at the provider FIRST, and a
# payment that can no longer be cancelled must block the local cancel.

from handlers.topup import cancel_top_up  # noqa: E402


class _FakeCancelProvider(_FakeProvider):
    def __init__(self, *, cancel_error=None, state=None):
        super().__init__()
        self.cancel_error = cancel_error
        self.cancel_calls = []
        self.state = state  # the top-up, to record its status at the moment of the call
        self.status_at_cancel = None

    def cancel(self, reference):
        self.cancel_calls.append(reference)
        self.status_at_cancel = self.state.status if self.state is not None else None
        if self.cancel_error is not None:
            raise self.cancel_error


class CancelTopUpTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, *, status="Pending", reference="pi_cancel_1", second_status=None):
        user_id, wallet_id = uuid.uuid4(), uuid.uuid4()
        request = _card_request()
        first = _existing_top_up(request, wallet_id, status=status, reference=reference)
        # The handler reads the top-up twice: unlocked (before the provider call) and again
        # under a row lock (after it). Both reads see the same row here unless a test says not.
        second = first if second_status is None else _existing_top_up(
            request, wallet_id, status=second_status, reference=reference
        )
        db = _CountingDB([first, second])
        return user_id, wallet_id, first, db

    async def _cancel(self, user_id, wallet_id, top_up, db, provider):
        with patch("handlers.topup._initiation_provider", return_value=provider):
            return await cancel_top_up(wallet_id, top_up.id, {"sub": str(user_id)}, db)

    async def test_the_payment_is_cancelled_at_the_provider_before_our_record_changes(self):
        user_id, wallet_id, top_up, db = self._setup()
        provider = _FakeCancelProvider(state=top_up)
        result = await self._cancel(user_id, wallet_id, top_up, db, provider)

        self.assertEqual(provider.cancel_calls, ["pi_cancel_1"])
        self.assertEqual(provider.status_at_cancel, "Pending")  # still Pending when Stripe was asked
        self.assertEqual((top_up.status, db.commits), ("Cancelled", 1))
        self.assertEqual(result.top_up.status.value, "Cancelled")

    async def test_no_row_lock_is_held_across_the_provider_call(self):
        user_id, wallet_id, top_up, db = self._setup()
        await self._cancel(user_id, wallet_id, top_up, db, _FakeCancelProvider(state=top_up))
        first, second = (str(s).upper() for s in db.statements[:2])
        self.assertNotIn("FOR UPDATE", first)
        self.assertIn("FOR UPDATE", second)

    async def test_requires_action_top_ups_are_cancelled_at_the_provider_too(self):
        user_id, wallet_id, top_up, db = self._setup(status="RequiresAction")
        provider = _FakeCancelProvider(state=top_up)
        await self._cancel(user_id, wallet_id, top_up, db, provider)
        self.assertEqual((provider.cancel_calls, top_up.status), (["pi_cancel_1"], "Cancelled"))

    async def test_a_payment_that_can_no_longer_be_cancelled_blocks_the_local_cancel(self):
        user_id, wallet_id, top_up, db = self._setup()
        provider = _FakeCancelProvider(cancel_error=ProviderRejectedError("succeeded"), state=top_up)
        with self.assertRaises(TopUpApiError) as raised:
            await self._cancel(user_id, wallet_id, top_up, db, provider)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_STATE)
        self.assertEqual((top_up.status, db.commits), ("Pending", 0))

    async def test_an_unavailable_provider_blocks_the_local_cancel(self):
        for error in (ProviderUnavailableError("down"), ProviderConfigurationError("bad key")):
            with self.subTest(error=type(error).__name__):
                user_id, wallet_id, top_up, db = self._setup()
                provider = _FakeCancelProvider(cancel_error=error, state=top_up)
                with self.assertRaises(TopUpApiError) as raised:
                    await self._cancel(user_id, wallet_id, top_up, db, provider)
                self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
                self.assertEqual((top_up.status, db.commits), ("Pending", 0))

    async def test_an_unconfigured_provider_blocks_cancelling_a_top_up_that_has_a_payment(self):
        # We cannot prove the payment is cancelled, so we must not mark it cancelled.
        user_id, wallet_id, top_up, db = self._setup()
        with self.assertRaises(TopUpApiError) as raised:
            await self._cancel(user_id, wallet_id, top_up, db, None)
        self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertEqual((top_up.status, db.commits), ("Pending", 0))

    async def test_a_top_up_with_no_provider_payment_cancels_locally_without_the_provider(self):
        user_id, wallet_id, top_up, db = self._setup(reference=None)
        with patch("handlers.topup._initiation_provider") as factory:
            await cancel_top_up(wallet_id, top_up.id, {"sub": str(user_id)}, db)
        factory.assert_not_called()
        self.assertEqual((top_up.status, db.commits), ("Cancelled", 1))

    async def test_cancelling_twice_is_idempotent_and_does_not_touch_the_provider(self):
        user_id, wallet_id, top_up, db = self._setup(status="Cancelled")
        provider = _FakeCancelProvider(state=top_up)
        result = await self._cancel(user_id, wallet_id, top_up, db, provider)
        self.assertEqual((provider.cancel_calls, db.commits), ([], 0))
        self.assertEqual(result.top_up.status.value, "Cancelled")

    async def test_states_that_cannot_be_cancelled_never_reach_the_provider(self):
        for status in ("Processing", "Completed", "Failed", "UnderReview"):
            with self.subTest(status=status):
                user_id, wallet_id, top_up, db = self._setup(status=status)
                provider = _FakeCancelProvider(state=top_up)
                with self.assertRaises(TopUpApiError) as raised:
                    await self._cancel(user_id, wallet_id, top_up, db, provider)
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_STATE)
                self.assertEqual((provider.cancel_calls, db.commits), ([], 0))

    async def test_a_state_change_while_the_provider_call_was_in_flight_is_respected(self):
        # Re-checked under the lock: if a webhook completed the top-up meanwhile, cancelling
        # locally would discard a real payment.
        user_id, wallet_id, top_up, db = self._setup(second_status="Processing")
        provider = _FakeCancelProvider(state=top_up)
        with self.assertRaises(TopUpApiError) as raised:
            await self._cancel(user_id, wallet_id, top_up, db, provider)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_STATE)
        self.assertEqual(db.commits, 0)


if __name__ == "__main__":
    unittest.main()
