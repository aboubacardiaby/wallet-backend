"""Deterministic mock payment provider (T012; FR-006).

Used for development and automated tests until a production provider has
official documentation and sandbox credentials (T002 documented assumption,
spec.md: mock-first). "Deterministic" here means the provider transaction
reference is a pure function of the idempotency key — the same key always
maps to the same reference, independent of call order or which
MockPaymentProvider instance made the call — not merely deterministic
within one instance's own counter, which would not survive a retry against
a fresh instance.

sign() is exposed publicly so tests (and T015's webhook processing) can
construct validly-signed payloads. A real provider would never expose this;
it is safe only because this class is a test double, never a production path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import InvalidOperation
from typing import Dict

from services.topup.money import InvalidCurrencyError, Money
from services.topup.provider import (
    PROVIDER_ORIGINATED_STATUSES,
    InitiationRequest,
    InitiationResult,
    InvalidProviderStateError,
    InvalidWebhookPayloadError,
    PaymentProvider,
    ProviderIdempotencyConflictError,
    ProviderTransactionNotFoundError,
    ReversalAmountMismatchError,
    ReversalNotSupportedError,
    ReversalResult,
    StatusResult,
    WebhookEvent,
    WebhookVerificationError,
)

DEFAULT_WEBHOOK_SECRET = "mock-provider-webhook-secret"
MAX_WEBHOOK_IDENTIFIER_LENGTH = 255


def _request_fingerprint(request: InitiationRequest) -> tuple:
    return (
        request.internal_reference,
        request.wallet_id,
        request.gross_amount.amount,
        request.gross_amount.currency,
        request.funding_method,
        request.funding_token,
    )


class MockPaymentProvider(PaymentProvider):
    def __init__(self, webhook_secret: str = DEFAULT_WEBHOOK_SECRET):
        self._secret = webhook_secret.encode()
        self._transactions: Dict[str, dict] = {}

    def sign(self, raw_body: bytes) -> str:
        return hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()

    def _reference_for(self, idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        return f"mock_{digest}"

    def initiate(self, request: InitiationRequest) -> InitiationResult:
        reference = self._reference_for(request.idempotency_key)
        fingerprint = _request_fingerprint(request)

        existing = self._transactions.get(reference)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ProviderIdempotencyConflictError(
                    f"idempotency key {request.idempotency_key!r} was already used for a different request"
                )
            return InitiationResult(
                provider_transaction_reference=reference,
                status=existing["status"],
                requires_action=(existing["status"] == "RequiresAction"),
                failure_code=existing["failure_code"],
            )

        if request.funding_token == "force_requires_action":
            status = "RequiresAction"
        elif request.funding_token == "force_failure":
            status = "Failed"
        else:
            status = "Processing"
        failure_code = "simulated_failure" if status == "Failed" else None

        self._transactions[reference] = {
            "amount": request.gross_amount,
            "status": status,
            "fingerprint": fingerprint,
            "failure_code": failure_code,
        }

        return InitiationResult(
            provider_transaction_reference=reference,
            status=status,
            requires_action=(status == "RequiresAction"),
            failure_code=failure_code,
        )

    def get_status(self, provider_transaction_reference: str) -> StatusResult:
        tx = self._transactions.get(provider_transaction_reference)
        if tx is None:
            raise ProviderTransactionNotFoundError(provider_transaction_reference)
        return StatusResult(
            provider_transaction_reference=provider_transaction_reference,
            status=tx["status"],
            amount=tx["amount"],
        )

    def verify_and_parse_webhook(self, raw_body: bytes, signature: str) -> WebhookEvent:
        expected = self.sign(raw_body)
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("signature does not match payload")

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidWebhookPayloadError("webhook payload is not valid JSON") from exc

        try:
            event_id = payload["event_id"]
            transaction_id = payload["transaction_id"]
            status = payload["status"]
            amount_value = payload["amount"]
            currency = payload["currency"]
        except (KeyError, TypeError) as exc:
            raise InvalidWebhookPayloadError("webhook payload is missing a required field") from exc

        if not isinstance(event_id, str) or not (0 < len(event_id) <= MAX_WEBHOOK_IDENTIFIER_LENGTH):
            raise InvalidWebhookPayloadError("webhook event_id must be a non-empty bounded string")
        if not isinstance(transaction_id, str) or not (0 < len(transaction_id) <= MAX_WEBHOOK_IDENTIFIER_LENGTH):
            raise InvalidWebhookPayloadError("webhook transaction_id must be a non-empty bounded string")
        if status not in PROVIDER_ORIGINATED_STATUSES:
            raise InvalidWebhookPayloadError(f"webhook status {status!r} is not a provider-originated status")

        # bool is a subclass of int in Python -- reject it explicitly before
        # the isinstance(..., int) check would otherwise let it through.
        # Non-scalar shapes (list/dict/None/float) are rejected the same way
        # rather than being handed to Decimal(str(...)), which raises a raw
        # decimal.InvalidOperation for several of them instead of a clean
        # provider-boundary error.
        if isinstance(amount_value, bool) or not isinstance(amount_value, (str, int)):
            raise InvalidWebhookPayloadError(
                f"webhook amount must be a string or integer, got {type(amount_value).__name__}"
            )

        try:
            amount = Money(amount_value, currency)
        except (InvalidCurrencyError, TypeError, ValueError, InvalidOperation) as exc:
            raise InvalidWebhookPayloadError("webhook amount/currency is invalid") from exc
        # "NaN" parses without raising (Decimal("NaN") is a valid, non-finite
        # Decimal) but must never reach the `<= 0` comparison below --
        # comparing a NaN Decimal raises InvalidOperation too.
        if not amount.amount.is_finite():
            raise InvalidWebhookPayloadError("webhook amount must be a finite number")
        if amount.amount <= 0:
            raise InvalidWebhookPayloadError("webhook amount must be positive")

        return WebhookEvent(
            provider_event_id=event_id,
            provider_transaction_reference=transaction_id,
            status=status,
            amount=amount,
        )

    def mark_completed(self, provider_transaction_reference: str) -> None:
        tx = self._transactions.get(provider_transaction_reference)
        if tx is None:
            raise ProviderTransactionNotFoundError(provider_transaction_reference)
        tx["status"] = "Completed"

    def reverse(self, provider_transaction_reference: str, amount: Money, reason: str) -> ReversalResult:
        if not self.supports_reversal():
            raise ReversalNotSupportedError(self.__class__.__name__)

        tx = self._transactions.get(provider_transaction_reference)
        if tx is None:
            raise ProviderTransactionNotFoundError(provider_transaction_reference)
        if tx["status"] != "Completed":
            raise InvalidProviderStateError(
                f"cannot reverse a transaction in status {tx['status']!r}"
            )
        stored_amount = tx["amount"]
        if amount.currency != stored_amount.currency or amount.amount != stored_amount.amount:
            raise ReversalAmountMismatchError(
                f"reversal amount {amount.amount} {amount.currency} does not match the "
                f"transaction's {stored_amount.amount} {stored_amount.currency}; "
                "only an exact full reversal is supported"
            )

        reversal_reference = "mock_rev_" + hashlib.sha256(
            f"{provider_transaction_reference}:{reason}".encode()
        ).hexdigest()[:24]
        tx["status"] = "Reversed"

        return ReversalResult(provider_reversal_reference=reversal_reference, status="Reversed")

    def supports_reversal(self) -> bool:
        return True
