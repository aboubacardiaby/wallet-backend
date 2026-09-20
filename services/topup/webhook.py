"""Webhook processing for verified, replay-safe, idempotent payment events (T015; FR-007, FR-008, FR-009).

This module handles the receiving side of provider webhooks:
- FR-007: Verify webhook authenticity using provider's protocol before any processing
- FR-008: Store provider event identity for deduplication and process idempotently
- FR-009: Route verified events to completion service for atomic ledger posting

Scope boundary (per T014's handoff): this module is responsible for transport-layer
concerns (verification, deduplication, event storage) and routing verified events to
the completion service. It does not perform ledger posting itself (FR-009's atomic
ledger completion is T014's responsibility via complete_verified_topup()).

Constitutional compliance:
- Constitution II: Only verified events can trigger state changes
- Constitution III: Uses database transactions for atomicity
- Constitution IV: Never stores or logs raw funding secrets
- Constitution IX: Uses deterministic mock provider for testing, no fabricated success
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.topup import ProviderEvent, TopUp
from services.topup.completion import (
    CompletionOutcome,
    InvalidCompletionStateError,
    TopUpNotFoundError,
    WalletNotFoundError,
    apply_verified_failure,
    apply_verified_progress,
    complete_verified_topup,
    record_payment_attempt_failure,
)
from services.topup.money import Money
from services.topup.provider import (
    ATTEMPT_FAILED,
    IgnoredWebhookEventError,
    InvalidWebhookPayloadError,
    PaymentProvider,
    WebhookEvent,
    WebhookVerificationError,
)
from utils.audit import log_audit

logger = logging.getLogger(__name__)


class WebhookProcessingError(Exception):
    """Base exception for webhook processing errors."""
    pass


class TopUpNotFoundErrorForWebhook(WebhookProcessingError):
    """Raised when a webhook references a non-existent top-up."""
    pass


class InvalidWebhookProcessingStateError(WebhookProcessingError):
    """Raised when webhook processing encounters an invalid state."""
    pass


def _sanitize_webhook_payload(raw_payload: bytes) -> dict:
    """
    Sanitize webhook payload by using an allowlist approach for minimal event summary.
    FR-022/Constitution IV: Never store raw funding tokens or secrets.
    
    HIGH FIX: Changed from denylist-based to allowlist-based sanitization to ensure
    only minimal, safe event data is persisted.
    """
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidWebhookPayloadError("Invalid webhook payload format") from exc

    # HIGH FIX: Allowlist of safe fields to keep - everything else is removed
    # Only keep minimal event metadata needed for audit/processing
    safe_fields = {
        "event_id",
        "provider_event_id",
        "event_type",
        "status",
        "amount",
        "currency",
        "provider_transaction_reference",
        "created_at",
        "updated_at",
        "timestamp",
        "provider",
    }

    def sanitize(obj):
        if isinstance(obj, dict):
            return {k: sanitize(v) if k.lower() in safe_fields else "***REDACTED***" for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize(item) for item in obj]
        else:
            return obj

    return sanitize(payload)


def _hash_payload(raw_payload: bytes) -> str:
    """Generate SHA-256 hash of raw payload for deduplication."""
    return hashlib.sha256(raw_payload).hexdigest()


async def _store_provider_event(
    db: AsyncSession,
    provider_name: str,
    provider_event_id: str,
    event_type: str,
    raw_payload: bytes,
    top_up_id: Optional[uuid.UUID] = None,
) -> ProviderEvent:
    """
    Store provider event for idempotency (FR-008).
    Uses database unique constraint for replay protection.
    Returns the existing event if duplicate with same payload hash,
    raises error if duplicate with different payload hash (tampering).
    """
    payload_hash = _hash_payload(raw_payload)
    sanitized_payload = _sanitize_webhook_payload(raw_payload)

    event = ProviderEvent(
        provider_name=provider_name,
        provider_event_id=provider_event_id,
        top_up_id=top_up_id,
        event_type=event_type,
        payload_hash=payload_hash,
        sanitized_payload=sanitized_payload,
        processing_status="received",
        received_at=datetime.now(timezone.utc),
    )

    db.add(event)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # Check if this is a duplicate event (same provider + event_id)
        existing = await db.scalar(
            select(ProviderEvent).where(
                ProviderEvent.provider_name == provider_name,
                ProviderEvent.provider_event_id == provider_event_id,
            )
        )
        if existing:
            # CRITICAL FIX: Compare payload hashes to detect tampering
            if existing.payload_hash != payload_hash:
                # Same event ID but different payload - potential tampering
                logger.error(
                    f"Payload hash mismatch for duplicate event {provider_event_id}: "
                    f"existing={existing.payload_hash}, new={payload_hash}"
                )
                raise WebhookProcessingError(
                    f"Duplicate event ID {provider_event_id} with different payload hash - potential tampering"
                ) from exc
            # Exact replay - same event ID and same payload hash
            # Return existing without mutating it
            return existing
        raise WebhookProcessingError("Failed to store provider event") from exc

    return event


async def _cancel_provider_payment_after_failure(db: AsyncSession, provider: PaymentProvider, top_up: TopUp) -> None:
    """Best-effort: cancel a bank payment whose top-up has just durably become Failed.

    Runs strictly AFTER the failure is committed, so a provider outage can never undo it. If
    the payment cannot be cancelled it stays payable at the provider; that is recorded so an
    operator can act, and a later verified success would be flagged by the late-success
    audit (topup_late_success_on_terminal_top_up) instead of being silently ignored.
    """
    reference = top_up.provider_transaction_reference
    try:
        await asyncio.to_thread(provider.cancel, reference)
        return
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded, never raised
        error_name = type(exc).__name__
        logger.error(
            "Top-up %s failed but its provider payment %s could not be cancelled (%s)",
            top_up.internal_reference, reference, error_name,
        )
    try:
        await log_audit(
            db,
            action="topup_provider_cancel_failed",
            resource_type="top_up",
            resource_id=str(top_up.id),
            details={"provider_transaction_reference": reference, "error": error_name},
        )
        await db.commit()
    except Exception:
        logger.exception("Failed to record the provider-cancel failure for top-up %s", top_up.id)
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to roll back after the provider-cancel audit failed")


async def process_webhook(
    db: AsyncSession,
    provider: PaymentProvider,
    provider_name: str,
    raw_body: bytes,
    signature: str,
) -> dict:
    """
    Process a verified webhook event (FR-007, FR-008, FR-009).

    Steps:
    1. Verify webhook signature using provider's protocol (FR-007)
    2. Parse webhook event to standard format (FR-007)
    3. Store provider event for idempotency (FR-008)
    4. Route to completion service based on event type (FR-009)

    Args:
        db: Database session
        provider: Payment provider instance for verification/parsing
        provider_name: Name of the provider (e.g., "mock", "stripe")
        raw_body: Raw webhook payload bytes
        signature: Webhook signature from request headers

    Returns:
        Processing result dictionary with status and details
    """
    # FR-007: Verify webhook authenticity before any processing
    try:
        # CRITICAL FIX: Provider interface expects 2 arguments, not 3
        webhook_event = provider.verify_and_parse_webhook(raw_body, signature)
    except WebhookVerificationError as exc:
        logger.warning(f"Webhook verification failed for provider {provider_name}: {exc}")
        await db.rollback()
        # HIGH FIX: Complete malformed-payload mapping with proper error categorization
        return {
            "status": "verification_failed",
            "error": str(exc),
            "error_type": "verification_error",
            "provider_event_id": None,
        }
    except IgnoredWebhookEventError as exc:
        # T034b: authentic, but not an event this integration acts on. Nothing is
        # stored and no state changes; the route acknowledges it with 2xx because a
        # retrying provider would otherwise redeliver it for days.
        logger.info(f"Ignoring authentic webhook for provider {provider_name}: {exc}")
        return {
            "status": "ignored_event",
            "error_type": None,
            "provider_event_id": None,
        }
    except InvalidWebhookPayloadError as exc:
        logger.error(f"Invalid webhook payload for provider {provider_name}: {exc}")
        await db.rollback()
        # HIGH FIX: Complete malformed-payload mapping for JSON decode errors
        return {
            "status": "malformed_payload",
            "error": str(exc),
            "error_type": "payload_error",
            "provider_event_id": None,
        }
    except Exception as exc:
        # HIGH FIX: Catch-all for unexpected payload errors with proper masking
        logger.error(f"Unexpected payload processing error for provider {provider_name}: {exc}")
        await db.rollback()
        return {
            "status": "payload_processing_error",
            "error": "Failed to process webhook payload",
            "error_type": "processing_error",
            "provider_event_id": None,
        }

    # FR-008: Store provider event for idempotency
    try:
        stored_event = await _store_provider_event(
            db,
            provider_name,
            webhook_event.provider_event_id,
            webhook_event.status,
            raw_body,
        )
    except WebhookProcessingError as exc:
        # CRITICAL FIX: Duplicate event with different payload hash - potential tampering
        await db.rollback()
        logger.error(f"Duplicate event with different payload hash: {exc}")
        raise
    except Exception as exc:
        # Other storage errors
        await db.rollback()
        logger.error(f"Failed to store provider event: {exc}")
        raise WebhookProcessingError("Failed to store provider event") from exc

    # CRITICAL FIX: Check if this was an exact replay (same event ID and payload hash)
    # The _store_provider_event function now returns the existing event without mutation
    # if it's an exact replay, so we can detect this by checking if it was already processed
    if hasattr(stored_event, 'processing_status') and stored_event.processing_status in ("processed", "ignored"):
        # This is an exact replay - return success without processing again
        return {
            "status": "duplicate_event",
            "provider_event_id": webhook_event.provider_event_id,
            "action": "ignored",
        }

    # Find the corresponding top-up using provider transaction reference
    top_up = await db.scalar(
        select(TopUp).where(
            TopUp.provider_name == provider_name,
            TopUp.provider_transaction_reference == webhook_event.provider_transaction_reference,
        )
    )

    if top_up is None:
        # Event references unknown transaction - store for audit but don't process
        stored_event.processing_status = "failed"
        stored_event.error_code = "top_up_not_found"
        stored_event.processed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.warning(
            f"Webhook event {webhook_event.provider_event_id} references "
            f"unknown transaction {webhook_event.provider_transaction_reference}"
        )
        return {
            "status": "top_up_not_found",
            "provider_event_id": webhook_event.provider_event_id,
            "provider_transaction_reference": webhook_event.provider_transaction_reference,
        }

    # Update event with top-up reference
    stored_event.top_up_id = top_up.id

    # FR-009: Route to completion service based on event status
    try:
        reported_amount = webhook_event.amount

        if webhook_event.status == "Completed":
            # Mark the event processed BEFORE calling the completion service,
            # not after. complete_verified_topup() owns and commits its own
            # atomic financial transaction internally (T014); marking the
            # event only after it returns created a real split-transaction
            # window -- a crash between T014's internal commit and this
            # function's own later commit would leave the top-up already
            # Completed (money moved) while the provider event still read
            # "received". Since this mutation and complete_verified_topup()'s
            # own changes share the same session, setting it first means
            # T014's internal commit durably persists both together, in one
            # transaction. If complete_verified_topup() raises, its own
            # internal rollback (or this function's outer rollback below)
            # discards this premature mutation along with everything else,
            # so a failure never leaves it stuck at "processed" either.
            stored_event.processing_status = "processed"
            stored_event.processed_at = datetime.now(timezone.utc)

            # Call completion service for verified completion
            outcome = await complete_verified_topup(
                db,
                top_up.id,
                reported_amount,
                provider_transaction_reference=webhook_event.provider_transaction_reference,
            )

            # Safety net only: complete_verified_topup()'s one internal path
            # that performs no mutation at all (an already-terminal top-up
            # whose reported amount matches) also performs no commit -- this
            # covers that case. Every other path already committed the
            # mutation above as part of T014's own transaction.
            await db.commit()

            return {
                "status": "success",
                "provider_event_id": webhook_event.provider_event_id,
                "top_up_id": str(top_up.id),
                "action": outcome.action,
                "top_up_status": top_up.status,
            }

        elif webhook_event.status == "Failed":
            # Same reasoning as the Completed branch above: mark the event
            # processed before calling apply_verified_failure(), so its own
            # internal commit (T019/T014's failure path) durably persists
            # both changes together instead of leaving a split-transaction
            # window between two separate commits.
            stored_event.processing_status = "processed"
            stored_event.processed_at = datetime.now(timezone.utc)

            # Call failure service for verified failure. The provider's own (sanitized) code is
            # kept when it gave one, e.g. payment_canceled_abandoned (T034i).
            outcome = await apply_verified_failure(
                db,
                top_up.id,
                failure_code=webhook_event.failure_code or "provider_failed",
                failure_message="Provider reported payment failure",
            )

            # Safety net only -- see the Completed branch above.
            await db.commit()

            return {
                "status": "success",
                "provider_event_id": webhook_event.provider_event_id,
                "top_up_id": str(top_up.id),
                "action": outcome.action,
                "top_up_status": top_up.status,
            }

        elif webhook_event.status == ATTEMPT_FAILED:
            # T034i (human decision 2026-09-19, option A): one payment ATTEMPT failed (a declined
            # card) but the payment stays payable, so the top-up is NOT ended. Recording it
            # only audits the attempt. Same atomicity rule as above: the event is marked
            # processed first so it shares record_payment_attempt_failure()'s own commit.
            stored_event.processing_status = "processed"
            stored_event.processed_at = datetime.now(timezone.utc)

            if top_up.funding_method == "bank_transfer":
                # T034d (human decision 2026-09-19): for a bank (ACH) top-up a failed debit is not
                # an instant retry (it can arrive days later, and trying again needs a fresh
                # authorization), so it ENDS the top-up as Failed and the provider payment is
                # cancelled so it can no longer be paid. Cards keep the attempt rule below.
                outcome = await apply_verified_failure(
                    db,
                    top_up.id,
                    failure_code=webhook_event.failure_code or "provider_failed",
                    failure_message="Provider reported payment failure",
                )
                # Safety net only, as above.
                await db.commit()
                if outcome.action == "failed":
                    await _cancel_provider_payment_after_failure(db, provider, top_up)
                return {
                    "status": "success",
                    "provider_event_id": webhook_event.provider_event_id,
                    "top_up_id": str(top_up.id),
                    "action": outcome.action,
                    "top_up_status": top_up.status,
                }

            outcome = await record_payment_attempt_failure(db, top_up.id, webhook_event.failure_code)

            # Safety net only, as above: the no-op path performs no commit of its own.
            await db.commit()

            return {
                "status": "acknowledged",
                "provider_event_id": webhook_event.provider_event_id,
                "top_up_id": str(top_up.id),
                "action": outcome.action,
                "provider_status": webhook_event.status,
            }

        else:
            # Non-terminal statuses (Processing, RequiresAction) -- T034b.
            # These used to be acknowledged without touching the top-up, so a later
            # verified success found it still Pending and failed with an illegal
            # transition. Now they advance it via apply_verified_progress(), which
            # moves no money and treats repeated/late events as no-ops. Same
            # atomicity rule as the branches above: mark the event processed
            # first so it rides along inside apply_verified_progress()'s own commit.
            stored_event.processing_status = "processed"
            stored_event.processed_at = datetime.now(timezone.utc)

            outcome = await apply_verified_progress(db, top_up.id, webhook_event.status)

            # Safety net only, as above: the no-op paths perform no commit of their own.
            await db.commit()

            return {
                "status": "acknowledged",
                "provider_event_id": webhook_event.provider_event_id,
                "top_up_id": str(top_up.id),
                "action": outcome.action,
                "provider_status": webhook_event.status,
            }

    except (TopUpNotFoundError, WalletNotFoundError, InvalidCompletionStateError) as exc:
        # CRITICAL FIX: Rollback current transaction, then record failure metadata in fresh transaction
        # Re-query event after rollback to avoid stale object mutation with real SQLAlchemy sessions
        # CRITICAL FIX: If re-query returns None (rollback removed the event), create a replacement failure record
        await db.rollback()
        
        # Start fresh transaction for failure metadata recording
        try:
            # Re-query the event to get a fresh object from the database
            fresh_event = await db.scalar(
                select(ProviderEvent).where(
                    ProviderEvent.provider_name == provider_name,
                    ProviderEvent.provider_event_id == webhook_event.provider_event_id
                )
            )
            
            if fresh_event:
                # Event still exists after rollback (unlikely but possible)
                fresh_event.error_code = type(exc).__name__
                fresh_event.processing_status = "failed"
                fresh_event.processed_at = datetime.now(timezone.utc)
            else:
                # CRITICAL FIX: Event was removed by rollback, create replacement failure record
                # This ensures we still have audit trail even when the initial event was rolled back.
                # ProviderEvent has no `event_data` field and requires `payload_hash`; WebhookEvent
                # (the parsed provider payload) has no `raw_body` -- the original raw bytes are only
                # available via this function's own `raw_body` parameter.
                failure_event = ProviderEvent(
                    provider_name=provider_name,
                    provider_event_id=webhook_event.provider_event_id,
                    top_up_id=top_up.id if top_up else None,
                    event_type="payment.webhook",  # Use default event type since WebhookEvent doesn't have this field
                    payload_hash=_hash_payload(raw_body),
                    sanitized_payload=_sanitize_webhook_payload(raw_body),
                    processing_status="failed",
                    error_code=type(exc).__name__,
                    processed_at=datetime.now(timezone.utc),
                )
                db.add(failure_event)
            
            await db.commit()
            
        except Exception as metadata_error:
            await db.rollback()
            logger.error(f"Failed to record failure metadata in fresh transaction: {metadata_error}")
            
        logger.error(f"Completion service error for top-up {top_up.id}: {exc}")
        return {
            "status": "completion_error",
            "provider_event_id": webhook_event.provider_event_id,
            "top_up_id": str(top_up.id),
            "error": type(exc).__name__,
        }

    except Exception as exc:
        # CRITICAL FIX: Rollback current transaction, then record failure metadata in fresh transaction
        # Re-query event after rollback to avoid stale object mutation with real SQLAlchemy sessions
        # CRITICAL FIX: If re-query returns None (rollback removed the event), create a replacement failure record
        await db.rollback()
        
        # Start fresh transaction for failure metadata recording
        try:
            # Re-query the event to get a fresh object from the database
            fresh_event = await db.scalar(
                select(ProviderEvent).where(
                    ProviderEvent.provider_name == provider_name,
                    ProviderEvent.provider_event_id == webhook_event.provider_event_id
                )
            )
            
            if fresh_event:
                # Event still exists after rollback (unlikely but possible)
                fresh_event.error_code = "internal_error"
                fresh_event.processing_status = "failed"
                fresh_event.processed_at = datetime.now(timezone.utc)
            else:
                # CRITICAL FIX: Event was removed by rollback, create replacement failure record
                # This ensures we still have audit trail even when the initial event was rolled back.
                # See the sibling except block above for why event_data/raw_body were wrong.
                failure_event = ProviderEvent(
                    provider_name=provider_name,
                    provider_event_id=webhook_event.provider_event_id,
                    top_up_id=top_up.id if top_up else None,
                    event_type="payment.webhook",  # Use default event type since WebhookEvent doesn't have this field
                    payload_hash=_hash_payload(raw_body),
                    sanitized_payload=_sanitize_webhook_payload(raw_body),
                    processing_status="failed",
                    error_code="internal_error",
                    processed_at=datetime.now(timezone.utc),
                )
                db.add(failure_event)
            
            await db.commit()
            
        except Exception as metadata_error:
            await db.rollback()
            logger.error(f"Failed to record failure metadata in fresh transaction: {metadata_error}")
            
        logger.exception(f"Unexpected error processing webhook for top-up {top_up.id}")
        return {
            "status": "internal_error",
            "provider_event_id": webhook_event.provider_event_id,
            "top_up_id": str(top_up.id) if top_up else None,
            "error": str(exc),
        }