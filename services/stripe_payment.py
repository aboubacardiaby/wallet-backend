"""
StripePayment Handler Service.
Handles Stripe payment processing for debit cards, credit cards, and ACH.
- Debit card: Uses TalencePaymentsAPI for real-time processing
- Credit card: Stub (not yet implemented)
- ACH: Stub (not yet implemented)
"""
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime

import httpx


class PaymentType(str, Enum):
    """Supported payment types."""
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    ACH = "ach"


class StripePaymentError(Exception):
    """Raised when Stripe payment processing fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        error_code: Optional[str] = None,
        requires_action: bool = False,
        client_secret: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.requires_action = requires_action
        self.client_secret = client_secret


@dataclass
class DebitCardInfo:
    """Debit card payment details."""
    payment_method_id: str  # Stripe PaymentMethod ID (pm_xxxx)
    cardholder_name: Optional[str] = None


@dataclass
class CreditCardInfo:
    """Credit card payment details (stub)."""
    payment_method_id: str
    cardholder_name: Optional[str] = None


@dataclass
class ACHInfo:
    """ACH payment details (stub)."""
    routing_number: str
    account_number: str
    account_type: str = "checking"  # "checking" | "savings"
    account_holder_name: str = ""


@dataclass
class CustomerInfo:
    """Customer information for payment processing."""
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    metadata: Optional[dict] = None


@dataclass
class StripePaymentResult:
    """Result from Stripe payment processing."""
    success: bool
    payment_type: PaymentType
    transaction_id: Optional[str] = None
    charge_id: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    amount_charged: Optional[float] = None
    currency: Optional[str] = None
    customer_name: Optional[str] = None
    merchant_name: Optional[str] = None
    timestamp: Optional[datetime] = None
    requires_action: bool = False
    client_secret: Optional[str] = None
    receipt_url: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None


# ── Configuration ─────────────────────────────────────────────────────────────


def _get_talence_api_url() -> str:
    """Get the TalencePaymentsAPI base URL from environment."""
    return os.getenv("TALENCE_PAYMENTS_API_URL", "https://talence-payments-api-525776555218.us-central1.run.app/")


def _get_merchant_name() -> str:
    """Get the merchant name from environment."""
    return os.getenv("STRIPE_MERCHANT_NAME", "Wallet Platform")


def _get_timeout() -> int:
    """Get the timeout in seconds from environment."""
    return int(os.getenv("TALENCE_PAYMENTS_API_TIMEOUT", "30"))


def _get_verify_ssl() -> bool:
    """Get SSL verification setting. Disable only for local dev with self-signed certs."""
    return os.getenv("TALENCE_PAYMENTS_API_VERIFY_SSL", "true").lower() != "false"


# ── Debit Card Processing (via TalencePaymentsAPI) ────────────────────────────


async def process_debit_card_payment(
    customer: CustomerInfo,
    card: DebitCardInfo,
    amount: float,
    currency: str = "usd",
    description: Optional[str] = None,
) -> StripePaymentResult:
    """
    Process a debit card payment through TalencePaymentsAPI.

    Args:
        customer: Customer information (name, email, phone)
        card: Debit card details (payment_method_id from Stripe.js)
        amount: Amount in dollars (e.g., 100.00 = $100.00)
        currency: Currency code (default: "usd")
        description: Optional payment description

    Returns:
        StripePaymentResult with payment outcome and details

    Raises:
        StripePaymentError: If the API call fails or payment is declined
    """
    base_url = _get_talence_api_url()
    timeout = _get_timeout()
    verify_ssl = _get_verify_ssl()
    merchant_name = _get_merchant_name()

    payload = {
        "paymentMethodId": card.payment_method_id,
        "amount": amount,
        "currency": currency.lower(),
        "customerName": customer.name,
        "customerEmail": customer.email,
        "merchantName": merchant_name,
        "description": description or f"Debit card payment for {customer.name}",
    }

    # Remove None values
    payload = {k: v for k, v in payload.items() if v is not None}

    async with httpx.AsyncClient(
        timeout=timeout,
        verify=verify_ssl,
        follow_redirects=True,
    ) as client:
        try:
            response = await client.post(
                f"{base_url}/api/payments/debit",
                json=payload,
            )
        except httpx.TimeoutException:
            raise StripePaymentError(
                "Payment service timeout - please try again",
                status_code=504,
                error_code="timeout",
            )
        except httpx.RequestError as exc:
            raise StripePaymentError(
                f"Failed to connect to payment service: {exc}",
                status_code=503,
                error_code="connection_error",
            )

    # Handle error responses
    if response.status_code >= 500:
        raise StripePaymentError(
            "Payment service unavailable",
            status_code=503,
            error_code="service_unavailable",
        )

    try:
        data = response.json()
    except Exception:
        raise StripePaymentError(            "Invalid response from payment service",
            status_code=502,
            error_code="invalid_response",
        )

    # Handle 402 Payment Required (card declined)
    if response.status_code == 402:
        raise StripePaymentError(
            data.get("error", "Payment declined"),
            status_code=402,
            error_code=data.get("code", "payment_declined"),
        )

    # Handle 400 Bad Request
    if response.status_code == 400:
        raise StripePaymentError(
            data.get("error", "Invalid request"),
            status_code=400,
            error_code=data.get("code", "invalid_request"),
        )

    # Parse timestamp
    timestamp = None
    if data.get("timestamp"):
        try:
            timestamp = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            timestamp = datetime.utcnow()

    # Build result
    result = StripePaymentResult(
        success=data.get("success", False),
        payment_type=PaymentType.DEBIT_CARD,
        transaction_id=data.get("transactionId"),
        charge_id=data.get("chargeId"),
        status=data.get("status"),
        message=data.get("message"),
        amount_charged=data.get("amountCharged"),
        currency=data.get("currency", "").upper(),
        customer_name=data.get("customerName"),
        merchant_name=data.get("merchantName"),
        timestamp=timestamp,
        requires_action=data.get("requiresAction", False),
        client_secret=data.get("clientSecret"),
        receipt_url=data.get("receiptUrl"),
    )

    # Handle 3DS required (202 Accepted)
    if response.status_code == 202 or result.requires_action:
        raise StripePaymentError(
            "Payment requires additional authentication (3D Secure)",
            status_code=202,
            error_code="requires_action",
            requires_action=True,
            client_secret=result.client_secret,
        )

    if not result.success:
        raise StripePaymentError(
            result.message or "Payment failed",
            status_code=400,
            error_code=result.status,
        )

    return result


# ── Credit Card Processing (Stub) ─────────────────────────────────────────────


async def process_credit_card_payment(
    customer: CustomerInfo,
    card: CreditCardInfo,
    amount: float,
    currency: str = "usd",
    description: Optional[str] = None,
) -> StripePaymentResult:
    """
    Process a credit card payment through Stripe.

    NOTE: This is a stub implementation. Credit card processing
    is not yet implemented.

    Args:
        customer: Customer information (name, email, phone)
        card: Credit card details (payment_method_id from Stripe.js)
        amount: Amount in dollars (e.g., 100.00 = $100.00)
        currency: Currency code (default: "usd")
        description: Optional payment description

    Returns:
        StripePaymentResult with payment outcome

    Raises:
        StripePaymentError: Credit card processing not implemented
    """
    raise StripePaymentError(
        "Credit card processing is not yet implemented. "
        "Please use debit card or ACH for payments.",
        status_code=501,
        error_code="not_implemented",
    )


# ── ACH Processing (Stub) ─────────────────────────────────────────────────────


async def process_ach_payment(
    customer: CustomerInfo,
    ach: ACHInfo,
    amount: float,
    currency: str = "usd",
    description: Optional[str] = None,
) -> StripePaymentResult:
    """
    Process an ACH payment through Stripe.

    NOTE: This is a stub implementation. ACH processing via Stripe
    is not yet implemented. Use the existing ACH service for ACH transfers.

    Args:
        customer: Customer information (name, email, phone)
        ach: ACH account details (routing, account number)
        amount: Amount in dollars (e.g., 100.00 = $100.00)
        currency: Currency code (default: "usd")
        description: Optional payment description

    Returns:
        StripePaymentResult with payment outcome

    Raises:
        StripePaymentError: ACH processing not implemented
    """
    raise StripePaymentError(
        "ACH processing via Stripe is not yet implemented. "
        "Please use the dedicated ACH endpoints for bank transfers.",
        status_code=501,
        error_code="not_implemented",
    )


# ── Unified Payment Handler ───────────────────────────────────────────────────


async def process_stripe_payment(
    payment_type: PaymentType,
    customer: CustomerInfo,
    amount: float,
    currency: str = "usd",
    description: Optional[str] = None,
    debit_card: Optional[DebitCardInfo] = None,
    credit_card: Optional[CreditCardInfo] = None,
    ach: Optional[ACHInfo] = None,
) -> StripePaymentResult:
    """
    Unified Stripe payment handler that routes to the appropriate processor.

    Args:
        payment_type: Type of payment (debit_card, credit_card, ach)
        customer: Customer information
        amount: Amount in dollars
        currency: Currency code
        description: Optional description
        debit_card: Debit card info (required for DEBIT_CARD type)
        credit_card: Credit card info (required for CREDIT_CARD type)
        ach: ACH info (required for ACH type)

    Returns:
        StripePaymentResult with payment outcome

    Raises:
        StripePaymentError: If payment fails or type is invalid
    """
    if payment_type == PaymentType.DEBIT_CARD:
        if not debit_card:
            raise StripePaymentError(
                "Debit card information is required",
                status_code=400,
                error_code="missing_card_info",
            )
        return await process_debit_card_payment(
            customer=customer,
            card=debit_card,
            amount=amount,
            currency=currency,
            description=description,
        )

    elif payment_type == PaymentType.CREDIT_CARD:
        if not credit_card:
            raise StripePaymentError(
                "Credit card information is required",
                status_code=400,
                error_code="missing_card_info",
            )
        return await process_credit_card_payment(
            customer=customer,
            card=credit_card,
            amount=amount,
            currency=currency,
            description=description,
        )

    elif payment_type == PaymentType.ACH:
        if not ach:
            raise StripePaymentError(
                "ACH account information is required",
                status_code=400,
                error_code="missing_ach_info",
            )
        return await process_ach_payment(
            customer=customer,
            ach=ach,
            amount=amount,
            currency=currency,
            description=description,
        )

    else:
        raise StripePaymentError(
            f"Unsupported payment type: {payment_type}",
            status_code=400,
            error_code="invalid_payment_type",
        )
