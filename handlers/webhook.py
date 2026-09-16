"""Webhook HTTP endpoint for provider payment events (T015; FR-007, FR-008, FR-009).

This module provides the HTTP endpoint that receives webhook callbacks from payment providers.
The endpoint delegates to services/topup/webhook.py for the actual processing logic.
"""
import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from models.topup import ProviderEvent
from services.topup.mock_provider import MockPaymentProvider
from services.topup.webhook import WebhookProcessingError, process_webhook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# FR-020: Body size limit to prevent abuse (1MB max payload)
MAX_WEBHOOK_BODY_SIZE = 1024 * 1024  # 1MB


@router.post("/api/webhooks/payments/{provider_name}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_webhook_signature: str = Header(..., alias="X-Webhook-Signature"),  # CRITICAL FIX: Signature now required
    content_length: int = Header(None, alias="Content-Length"),
):
    """
    Receive and process webhook events from payment providers.

    Args:
        provider_name: Name of the provider (e.g., "mock", "stripe")
        request: FastAPI request object
        db: Database session
        x_webhook_signature: Webhook signature from provider
        x_webhook_timestamp: Webhook timestamp from provider
        content_length: Content length header for body size validation

    Returns:
        Processing result dictionary

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

    # Read raw body for signature verification
    raw_body = await request.body()

    if not raw_body:
        raise HTTPException(status_code=400, detail="Empty webhook payload")

    if len(raw_body) > MAX_WEBHOOK_BODY_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body too large. Maximum size: {MAX_WEBHOOK_BODY_SIZE} bytes"
        )

    # CRITICAL FIX: Remove hardcoded placeholder secret - provider interface doesn't use it
# The provider's verify_and_parse_webhook() only needs raw_body and signature
# Provider-specific secrets are handled internally by the provider implementation

    # Create provider instance based on provider_name
    # For now, only mock provider is available per T002 spec assumption
    if provider_name == "mock":
        provider = MockPaymentProvider()
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {provider_name} not supported. Only 'mock' is currently available."
        )

    # Process the webhook
    try:
        result = await process_webhook(
            db=db,
            provider=provider,
            provider_name=provider_name,
            raw_body=raw_body,
            signature=x_webhook_signature,
        )
        return result
    except WebhookProcessingError as exc:
        logger.error(f"Webhook processing error: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Unexpected webhook processing error: {exc}")
        raise HTTPException(status_code=500, detail="Internal webhook processing error")