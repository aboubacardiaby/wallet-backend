"""
Card Payment Processor Service client.
Calls the CardPaymentProcessorService API for Stripe payment processing.
"""
import os
from dataclasses import dataclass
from typing import Optional

import httpx


class CardPaymentError(Exception):
    """Raised when the card payment processing fails."""

    def __init__(self, message: str, status_code: int = 400, error_step: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_step = error_step


@dataclass
class CardInfo:
    """Card details for payment processing."""
    number: str
    exp_month: int
    exp_year: int
    cvc: str
    cardholder_name: Optional[str] = None


@dataclass
class CustomerInfo:
    """Customer information for Stripe processing."""
    email: str
    name: Optional[str] = None
    phone: Optional[str] = None
    metadata: Optional[dict] = None


@dataclass
class CardPaymentResult:
    """Result from card payment processing."""
    success: bool
    customer_id: Optional[str] = None
    payment_intent_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    status: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None
    card_brand: Optional[str] = None
    card_last4: Optional[str] = None
    card_exp_month: Optional[int] = None
    card_exp_year: Optional[int] = None
    error_message: Optional[str] = None
    error_step: Optional[str] = None


def _get_base_url() -> str:
    """Get the CardPaymentProcessorService base URL from environment."""
    return os.getenv("CARD_PAYMENT_SERVICE_URL", "http://localhost:5200")


def _get_timeout() -> int:
    """Get the timeout in seconds from environment."""
    return int(os.getenv("CARD_PAYMENT_SERVICE_TIMEOUT", "30"))


def _get_verify_ssl() -> bool:
    """Get SSL verification setting. Disable only for dev with self-signed certs."""
    return os.getenv("CARD_PAYMENT_SERVICE_VERIFY_SSL", "true").lower() != "false"


async def process_card_payment(
    customer: CustomerInfo,
    card: CardInfo,
    amount_cents: int,
    currency: str = "usd",
    description: Optional[str] = None,
) -> CardPaymentResult:
    """
    Process a card payment through the CardPaymentProcessorService.

    Args:
        customer: Customer information (email, name, phone)
        card: Card details (number, exp_month, exp_year, cvc)
        amount_cents: Amount in cents (e.g., 1000 = $10.00)
        currency: Currency code (default: "usd")
        description: Optional payment description

    Returns:
        CardPaymentResult with payment outcome and details

    Raises:
        CardPaymentError: If the API call fails or payment is declined
    """
    base_url = _get_base_url()
    timeout = _get_timeout()

    payload = {
        "email": customer.email,
        "name": customer.name,
        "phone": customer.phone,
        "amountInCents": amount_cents,
        "currency": currency,
        "description": description,
        "card": {
            "number": card.number,
            "expMonth": card.exp_month,
            "expYear": card.exp_year,
            "cvc": card.cvc,
            "cardholderName": card.cardholder_name,
        },
    }

    # Remove None values from payload
    payload = {k: v for k, v in payload.items() if v is not None}
    if payload.get("card"):
        payload["card"] = {k: v for k, v in payload["card"].items() if v is not None}

    verify_ssl = _get_verify_ssl()

    async with httpx.AsyncClient(timeout=timeout, verify=verify_ssl) as client:
        try:
            response = await client.post(
                f"{base_url}/api/process/payment",
                json=payload,
            )
        except httpx.TimeoutException:
            raise CardPaymentError(
                "Payment service timeout - please try again",
                status_code=504,
            )
        except httpx.RequestError as exc:
            raise CardPaymentError(
                f"Failed to connect to payment service: {exc}",
                status_code=503,
            )

    if response.status_code >= 500:
        raise CardPaymentError(
            "Payment service unavailable",
            status_code=503,
        )

    try:
        data = response.json()
    except Exception:
        raise CardPaymentError(
            "Invalid response from payment service",
            status_code=502,
        )

    # Parse the response into CardPaymentResult
    card_info = data.get("card", {}) or {}
    result = CardPaymentResult(
        success=data.get("success", False),
        customer_id=data.get("customerId"),
        payment_intent_id=data.get("paymentIntentId"),
        payment_method_id=data.get("paymentMethodId"),
        status=data.get("status"),
        amount=data.get("amount"),
        currency=data.get("currency"),
        card_brand=card_info.get("brand"),
        card_last4=card_info.get("last4"),
        card_exp_month=card_info.get("expMonth"),
        card_exp_year=card_info.get("expYear"),
        error_message=data.get("errorMessage"),
        error_step=data.get("errorStep"),
    )

    if not result.success:
        raise CardPaymentError(
            result.error_message or "Payment failed",
            status_code=400 if response.status_code < 500 else response.status_code,
            error_step=result.error_step,
        )

    return result


async def get_payment_status(payment_intent_id: str) -> dict:
    """
    Get the status of a payment intent.

    Args:
        payment_intent_id: The Stripe payment intent ID

    Returns:
        Payment status information

    Raises:
        CardPaymentError: If the API call fails
    """
    base_url = _get_base_url()
    timeout = _get_timeout()

    verify_ssl = _get_verify_ssl()

    async with httpx.AsyncClient(timeout=timeout, verify=verify_ssl) as client:
        try:
            response = await client.get(
                f"{base_url}/api/process/status/{payment_intent_id}",
            )
        except httpx.TimeoutException:
            raise CardPaymentError(
                "Payment service timeout",
                status_code=504,
            )
        except httpx.RequestError as exc:
            raise CardPaymentError(
                f"Failed to connect to payment service: {exc}",
                status_code=503,
            )

    if response.status_code == 404:
        raise CardPaymentError(
            "Payment not found",
            status_code=404,
        )

    if response.status_code >= 400:
        raise CardPaymentError(
            "Failed to get payment status",
            status_code=response.status_code,
        )

    return response.json()
