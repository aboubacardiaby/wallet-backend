"""HTTP contracts for the wallet top-up API (T005, FR-001--FR-005, FR-015).

These models deliberately contain no persistence or provider behavior.  T013 owns
the routes which consume them.  Wallet identifiers come from the route path and
the idempotency key comes from the ``Idempotency-Key`` header.

Authorization contract for customer routes:

* a valid bearer token is always required;
* the authenticated user must own ``walletId``;
* an absent wallet, top-up, or cross-wallet lookup returns the same 404 error so
  callers cannot enumerate another customer's resources;
* cancellation additionally requires the top-up to belong to that wallet;
* responses never expose funding tokens/references or provider secrets.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=255),
]
MoneyAmount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
NonNegativeMoneyAmount = Annotated[
    Decimal, Field(ge=0, max_digits=18, decimal_places=2)
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FundingMethod(str, Enum):
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    MOBILE_MONEY = "mobile_money"
    AGENT_CASH = "agent_cash"


class TopUpStatus(str, Enum):
    CREATED = "Created"
    PENDING = "Pending"
    PROCESSING = "Processing"
    REQUIRES_ACTION = "RequiresAction"
    COMPLETED = "Completed"
    FAILED = "Failed"
    EXPIRED = "Expired"
    CANCELLED = "Cancelled"
    REVERSED = "Reversed"
    UNDER_REVIEW = "UnderReview"


class ErrorCode(str, Enum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    RESOURCE_NOT_FOUND = "resource_not_found"
    VALIDATION_ERROR = "validation_error"
    WALLET_INACTIVE = "wallet_inactive"
    LIMIT_EXCEEDED = "limit_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    UNSUPPORTED_FUNDING_METHOD = "unsupported_funding_method"
    INTERNAL_ERROR = "internal_error"


ERROR_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.WALLET_INACTIVE: 409,
    ErrorCode.LIMIT_EXCEEDED: 422,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.INVALID_STATE: 409,
    ErrorCode.UNSUPPORTED_CURRENCY: 422,
    ErrorCode.UNSUPPORTED_FUNDING_METHOD: 422,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ApiError(ContractModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    field: str | None = None
    correlation_id: str | None = None


class ErrorResponse(ContractModel):
    error: ApiError


class InitiateTopUpRequest(ContractModel):
    amount: MoneyAmount
    currency: CurrencyCode
    funding_method: FundingMethod
    funding_token: str | None = Field(default=None, min_length=1, max_length=512)
    funding_reference: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_binary_float_amount(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("amount must be sent as a decimal string or integer")
        return value

    @field_validator("funding_token", "funding_reference")
    @classmethod
    def reject_blank_funding_values(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


class InitiateTopUpHeaders(ContractModel):
    """Headers required by POST /wallets/{walletId}/top-ups."""

    idempotency_key: IdempotencyKey


class NextAction(ContractModel):
    type: Literal["none", "redirect", "display_instructions", "await_provider"]
    url: str | None = None
    instructions: str | None = Field(default=None, max_length=1000)
    expires_at: datetime | None = None


class FundingSummary(ContractModel):
    """Safe display metadata; secret funding inputs are intentionally absent."""

    method: FundingMethod
    provider: str | None = Field(default=None, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    last4: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}$")] | None = None


class TopUpResponse(ContractModel):
    id: UUID
    reference: str = Field(min_length=1, max_length=80)
    wallet_id: UUID
    status: TopUpStatus
    gross_amount: NonNegativeMoneyAmount
    fee_amount: NonNegativeMoneyAmount
    net_amount: NonNegativeMoneyAmount
    currency: CurrencyCode
    funding: FundingSummary
    next_action: NextAction | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class InitiateTopUpResponse(ContractModel):
    top_up: TopUpResponse
    idempotent_replay: bool = False


class TopUpDetailResponse(ContractModel):
    top_up: TopUpResponse


class TopUpHistoryResponse(ContractModel):
    items: list[TopUpResponse]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_next: bool


class CancelTopUpResponse(ContractModel):
    top_up: TopUpResponse


class ValidationIssue(ContractModel):
    field: str | None = None
    message: str


def error_response(
    code: ErrorCode,
    message: str,
    *,
    field: str | None = None,
    correlation_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Build the canonical status/body pair for a top-up API failure."""

    body = ErrorResponse(
        error=ApiError(
            code=code,
            message=message,
            field=field,
            correlation_id=correlation_id,
        )
    )
    return ERROR_STATUS_BY_CODE[code], body.model_dump(mode="json", exclude_none=True)


def resource_not_found(correlation_id: str | None = None) -> tuple[int, dict[str, Any]]:
    """Return the indistinguishable missing/cross-wallet response."""

    return error_response(
        ErrorCode.RESOURCE_NOT_FOUND,
        "Top-up resource not found",
        correlation_id=correlation_id,
    )
