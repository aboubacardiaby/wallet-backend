"""Stripe payment provider (T034a webhooks, T034c initiation; FR-006, FR-007, FR-009).

Two jobs:

* **Webhooks (T034a):** authenticate Stripe webhooks and map the PaymentIntent events
  this integration acts on onto the domain's provider statuses.
* **Initiation (T034c):** create a PaymentIntent for a top-up and hand the customer's
  device the ``client_secret`` it needs to confirm the payment with Stripe's own SDK.
  Card numbers, bank details and mandate acceptance therefore never reach this backend
  (Constitution IV). Completion is still decided only by a verified webhook
  (Constitution II): creating or even confirming a PaymentIntent credits nothing.

Protocol facts come from Stripe's official documentation, not invention
(https://docs.stripe.com/webhooks; https://docs.stripe.com/payments/ach-direct-debit;
https://docs.stripe.com/api/payment_intents):

* ``Stripe-Signature`` carries ``t=<unix>`` and one or more ``v1=<hex>``
  HMAC-SHA256 signatures over ``"<t>.<raw body>"`` keyed by the endpoint's
  signing secret. Only ``v1`` is trusted. Verification is delegated to the
  official SDK (``stripe.Webhook.construct_event``) rather than re-implementing
  the cryptography here.
* The timestamp tolerance defaults to five minutes. Zero disables the replay
  check entirely, so a non-positive tolerance is refused at construction.
* Events are unordered and may repeat; identity is the event id (``evt_...``),
  which the webhook processor already stores for idempotency (FR-008).
* ACH Direct Debit is a delayed-notification method: ``payment_intent.succeeded``
  is the fulfillment signal and can arrive days after initiation. A bank can
  still return the debit after ``succeeded``; that is a dispute event handled
  by T034e, not here.
* PaymentIntent amounts are integers in minor units. This adapter supports USD
  only; any other currency is rejected rather than guessed at (zero-decimal
  currencies would silently mis-scale).
* Idempotency keys are scoped to the whole Stripe account, and Stripe replays the
  original response for a repeated key (for at least 24 hours). The caller must
  therefore supply a key that is unique per top-up across all wallets; the top-up's
  internal reference is used, never a client-supplied key.

Constructing the provider requires the webhook secret even for initiation, on
purpose: a payment must never be created that this system could not later confirm.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

import stripe

from services.topup.money import InvalidCurrencyError, Money
from services.topup.provider import (
    ATTEMPT_FAILED,
    IgnoredWebhookEventError,
    InitiationRequest,
    InitiationResult,
    InvalidProviderStateError,
    InvalidWebhookPayloadError,
    PaymentProvider,
    ProviderConfigurationError,
    ProviderError,
    ProviderNotImplementedError,
    ProviderRejectedError,
    ProviderUnavailableError,
    ReversalNotSupportedError,
    ReversalResult,
    StatusResult,
    WebhookEvent,
    WebhookVerificationError,
)

# Stripe documents webhook signing secrets as beginning with "whsec_"
# (https://docs.stripe.com/webhooks). Anything else, notably a publishable or secret API key
# pasted into the wrong setting, could never authenticate a webhook, so it is refused up front
# rather than allowing payments that could never be confirmed.
WEBHOOK_SECRET_PREFIX = "whsec_"
MIN_WEBHOOK_SECRET_LENGTH = len(WEBHOOK_SECRET_PREFIX) + 8
API_KEY_PREFIXES = ("sk_", "rk_")  # secret and restricted keys

DEFAULT_TOLERANCE_SECONDS = 300
# Bounded network behavior: a customer's request must not hang on a stuck call, and
# transient failures are retried by the SDK (safe: every create carries an idempotency key).
REQUEST_TIMEOUT_SECONDS = 20
MAX_NETWORK_RETRIES = 2
MAX_IDENTIFIER_LENGTH = 255
SUPPORTED_CURRENCY = "usd"

# Our funding method -> Stripe payment_method_types.
_PAYMENT_METHOD_TYPES = {
    "card": ["card"],
    "bank_transfer": ["us_bank_account"],
}

# PaymentIntent statuses in which the customer still has something to do on-device.
_CUSTOMER_ACTIONABLE = {"requires_payment_method", "requires_confirmation", "requires_action"}

# Stripe event type -> (domain status, PaymentIntent field carrying the amount).
# For a terminal success the money actually received is authoritative, so a
# short receipt surfaces as a mismatch (-> UnderReview) instead of being
# papered over with the requested amount.
#
# T034i (human decision 2026-09-19, option A): Stripe sends payment_failed for EVERY failed
# attempt and keeps the payment payable (requires_payment_method), so it is a failed ATTEMPT
# and never ends the top-up. The payment is over only when it is canceled, which is what makes
# the top-up Failed. (The Stripe endpoint or `stripe listen` must be subscribed to
# payment_intent.canceled for this to be delivered.)
_EVENT_MAP: dict[str, tuple[str, str]] = {
    "payment_intent.processing": ("Processing", "amount"),
    "payment_intent.requires_action": ("RequiresAction", "amount"),
    "payment_intent.succeeded": ("Completed", "amount_received"),
    "payment_intent.payment_failed": (ATTEMPT_FAILED, "amount"),
    "payment_intent.canceled": ("Failed", "amount"),
}

_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _safe_code(value: object) -> str | None:
    """A provider code we are willing to store: short and made of safe characters only."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _failure_code(event_type: str, obj: dict) -> str | None:
    if event_type == "payment_intent.payment_failed":
        error = obj.get("last_payment_error")
        # The error MESSAGE is never carried: it can mention card details.
        return _safe_code(error.get("code")) if isinstance(error, dict) else None
    if event_type == "payment_intent.canceled":
        reason = _safe_code(obj.get("cancellation_reason"))
        return f"payment_canceled_{reason}" if reason else "payment_canceled"
    return None


def _bounded_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not (0 < len(value) <= MAX_IDENTIFIER_LENGTH):
        raise InvalidWebhookPayloadError(f"stripe {name} must be a non-empty bounded string")
    return value


def _to_minor_units(amount: Decimal) -> int:
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ProviderRejectedError("amount is not a whole number of cents")
    return int(cents)


def _map_stripe_error(exc: stripe.StripeError) -> ProviderError:
    """Stripe error -> definitive or retryable outcome.

    The original message is deliberately not carried over: Stripe's text can quote
    fragments of credentials ("Invalid API Key provided: sk_test_...").
    """
    if isinstance(exc, (stripe.AuthenticationError, stripe.PermissionError)):
        return ProviderConfigurationError("stripe credentials were refused")
    if isinstance(exc, (stripe.InvalidRequestError, stripe.IdempotencyError)):
        return ProviderRejectedError("stripe rejected the request")
    # Connection errors, timeouts, rate limits and 5xx: the outcome may be unknown, and
    # repeating the request is safe because it carries an idempotency key.
    return ProviderUnavailableError("stripe is temporarily unavailable")


class StripePaymentProvider(PaymentProvider):
    name = "stripe"

    def __init__(
        self,
        webhook_secret: str | None,
        *,
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        api_key: str | None = None,
        client=None,
    ):
        if webhook_secret is None or not webhook_secret.strip():
            raise ProviderConfigurationError("a Stripe webhook signing secret is required")
        if (
            webhook_secret != webhook_secret.strip()
            or not webhook_secret.startswith(WEBHOOK_SECRET_PREFIX)
            or len(webhook_secret) < MIN_WEBHOOK_SECRET_LENGTH
        ):
            # The configured value is deliberately not echoed.
            raise ProviderConfigurationError(
                "STRIPE_WEBHOOK_SECRET is not a Stripe webhook signing secret (expected a whsec_... value)"
            )
        if api_key is not None and not api_key.strip().startswith(API_KEY_PREFIXES):
            raise ProviderConfigurationError(
                "STRIPE_SECRET_KEY is not a Stripe secret key (expected an sk_... or rk_... value)"
            )
        if tolerance_seconds <= 0:
            raise ProviderConfigurationError(
                "webhook timestamp tolerance must be positive; zero disables the replay check"
            )
        self._webhook_secret = webhook_secret
        self._tolerance_seconds = tolerance_seconds
        self._api_key = api_key
        self._client = client  # anything shaped like stripe.StripeClient; injected in tests

    def _get_client(self):
        if self._client is None:
            if not self._api_key or not self._api_key.strip():
                raise ProviderConfigurationError("a Stripe API key is required to create payments")
            self._client = stripe.StripeClient(
                self._api_key,
                max_network_retries=MAX_NETWORK_RETRIES,
                http_client=stripe.new_default_http_client(timeout=REQUEST_TIMEOUT_SECONDS),
            )
        return self._client

    def initiate(self, request: InitiationRequest) -> InitiationResult:
        method_types = _PAYMENT_METHOD_TYPES.get(request.funding_method)
        if method_types is None:
            raise ProviderRejectedError("funding method is not supported by Stripe")
        if request.gross_amount.currency.lower() != SUPPORTED_CURRENCY:
            raise ProviderRejectedError("only USD is supported")
        minor_units = _to_minor_units(request.gross_amount.amount)
        client = self._get_client()

        try:
            intent = client.v1.payment_intents.create(
                {
                    "amount": minor_units,
                    "currency": SUPPORTED_CURRENCY,
                    "payment_method_types": list(method_types),
                    # Non-sensitive correlation only; never card, bank or user data.
                    "metadata": {"top_up_reference": request.internal_reference},
                },
                {"idempotency_key": request.idempotency_key},
            )
        except stripe.StripeError as exc:
            raise _map_stripe_error(exc) from None

        intent_id = getattr(intent, "id", None)
        client_secret = getattr(intent, "client_secret", None)
        if not isinstance(intent_id, str) or not intent_id:
            raise ProviderRejectedError("stripe response did not include a payment intent id")
        if not isinstance(client_secret, str) or not client_secret:
            raise ProviderRejectedError("stripe response did not include a client secret")

        return InitiationResult(
            provider_transaction_reference=intent_id,
            status="Pending",
            requires_action=True,
            client_secret=client_secret,
        )

    def retrieve_client_secret(self, provider_transaction_reference: str) -> str:
        client = self._get_client()
        try:
            intent = client.v1.payment_intents.retrieve(provider_transaction_reference)
        except stripe.StripeError as exc:
            raise _map_stripe_error(exc) from None

        status = getattr(intent, "status", None)
        if status not in _CUSTOMER_ACTIONABLE:
            raise InvalidProviderStateError(f"payment intent is {status!r}; nothing left for the customer to do")
        client_secret = getattr(intent, "client_secret", None)
        if not isinstance(client_secret, str) or not client_secret:
            raise ProviderRejectedError("stripe response did not include a client secret")
        return client_secret

    def cancel(self, provider_transaction_reference: str) -> None:
        client = self._get_client()
        try:
            client.v1.payment_intents.cancel(provider_transaction_reference)
            return
        except stripe.InvalidRequestError:
            # Either it can no longer be cancelled (succeeded / processing) or it is already
            # cancelled (a retry after our own commit failed). Look at the real state.
            pass
        except stripe.StripeError as exc:
            raise _map_stripe_error(exc) from None

        try:
            intent = client.v1.payment_intents.retrieve(provider_transaction_reference)
        except stripe.StripeError as exc:
            raise _map_stripe_error(exc) from None
        if getattr(intent, "status", None) == "canceled":
            return
        raise ProviderRejectedError("the payment can no longer be cancelled")

    def get_status(self, provider_transaction_reference: str) -> StatusResult:
        raise ProviderNotImplementedError("Stripe status inquiry is not implemented yet")

    def supports_reversal(self) -> bool:
        return False

    def reverse(self, provider_transaction_reference: str, amount: Money, reason: str) -> ReversalResult:
        raise ReversalNotSupportedError(self.__class__.__name__)

    def verify_and_parse_webhook(self, raw_body: bytes, signature: str) -> WebhookEvent:
        # Authenticate first. Nothing below runs on a body that did not verify,
        # including deciding whether the event type is one we act on. The SDK is
        # used for verification only; the (now authenticated) bytes are parsed
        # here so this adapter does not depend on the SDK's event object model.
        try:
            stripe.Webhook.construct_event(
                raw_body, signature, self._webhook_secret, tolerance=self._tolerance_seconds
            )
        except stripe.SignatureVerificationError:
            # Do not chain: the SDK message can echo header contents.
            raise WebhookVerificationError("stripe signature verification failed") from None
        except ValueError as exc:
            # Signature verified but the body is not valid JSON.
            raise InvalidWebhookPayloadError("stripe webhook payload is not valid JSON") from exc

        try:
            event = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:  # pragma: no cover - construct_event parsed it
            raise InvalidWebhookPayloadError("stripe webhook payload is not valid JSON") from exc
        if not isinstance(event, dict):
            raise InvalidWebhookPayloadError("stripe webhook payload must be a JSON object")

        event_type = event.get("type")
        mapping = _EVENT_MAP.get(event_type) if isinstance(event_type, str) else None
        if mapping is None:
            raise IgnoredWebhookEventError(f"stripe event type {event_type!r} is not handled")
        status, amount_field = mapping

        event_id = _bounded_identifier(event.get("id"), "event id")

        data = event.get("data")
        obj = data.get("object") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            raise InvalidWebhookPayloadError("stripe webhook is missing data.object")
        if obj.get("object") != "payment_intent":
            raise InvalidWebhookPayloadError("stripe webhook data.object is not a payment_intent")
        intent_id = _bounded_identifier(obj.get("id"), "payment intent id")

        currency = obj.get("currency")
        if not isinstance(currency, str) or currency.lower() != SUPPORTED_CURRENCY:
            raise InvalidWebhookPayloadError("stripe webhook currency is not supported")

        minor_units = obj.get(amount_field)
        # bool is an int subclass; floats and strings are rejected outright.
        if isinstance(minor_units, bool) or not isinstance(minor_units, int):
            raise InvalidWebhookPayloadError(f"stripe {amount_field} must be an integer")
        if minor_units <= 0:
            raise InvalidWebhookPayloadError(f"stripe {amount_field} must be positive")

        try:
            amount = Money(Decimal(minor_units) / Decimal(100), "USD")
        except InvalidCurrencyError as exc:  # pragma: no cover - constant currency
            raise InvalidWebhookPayloadError("stripe webhook amount/currency is invalid") from exc

        return WebhookEvent(
            provider_event_id=event_id,
            provider_transaction_reference=intent_id,
            status=status,
            amount=amount,
            failure_code=_failure_code(event_type, obj),
        )
