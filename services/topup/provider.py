"""Top-up payment provider abstraction (T012; FR-006).

FR-006 requires calling providers through an abstraction supporting
initiation, status inquiry, webhook verification/parsing, and reversal
where supported. Verification and parsing are combined into one
verify_and_parse_webhook() call rather than two separate methods: a caller
that could parse before (or without) verifying would violate FR-007
("verify webhook authenticity ... before changing financial state") by
construction. This is a deliberate change from the two-step shape sketched
in T006's tests/test_topup_provider_fixtures.py.

Status values returned by a provider reuse the domain vocabulary from
services/topup/state_machine.py (Processing, RequiresAction, Completed,
Failed) rather than a second parallel enum — a provider can only ever
originate the subset of states caused by an external event; Created,
Pending, Expired, Cancelled, UnderReview, and Reversed are internal-only
transitions the application layer (T014) drives itself.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from services.topup.money import Money

PROVIDER_ORIGINATED_STATUSES = ("Processing", "RequiresAction", "Completed", "Failed")

# A provider reporting that one payment ATTEMPT failed (a declined card) while the payment
# itself stays payable and the customer can retry. Deliberately not a domain status and not
# in PROVIDER_ORIGINATED_STATUSES: it never changes a top-up's state (T034i, human decision
# 2026-09-19). Treating it as the terminal "Failed" charged customers whose retry then
# succeeded and never credited them.
ATTEMPT_FAILED = "AttemptFailed"


class ProviderError(Exception):
    pass


class ProviderTransactionNotFoundError(ProviderError):
    pass


class InvalidProviderStateError(ProviderError):
    pass


class WebhookVerificationError(ProviderError):
    pass


class ProviderIdempotencyConflictError(ProviderError):
    pass


class ReversalAmountMismatchError(ProviderError):
    pass


class InvalidWebhookPayloadError(ProviderError):
    pass


class ReversalNotSupportedError(ProviderError):
    pass


class IgnoredWebhookEventError(ProviderError):
    """An authentic webhook of a type this integration does not act on.

    Raised only after verification succeeds. Callers acknowledge it with a 2xx
    and change no financial state: providers such as Stripe retry any non-2xx
    response for days, so it must not be reported as a failure.
    """


class ProviderConfigurationError(ProviderError):
    """A provider was constructed with missing or unsafe configuration."""


class ProviderNotImplementedError(ProviderError):
    """The provider does not implement this operation yet (fails closed)."""


class ProviderRejectedError(ProviderError):
    """The provider definitively refused the request (or it can never be valid).

    Nothing was created at the provider, so the top-up can safely be failed.
    """


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached, or its outcome is unknown.

    Retryable: initiation requests carry an idempotency key, so repeating one never
    creates a second payment.
    """


@dataclass(frozen=True)
class InitiationRequest:
    internal_reference: str
    wallet_id: str
    gross_amount: Money
    funding_method: str
    idempotency_key: str
    funding_token: Optional[str] = None


@dataclass(frozen=True)
class InitiationResult:
    provider_transaction_reference: str
    status: str
    requires_action: bool = False
    action_details: Optional[str] = None
    failure_code: Optional[str] = None
    # A secret the customer's device needs to complete the payment with the provider's
    # own SDK (Stripe PaymentIntent client secret). It can complete a charge, so it is
    # excluded from repr() to keep it out of logs and tracebacks, and it is never stored.
    client_secret: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class StatusResult:
    provider_transaction_reference: str
    status: str
    amount: Money


@dataclass(frozen=True)
class WebhookEvent:
    provider_event_id: str
    provider_transaction_reference: str
    status: str
    amount: Money
    # A short, sanitized provider error/cancellation code (never a message): recorded on a
    # failed attempt (ATTEMPT_FAILED) or a terminal failure. None when the provider gave none.
    failure_code: Optional[str] = None


@dataclass(frozen=True)
class ReversalResult:
    provider_reversal_reference: str
    status: str


class PaymentProvider(ABC):
    """Abstract provider contract. See module docstring for design rationale."""

    @abstractmethod
    def initiate(self, request: InitiationRequest) -> InitiationResult:
        ...

    @abstractmethod
    def get_status(self, provider_transaction_reference: str) -> StatusResult:
        ...

    @abstractmethod
    def verify_and_parse_webhook(self, raw_body: bytes, signature: str) -> WebhookEvent:
        """Verify authenticity first; only ever parse a body that verified."""
        ...

    @abstractmethod
    def reverse(self, provider_transaction_reference: str, amount: Money, reason: str) -> ReversalResult:
        ...

    def retrieve_client_secret(self, provider_transaction_reference: str) -> str:
        """Re-fetch the customer-side secret for an already-initiated payment.

        Optional: only providers whose customer completes payment on-device need it.
        Raises InvalidProviderStateError when the customer has nothing left to do.
        """
        raise ProviderNotImplementedError(self.__class__.__name__)

    def cancel(self, provider_transaction_reference: str) -> None:
        """Cancel an initiated payment at the provider so it can no longer be completed.

        Returns normally when the payment is (now, or already) cancelled. Raises
        ProviderRejectedError when it can no longer be cancelled (the customer may already
        have been charged), in which case the caller MUST NOT cancel its own record: a
        later verified success would find that record terminal and be ignored.
        Optional: only providers with a customer-completed payment need it.
        """
        raise ProviderNotImplementedError(self.__class__.__name__)

    @abstractmethod
    def supports_reversal(self) -> bool:
        ...
