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
from dataclasses import dataclass
from typing import Optional

from services.topup.money import Money

PROVIDER_ORIGINATED_STATUSES = ("Processing", "RequiresAction", "Completed", "Failed")


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

    @abstractmethod
    def supports_reversal(self) -> bool:
        ...
