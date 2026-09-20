"""Webhook HTTP endpoint for provider payment events (T015, T034b; FR-007, FR-008, FR-009).

This module provides the HTTP endpoint that receives webhook callbacks from payment providers.
The endpoint delegates to services/topup/webhook.py for the actual processing logic.

Providers that redeliver (Stripe retries any non-2xx for up to three days) treat a 2xx as
"delivered, stop retrying". The response status is therefore part of the financial
contract, not decoration:

* 202 -- handled, or authentic-but-deliberately-ignored, or an exact duplicate;
* 401 -- the signature did not verify (never 2xx: a forged or misconfigured delivery must
  not look delivered);
* 400 -- signature fine but the payload is unusable, or the provider is unsupported;
* 404 -- a verified event for a top-up we have no record of; not acknowledged, so the
  provider retries (the event may simply have raced our own commit) instead of dropping it;
* 500 -- anything that failed on our side; retry.
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from services.topup.mock_provider import MockPaymentProvider
from services.topup.provider import PaymentProvider, ProviderConfigurationError
from services.topup.stripe_provider import StripePaymentProvider
from services.topup.webhook import WebhookProcessingError, process_webhook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# FR-020: Body size limit to prevent abuse (1MB max payload)
MAX_WEBHOOK_BODY_SIZE = 1024 * 1024  # 1MB

# Each provider signs with its own header.
SIGNATURE_HEADERS = {
    "mock": "X-Webhook-Signature",
    "stripe": "Stripe-Signature",
}

# process_webhook() outcome -> HTTP status. Unknown outcomes fail toward a retry.
_OUTCOME_STATUS = {
    "success": status.HTTP_202_ACCEPTED,
    "acknowledged": status.HTTP_202_ACCEPTED,
    "duplicate_event": status.HTTP_202_ACCEPTED,
    "ignored_event": status.HTTP_202_ACCEPTED,
    "verification_failed": status.HTTP_401_UNAUTHORIZED,
    "malformed_payload": status.HTTP_400_BAD_REQUEST,
    "top_up_not_found": status.HTTP_404_NOT_FOUND,
    "completion_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "payload_processing_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _resolve_provider(provider_name: str) -> PaymentProvider:
    """Provider registry. Secrets come from the environment only, never from code."""
    if provider_name == "mock":
        # The mock provider signs with a secret that is hardcoded in this repository, so
        # it must never be reachable in production.
        if os.getenv("APP_ENV", "").strip().lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider {provider_name} not supported.",
            )
        return MockPaymentProvider()
    if provider_name == "stripe":
        try:
            # Same normalization as payment initiation (handlers/topup.py): the two must agree,
            # or payments could start while every webhook is refused. The API key is optional
            # here (verification needs only the signing secret); it is passed when configured so a
            # failed bank debit can cancel its provider payment (T034d). A malformed key fails
            # closed exactly as it does for initiation.
            return StripePaymentProvider(
                webhook_secret=(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip(),
                api_key=(os.getenv("STRIPE_SECRET_KEY") or "").strip() or None,
            )
        except ProviderConfigurationError:
            # Fail closed: a missing or malformed (not whsec_...) secret cannot authenticate anything.
            logger.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET is missing or not a whsec_ signing secret")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Provider not configured.",
            )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Provider {provider_name} not supported.",
    )


@router.post("/api/webhooks/payments/{provider_name}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    content_length: int = Header(None, alias="Content-Length"),
):
    """
    Receive and process webhook events from payment providers.

    FR-007: Verify webhook authenticity before processing
    FR-008: Store provider event for idempotency
    FR-009: Route verified events to completion service
    FR-020: Rate limiting and body size limits
    """
    # FR-020: Body size limit validation
    if content_length and content_length > MAX_WEBHOOK_BODY_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body too large. Maximum size: {MAX_WEBHOOK_BODY_SIZE} bytes"
        )

    # Read raw body for signature verification (it must not be re-serialized)
    raw_body = await request.body()

    if not raw_body:
        raise HTTPException(status_code=400, detail="Empty webhook payload")

    if len(raw_body) > MAX_WEBHOOK_BODY_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body too large. Maximum size: {MAX_WEBHOOK_BODY_SIZE} bytes"
        )

    provider = _resolve_provider(provider_name)

    header_name = SIGNATURE_HEADERS[provider_name]
    signature = request.headers.get(header_name)
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing {header_name} header",
        )

    try:
        result = await process_webhook(
            db=db,
            provider=provider,
            provider_name=provider_name,
            raw_body=raw_body,
            signature=signature,
        )
    except WebhookProcessingError as exc:
        logger.error(f"Webhook processing error: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Unexpected webhook processing error: {exc}")
        raise HTTPException(status_code=500, detail="Internal webhook processing error")

    outcome = result.get("status") if isinstance(result, dict) else None
    http_status = _OUTCOME_STATUS.get(outcome, status.HTTP_500_INTERNAL_SERVER_ERROR)
    if http_status == status.HTTP_202_ACCEPTED:
        return result

    # Non-2xx: say what class of failure it was, but never echo verification detail.
    detail = {
        status.HTTP_401_UNAUTHORIZED: "Webhook signature verification failed",
        status.HTTP_400_BAD_REQUEST: "Webhook payload is invalid",
        status.HTTP_404_NOT_FOUND: "Top-up not found for this event",
    }.get(http_status, "Webhook could not be processed")
    raise HTTPException(status_code=http_status, detail=detail)
