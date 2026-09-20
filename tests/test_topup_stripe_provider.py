"""Stripe provider: webhook verification and event mapping (T034a; FR-006, FR-007, FR-009).

Written before services/topup/stripe_provider.py exists (Constitution VI).

Signing here is done with this file's own HMAC code, written from Stripe's
documented manual-verification steps (https://docs.stripe.com/webhooks,
"Verify webhook signatures manually"), NOT by calling the SDK's signer, so
the tests do not depend on the implementation under test. Documented facts
pinned below:

* header ``Stripe-Signature: t=<unix>,v1=<hex>[,v1=<hex>][,v0=<hex>]``;
* signed payload is ``"<t>.<raw body>"``, HMAC-SHA256 keyed by the endpoint secret;
* only ``v1`` is valid (``v0`` is a fake test scheme and must be ignored);
* several ``v1`` entries appear while an endpoint secret is being rolled;
* default tolerance is 5 minutes, and a tolerance of 0 disables the check;
* Stripe amounts are integers in minor units.

The secret below is an obviously fake test constant, not a credential.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from services.topup.money import Money
from services.topup.provider import (
    ATTEMPT_FAILED,
    IgnoredWebhookEventError,
    InvalidWebhookPayloadError,
    PaymentProvider,
    ProviderConfigurationError,
    ProviderNotImplementedError,
    ReversalNotSupportedError,
    WebhookEvent,
    WebhookVerificationError,
)
from services.topup.stripe_provider import StripePaymentProvider

# Stripe signing secrets begin with "whsec_" (https://docs.stripe.com/webhooks), and the
# provider now refuses anything else, so the fake has the real shape.
SECRET = "whsec_test_endpoint_secret_not_a_real_credential"


def sign(body: bytes, *, secret: str = SECRET, timestamp: int | None = None, scheme: str = "v1") -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},{scheme}={digest}"


def event_body(
    event_type: str = "payment_intent.succeeded",
    *,
    event_id="evt_test_1",
    intent_id="pi_test_1",
    amount=500,
    amount_received=500,
    currency="usd",
    object_type="payment_intent",
    extra=None,
) -> bytes:
    """``extra`` merges further PaymentIntent fields (last_payment_error, cancellation_reason...)."""
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "livemode": False,
            "data": {
                "object": {
                    "id": intent_id,
                    "object": object_type,
                    "amount": amount,
                    "amount_received": amount_received,
                    "currency": currency,
                    **(extra or {}),
                }
            },
        }
    ).encode()


@pytest.fixture
def provider() -> StripePaymentProvider:
    return StripePaymentProvider(webhook_secret=SECRET)


def parse(provider, body: bytes, **sign_kwargs) -> WebhookEvent:
    return provider.verify_and_parse_webhook(body, sign(body, **sign_kwargs))


# --------------------------------------------------------------------------- mapping


def test_is_a_payment_provider(provider):
    assert isinstance(provider, PaymentProvider)


def test_succeeded_maps_to_completed_using_amount_received(provider):
    body = event_body("payment_intent.succeeded", amount=500, amount_received=500)
    assert parse(provider, body) == WebhookEvent(
        provider_event_id="evt_test_1",
        provider_transaction_reference="pi_test_1",
        status="Completed",
        amount=Money("5.00", "USD"),
    )


def test_succeeded_reports_amount_received_not_requested_amount(provider):
    # A short receipt must surface as a different amount so the completion
    # service routes it to UnderReview (FR-009), never as the requested amount.
    body = event_body("payment_intent.succeeded", amount=500, amount_received=400)
    assert parse(provider, body).amount == Money("4.00", "USD")


@pytest.mark.parametrize(
    "event_type, expected_status",
    [
        ("payment_intent.processing", "Processing"),
        ("payment_intent.requires_action", "RequiresAction"),
    ],
)
def test_non_terminal_events_use_requested_amount(provider, event_type, expected_status):
    body = event_body(event_type, amount=1250, amount_received=0)
    event = parse(provider, body)
    assert event.status == expected_status
    assert event.amount == Money("12.50", "USD")


def test_payment_failed_is_a_failed_attempt_not_a_failed_payment(provider):
    # T034i (human decision 2026-09-19, option A). Stripe sends payment_failed for EVERY failed
    # attempt and leaves the payment payable, so it must not end the top-up: a decline followed by
    # a successful retry on the same payment was charged and never credited.
    body = event_body("payment_intent.payment_failed", amount=500, amount_received=0)
    event = parse(provider, body)
    assert event.status == ATTEMPT_FAILED
    assert event.status != "Failed"
    assert event.amount == Money("5.00", "USD")


def test_a_failed_attempt_carries_stripes_error_code(provider):
    body = event_body(
        "payment_intent.payment_failed",
        extra={"last_payment_error": {"code": "card_declined", "decline_code": "generic_decline",
                                      "message": "Your card was declined."}},
    )
    assert parse(provider, body).failure_code == "card_declined"


@pytest.mark.parametrize(
    "error",
    [None, {}, {"code": None}, {"code": 123}, {"code": ""}, {"code": "x" * 65}, {"code": "has space"},
     {"code": "semi;colon"}, "not-a-dict"],
)
def test_an_unusable_error_code_is_dropped_not_stored(provider, error):
    extra = {} if error is None else {"last_payment_error": error}
    body = event_body("payment_intent.payment_failed", extra=extra)
    assert parse(provider, body).failure_code is None


def test_the_error_message_is_never_carried(provider):
    body = event_body(
        "payment_intent.payment_failed",
        extra={"last_payment_error": {"code": "card_declined", "message": "Card ending 0002 was declined."}},
    )
    assert "0002" not in repr(parse(provider, body))


def test_a_canceled_payment_is_the_terminal_failure(provider):
    body = event_body(
        "payment_intent.canceled", amount=500, amount_received=0,
        extra={"cancellation_reason": "abandoned"},
    )
    event = parse(provider, body)
    assert event.status == "Failed"
    assert event.failure_code == "payment_canceled_abandoned"
    assert event.amount == Money("5.00", "USD")


@pytest.mark.parametrize("reason", [None, "", 5, "has space", "x" * 65])
def test_a_canceled_payment_without_a_usable_reason_still_fails_with_a_plain_code(provider, reason):
    extra = {} if reason is None else {"cancellation_reason": reason}
    body = event_body("payment_intent.canceled", amount_received=0, extra=extra)
    event = parse(provider, body)
    assert (event.status, event.failure_code) == ("Failed", "payment_canceled")


@pytest.mark.parametrize(
    "minor_units, expected",
    [(1, "0.01"), (50, "0.50"), (100, "1.00"), (123456, "1234.56")],
)
def test_minor_units_convert_exactly_to_dollars(provider, minor_units, expected):
    body = event_body("payment_intent.processing", amount=minor_units)
    assert parse(provider, body).amount == Money(expected, "USD")


# --------------------------------------------------------------------------- authenticity


def test_wrong_secret_is_rejected(provider):
    body = event_body()
    with pytest.raises(WebhookVerificationError):
        provider.verify_and_parse_webhook(body, sign(body, secret="a-different-secret"))


def test_tampered_body_is_rejected(provider):
    body = event_body(amount=500, amount_received=500)
    header = sign(body)
    tampered = event_body(amount=500000, amount_received=500000)
    with pytest.raises(WebhookVerificationError):
        provider.verify_and_parse_webhook(tampered, header)


@pytest.mark.parametrize("header", [None, "", "garbage", "t=,v1=", "v1=abc"])
def test_missing_or_malformed_header_is_rejected(provider, header):
    with pytest.raises(WebhookVerificationError):
        provider.verify_and_parse_webhook(event_body(), header)


def test_timestamp_older_than_tolerance_is_rejected(provider):
    body = event_body()
    with pytest.raises(WebhookVerificationError):
        parse(provider, body, timestamp=int(time.time()) - 301)


def test_timestamp_inside_tolerance_is_accepted(provider):
    body = event_body()
    assert parse(provider, body, timestamp=int(time.time()) - 290).status == "Completed"


def test_v0_only_signature_is_ignored_and_rejected(provider):
    # Stripe sends a fake v0 signature for test events; only v1 may be trusted
    # (downgrade-attack protection).
    body = event_body()
    with pytest.raises(WebhookVerificationError):
        provider.verify_and_parse_webhook(body, sign(body, scheme="v0"))


def test_any_valid_v1_signature_is_accepted_while_secret_is_rolling(provider):
    body = event_body()
    ts = int(time.time())
    good = sign(body, timestamp=ts).split(",v1=")[1]
    header = f"t={ts},v1={'0' * 64},v1={good}"
    assert provider.verify_and_parse_webhook(body, header).status == "Completed"


def test_verification_happens_before_event_classification(provider):
    # A forged event of an ignorable type must be a verification failure, not a
    # quiet "ignored" acknowledgement.
    body = event_body("charge.succeeded")
    with pytest.raises(WebhookVerificationError):
        provider.verify_and_parse_webhook(body, sign(body, secret="a-different-secret"))


def test_secret_never_appears_in_error_messages(provider):
    body = event_body()
    with pytest.raises(WebhookVerificationError) as excinfo:
        provider.verify_and_parse_webhook(body, sign(body, secret="a-different-secret"))
    assert SECRET not in str(excinfo.value)


# --------------------------------------------------------------------------- ignored events


@pytest.mark.parametrize(
    "event_type",
    ["charge.succeeded", "payment_method.attached", "payment_intent.created", "customer.created"],
)
def test_authentic_but_unhandled_event_types_are_ignored_not_errors(provider, event_type):
    # Stripe retries any non-2xx for days, so a verified event this integration
    # does not act on must be distinguishable from a failure.
    with pytest.raises(IgnoredWebhookEventError):
        parse(provider, event_body(event_type))


# --------------------------------------------------------------------------- payload validation


def test_invalid_json_with_valid_signature_is_an_invalid_payload(provider):
    body = b"{not json"
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, body)


def test_non_usd_currency_is_rejected(provider):
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, event_body(currency="eur"))


@pytest.mark.parametrize("bad_amount", [0, -500, True, 5.5, "500", None, [500]])
def test_bad_amount_shapes_are_rejected(provider, bad_amount):
    body = event_body("payment_intent.processing", amount=bad_amount)
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, body)


def test_succeeded_with_zero_amount_received_is_rejected(provider):
    body = event_body("payment_intent.succeeded", amount=500, amount_received=0)
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, body)


def test_payment_intent_event_whose_object_is_not_a_payment_intent_is_rejected(provider):
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, event_body(object_type="charge"))


@pytest.mark.parametrize(
    "field, value",
    [
        ("event_id", ""),
        ("event_id", 123),
        ("event_id", "e" * 256),
        ("intent_id", ""),
        ("intent_id", None),
        ("intent_id", "p" * 256),
    ],
)
def test_bad_identifiers_are_rejected(provider, field, value):
    body = event_body(**{field: value})
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, body)


def test_missing_data_object_is_rejected(provider):
    body = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded", "data": {}}).encode()
    with pytest.raises(InvalidWebhookPayloadError):
        parse(provider, body)


# --------------------------------------------------------------------------- configuration (fail closed)


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_missing_webhook_secret_fails_closed(secret):
    with pytest.raises(ProviderConfigurationError):
        StripePaymentProvider(webhook_secret=secret)


@pytest.mark.parametrize("tolerance", [0, -1])
def test_non_positive_tolerance_is_refused_because_zero_disables_the_replay_check(tolerance):
    with pytest.raises(ProviderConfigurationError):
        StripePaymentProvider(webhook_secret=SECRET, tolerance_seconds=tolerance)


# --------------------------------------------------------------------------- not yet implemented (fail closed)


def test_status_inquiry_is_explicitly_not_implemented_yet(provider):
    with pytest.raises(ProviderNotImplementedError):
        provider.get_status("pi_test_1")


def test_initiation_without_an_api_key_or_client_fails_closed(provider):
    # T034c: `provider` above has only a webhook secret. It can verify webhooks but
    # must refuse to create payments.
    with pytest.raises(ProviderConfigurationError):
        provider.initiate(_request())


def test_reversal_is_not_supported_yet(provider):
    assert provider.supports_reversal() is False
    with pytest.raises(ReversalNotSupportedError):
        provider.reverse("pi_test_1", Money("5.00", "USD"), "test")


# =========================================================================== T034c: initiation
#
# The provider talks to Stripe through an injected client shaped like
# ``stripe.StripeClient`` (``client.v1.payment_intents.create/retrieve``), so none of
# these tests touches the network. Field names and parameters follow Stripe's
# documented PaymentIntents API (https://docs.stripe.com/payments/ach-direct-debit/
# accept-a-payment): amount in minor units, ``usd``, ``payment_method_types``, and an
# idempotency key on the request options.

from types import SimpleNamespace  # noqa: E402

import stripe  # noqa: E402

from services.topup.provider import (  # noqa: E402
    InitiationRequest,
    InvalidProviderStateError,
    ProviderRejectedError,
    ProviderUnavailableError,
)

SECRET_VALUE = "pi_secret_value_must_never_leak"


class FakePaymentIntents:
    def __init__(self, *, create_result=None, retrieve_result=None, error=None):
        self.create_calls = []
        self.retrieve_calls = []
        self._create_result = create_result if create_result is not None else SimpleNamespace(
            id="pi_created_1", client_secret=SECRET_VALUE, status="requires_payment_method"
        )
        self._retrieve_result = retrieve_result
        self._error = error

    def create(self, params, options=None):
        self.create_calls.append((params, options))
        if self._error is not None:
            raise self._error
        return self._create_result

    def retrieve(self, intent, params=None, options=None):
        self.retrieve_calls.append(intent)
        if self._error is not None:
            raise self._error
        return self._retrieve_result


def _client(**kwargs):
    intents = FakePaymentIntents(**kwargs)
    return SimpleNamespace(v1=SimpleNamespace(payment_intents=intents)), intents


def _request(*, funding_method="card", amount="100.00", currency="USD", idem="tu_reference_1"):
    return InitiationRequest(
        internal_reference="tu_reference_1",
        wallet_id="00000000-0000-0000-0000-000000000001",
        gross_amount=Money(amount, currency),
        funding_method=funding_method,
        idempotency_key=idem,
    )


def _live(client) -> StripePaymentProvider:
    return StripePaymentProvider(webhook_secret=SECRET, client=client)


def test_card_initiation_creates_a_usd_payment_intent_and_returns_the_client_secret():
    client, intents = _client()
    result = _live(client).initiate(_request(funding_method="card", amount="100.00"))

    (params, options), = intents.create_calls
    assert params["amount"] == 10000  # minor units, exact
    assert params["currency"] == "usd"
    assert params["payment_method_types"] == ["card"]
    assert params["metadata"] == {"top_up_reference": "tu_reference_1"}
    assert result.provider_transaction_reference == "pi_created_1"
    assert result.client_secret == SECRET_VALUE
    assert result.requires_action is True


def test_bank_initiation_requests_the_us_bank_account_method():
    client, intents = _client()
    _live(client).initiate(_request(funding_method="bank_transfer"))
    (params, _), = intents.create_calls
    assert params["payment_method_types"] == ["us_bank_account"]


def test_the_idempotency_key_passed_to_stripe_is_the_one_the_caller_supplied():
    # The handler supplies the top-up's globally unique internal reference. Client
    # keys are per-wallet while Stripe's are account-wide, so they must never be
    # forwarded; this pins that the provider forwards exactly what it is given.
    client, intents = _client()
    _live(client).initiate(_request(idem="tu_unique_reference"))
    (_, options), = intents.create_calls
    assert options["idempotency_key"] == "tu_unique_reference"


@pytest.mark.parametrize(
    "amount, cents", [("0.50", 50), ("1.00", 100), ("25.10", 2510), ("1234.56", 123456)]
)
def test_amounts_convert_to_exact_minor_units(amount, cents):
    client, intents = _client()
    _live(client).initiate(_request(amount=amount))
    assert intents.create_calls[0][0]["amount"] == cents


@pytest.mark.parametrize("method", ["agent_cash", "mobile_money", "paypal", ""])
def test_unsupported_funding_methods_are_rejected_before_any_network_call(method):
    client, intents = _client()
    with pytest.raises(ProviderRejectedError):
        _live(client).initiate(_request(funding_method=method))
    assert intents.create_calls == []


def test_non_usd_is_rejected_before_any_network_call():
    client, intents = _client()
    with pytest.raises(ProviderRejectedError):
        _live(client).initiate(_request(currency="EUR"))
    assert intents.create_calls == []


def test_result_repr_does_not_contain_the_client_secret():
    client, _ = _client()
    result = _live(client).initiate(_request())
    assert SECRET_VALUE not in repr(result)
    assert SECRET_VALUE not in str(result)


def test_a_response_without_a_client_secret_is_rejected():
    client, _ = _client(create_result=SimpleNamespace(id="pi_x", client_secret=None, status="requires_payment_method"))
    with pytest.raises(ProviderRejectedError):
        _live(client).initiate(_request())


def test_a_response_without_a_payment_intent_id_is_rejected():
    client, _ = _client(create_result=SimpleNamespace(id="", client_secret=SECRET_VALUE, status="x"))
    with pytest.raises(ProviderRejectedError):
        _live(client).initiate(_request())


@pytest.mark.parametrize(
    "error, expected",
    [
        (stripe.InvalidRequestError("bad request", param="amount"), ProviderRejectedError),
        (stripe.AuthenticationError("bad key"), ProviderConfigurationError),
        (stripe.PermissionError("no permission"), ProviderConfigurationError),
        (stripe.APIConnectionError("network down"), ProviderUnavailableError),
        (stripe.RateLimitError("slow down"), ProviderUnavailableError),
        (stripe.APIError("stripe 500"), ProviderUnavailableError),
        (stripe.StripeError("something else"), ProviderUnavailableError),
    ],
)
def test_stripe_errors_map_to_definitive_or_retryable_outcomes(error, expected):
    # Definitive (Stripe refused the request) -> the top-up fails. Ambiguous or
    # transient -> retryable, which is safe because the request carries an
    # idempotency key.
    client, _ = _client(error=error)
    with pytest.raises(expected):
        _live(client).initiate(_request())


def test_error_messages_do_not_echo_stripe_detail_or_keys():
    client, _ = _client(error=stripe.AuthenticationError("Invalid API Key provided: sk_test_abc123"))
    with pytest.raises(ProviderConfigurationError) as excinfo:
        _live(client).initiate(_request())
    assert "sk_test_abc123" not in str(excinfo.value)


def test_provider_repr_does_not_contain_the_api_key():
    provider = StripePaymentProvider(webhook_secret=SECRET, api_key="sk_test_should_not_appear")
    assert "sk_test_should_not_appear" not in repr(provider)


def test_provider_name_is_stripe():
    assert StripePaymentProvider(webhook_secret=SECRET).name == "stripe"


# --------------------------------------------------------------------------- retrieve_client_secret


@pytest.mark.parametrize("status", ["requires_payment_method", "requires_confirmation", "requires_action"])
def test_retrieve_returns_the_secret_while_the_customer_still_has_a_step_to_take(status):
    client, intents = _client(
        retrieve_result=SimpleNamespace(id="pi_1", client_secret=SECRET_VALUE, status=status)
    )
    assert _live(client).retrieve_client_secret("pi_1") == SECRET_VALUE
    assert intents.retrieve_calls == ["pi_1"]


@pytest.mark.parametrize("status", ["processing", "succeeded", "canceled", "requires_capture"])
def test_retrieve_refuses_once_the_customer_has_nothing_left_to_do(status):
    client, _ = _client(retrieve_result=SimpleNamespace(id="pi_1", client_secret=SECRET_VALUE, status=status))
    with pytest.raises(InvalidProviderStateError):
        _live(client).retrieve_client_secret("pi_1")


def test_retrieve_maps_stripe_errors_like_initiation():
    client, _ = _client(error=stripe.APIConnectionError("down"))
    with pytest.raises(ProviderUnavailableError):
        _live(client).retrieve_client_secret("pi_1")
    client, _ = _client(error=stripe.InvalidRequestError("no such intent", param="intent"))
    with pytest.raises(ProviderRejectedError):
        _live(client).retrieve_client_secret("pi_1")


def test_base_provider_default_is_not_implemented():
    from services.topup.mock_provider import MockPaymentProvider

    with pytest.raises(ProviderNotImplementedError):
        MockPaymentProvider().retrieve_client_secret("tx_1")


# =========================================================================== review fixes
#
# An independent review found that a wrong value in STRIPE_WEBHOOK_SECRET (this project
# had already mistakenly supplied a publishable key there) was accepted as long as it was
# non-empty. Initiation then created payments whose confirmations could never be
# authenticated: customers charged, wallets never credited. Signing secrets are documented
# to begin with "whsec_", so anything else is refused at construction, for both
# initiation and the webhook route.


@pytest.mark.parametrize(
    "bad_secret",
    [
        "pk_test_51ExamplePublishableKeyValue",   # the exact mistake already made once
        "sk_test_51ExampleSecretKeyValue",
        "rk_test_51ExampleRestrictedKeyValue",
        "plain-secret-value-1234567890",
        "whsec_",                                  # prefix only
        "whsec_short",                             # too short to be real
        "WHSEC_test_endpoint_secret_not_a_real",   # wrong case
        " whsec_test_endpoint_secret_not_a_real_credential",  # must be exactly as issued
    ],
)
def test_a_value_that_is_not_a_webhook_signing_secret_is_refused(bad_secret):
    with pytest.raises(ProviderConfigurationError) as excinfo:
        StripePaymentProvider(webhook_secret=bad_secret)
    # Never echo the configured value. (The message legitimately mentions the expected
    # "whsec_" prefix, so a value that is only that prefix cannot be checked this way.)
    if len(bad_secret.strip()) > len("whsec_"):
        assert bad_secret.strip() not in str(excinfo.value)


def test_a_well_formed_signing_secret_is_accepted():
    assert StripePaymentProvider(webhook_secret="whsec_" + "a1" * 16).name == "stripe"


@pytest.mark.parametrize("bad_key", ["pk_test_51ExamplePublishableKeyValue", "whsec_abcdefghijklmnop", "not-a-key", "   "])
def test_an_api_key_that_is_not_a_secret_key_is_refused(bad_key):
    with pytest.raises(ProviderConfigurationError) as excinfo:
        StripePaymentProvider(webhook_secret=SECRET, api_key=bad_key)
    assert bad_key.strip() not in str(excinfo.value) or not bad_key.strip()


@pytest.mark.parametrize("good_key", ["sk_test_51ExampleKey", "sk_live_51ExampleKey", "rk_test_51ExampleKey"])
def test_secret_and_restricted_keys_are_accepted(good_key):
    StripePaymentProvider(webhook_secret=SECRET, api_key=good_key)


# --------------------------------------------------------------------------- cancel


class FakeCancelIntents(FakePaymentIntents):
    """create/retrieve from FakePaymentIntents plus cancel()."""

    def __init__(self, *, cancel_error=None, retrieve_result=None):
        super().__init__(retrieve_result=retrieve_result)
        self.cancel_calls = []
        self._cancel_error = cancel_error

    def cancel(self, intent, params=None, options=None):
        self.cancel_calls.append(intent)
        if self._cancel_error is not None:
            raise self._cancel_error
        return SimpleNamespace(id=intent, status="canceled")


def _cancel_provider(**kwargs):
    intents = FakeCancelIntents(**kwargs)
    return _live(SimpleNamespace(v1=SimpleNamespace(payment_intents=intents))), intents


def test_cancel_cancels_the_payment_intent_at_stripe():
    provider, intents = _cancel_provider()
    provider.cancel("pi_1")
    assert intents.cancel_calls == ["pi_1"]


def test_cancelling_an_already_cancelled_payment_intent_is_success():
    # Recovery case: Stripe cancelled it but our own commit failed, so the caller retries.
    provider, _ = _cancel_provider(
        cancel_error=stripe.InvalidRequestError("already canceled", param="intent"),
        retrieve_result=SimpleNamespace(id="pi_1", status="canceled", client_secret="x"),
    )
    provider.cancel("pi_1")  # must not raise


@pytest.mark.parametrize("status", ["succeeded", "processing", "requires_capture"])
def test_a_payment_that_can_no_longer_be_cancelled_is_refused(status):
    # The customer may already have been (or be about to be) charged, so the top-up must
    # not be cancelled locally: a later verified success would find it terminal and be
    # ignored, leaving the customer charged and never credited.
    provider, _ = _cancel_provider(
        cancel_error=stripe.InvalidRequestError("cannot cancel", param="intent"),
        retrieve_result=SimpleNamespace(id="pi_1", status=status, client_secret="x"),
    )
    with pytest.raises(ProviderRejectedError):
        provider.cancel("pi_1")


def test_cancel_maps_unavailable_and_credential_errors():
    provider, _ = _cancel_provider(cancel_error=stripe.APIConnectionError("down"))
    with pytest.raises(ProviderUnavailableError):
        provider.cancel("pi_1")
    provider, _ = _cancel_provider(cancel_error=stripe.AuthenticationError("bad key"))
    with pytest.raises(ProviderConfigurationError):
        provider.cancel("pi_1")


def test_cancel_without_an_api_key_or_client_fails_closed():
    with pytest.raises(ProviderConfigurationError):
        StripePaymentProvider(webhook_secret=SECRET).cancel("pi_1")


def test_base_provider_cancel_default_is_not_implemented():
    from services.topup.mock_provider import MockPaymentProvider

    with pytest.raises(ProviderNotImplementedError):
        MockPaymentProvider().cancel("tx_1")
