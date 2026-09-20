"""Authenticated customer top-up endpoints (T013; FR-001--FR-005, FR-015, FR-020)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from datetime import timedelta
from decimal import Decimal
from typing import Callable, Coroutine, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from handlers.topup_contracts import (
    CancelTopUpResponse,
    ErrorCode,
    FundingSummary,
    InitiateTopUpRequest,
    InitiateTopUpResponse,
    NextAction,
    TopUpDetailResponse,
    TopUpHistoryResponse,
    TopUpResponse,
    error_response,
)
from handlers.auth import _generate_otp, _send_otp_sms
from middleware.auth import verify_token
from models.fee_rule import FeeRule
from models.topup import TopUp
from models.wallet import Wallet
from models.user import User
from services.topup.money import Money, calculate_fee, net_credit
from services.topup.agent_cash import hash_confirmation
from services.topup.provider import (
    InitiationRequest,
    InvalidProviderStateError,
    PaymentProvider,
    ProviderConfigurationError,
    ProviderNotImplementedError,
    ProviderRejectedError,
    ProviderUnavailableError,
)
from services.topup.state_machine import transition
from services.topup.stripe_provider import StripePaymentProvider

logger = logging.getLogger(__name__)
CANCELLABLE_STATUSES = {"Pending", "RequiresAction"}

# Funding methods whose payment is created at, and confirmed by, the payment provider
# (T034c). agent_cash and mobile_money have their own flows and never reach it.
PROVIDER_FUNDING_METHODS = {"card", "bank_transfer"}


class TopUpApiError(Exception):
    def __init__(self, code: ErrorCode, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class TopUpRoute(APIRoute):
    """Keep validation/auth/application failures in T005's stable envelope."""

    def get_route_handler(self) -> Callable[[Request], Coroutine]:
        original = super().get_route_handler()

        async def route_handler(request: Request):
            try:
                return await original(request)
            except TopUpApiError as exc:
                status_code, body = error_response(exc.code, exc.message, field=exc.field)
                return JSONResponse(status_code=status_code, content=body)
            except RequestValidationError as exc:
                issue = exc.errors()[0] if exc.errors() else {}
                location = issue.get("loc", ())
                field = ".".join(str(part) for part in location if part not in {"body", "path", "query", "header"}) or None
                status_code, body = error_response(
                    ErrorCode.VALIDATION_ERROR,
                    issue.get("msg", "Request validation failed"),
                    field=field,
                )
                return JSONResponse(status_code=status_code, content=body)
            except HTTPException as exc:
                code = (
                    ErrorCode.AUTHENTICATION_REQUIRED
                    if exc.status_code in {401, 403}
                    else ErrorCode.INTERNAL_ERROR
                )
                status_code, body = error_response(code, str(exc.detail))
                return JSONResponse(status_code=status_code, content=body)
            except Exception:
                logger.exception("Unhandled top-up endpoint failure")
                status_code, body = error_response(
                    ErrorCode.INTERNAL_ERROR, "An internal error occurred"
                )
                return JSONResponse(status_code=status_code, content=body)

        return route_handler


router = APIRouter(tags=["top-ups"], route_class=TopUpRoute)


def _user_id_from_token(token: dict) -> uuid.UUID:
    raw_user_id = token.get("sub") or token.get("user_id")
    try:
        return uuid.UUID(str(raw_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TopUpApiError(
            ErrorCode.AUTHENTICATION_REQUIRED, "Authenticated user identity is invalid"
        ) from exc


def _build_request_fingerprint(request: InitiateTopUpRequest) -> str:
    payload = request.model_dump(mode="json", exclude_none=False)
    payload["amount"] = format(request.amount.quantize(Decimal("0.01")), "f")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not 8 <= len(normalized) <= 255:
        raise TopUpApiError(
            ErrorCode.VALIDATION_ERROR,
            "Idempotency-Key must contain between 8 and 255 characters",
            "Idempotency-Key",
        )
    return normalized


def _enforce_wallet_limits(wallet: Wallet, amount: Decimal) -> None:
    daily_spent = Decimal(str(wallet.daily_spent or 0))
    monthly_spent = Decimal(str(wallet.monthly_spent or 0))
    daily_limit = Decimal(str(wallet.daily_limit or 0))
    monthly_limit = Decimal(str(wallet.monthly_limit or 0))
    if daily_spent + amount > daily_limit:
        raise TopUpApiError(ErrorCode.LIMIT_EXCEEDED, "Daily wallet limit exceeded", "amount")
    if monthly_spent + amount > monthly_limit:
        raise TopUpApiError(ErrorCode.LIMIT_EXCEEDED, "Monthly wallet limit exceeded", "amount")


def _provider_unavailable() -> TopUpApiError:
    return TopUpApiError(
        ErrorCode.PROVIDER_UNAVAILABLE,
        "Payments are temporarily unavailable. Please try again in a moment.",
    )


def _initiation_provider(funding_method: str) -> Optional[PaymentProvider]:
    """The provider that creates payments for ``funding_method``, or None.

    None means "no provider": the top-up is only recorded as Pending (the legacy
    development behavior). Configuration comes from the environment only and fails
    closed:

    * no ``STRIPE_SECRET_KEY``: None in development, a 503 in production (never leave
      dead-end Pending top-ups behind a live app);
    * a key but no ``STRIPE_WEBHOOK_SECRET``: a 503 everywhere, because a payment must
      never be created that this system could not later confirm;
    * a live (``sk_live_``) key outside production: a 503, so a development machine can
      never move real money.
    """
    if funding_method not in PROVIDER_FUNDING_METHODS:
        return None
    api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    webhook_secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    production = os.getenv("APP_ENV", "").strip().lower() == "production"

    if not api_key:
        if production:
            logger.error("STRIPE_SECRET_KEY is not configured in production")
            raise _provider_unavailable()
        return None
    if api_key.startswith("sk_live_") and not production:
        logger.error("A live Stripe key is configured outside production; refusing to use it")
        raise _provider_unavailable()
    if not webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET is not configured; refusing to create payments")
        raise _provider_unavailable()
    try:
        return StripePaymentProvider(webhook_secret=webhook_secret, api_key=api_key)
    except ProviderConfigurationError:
        raise _provider_unavailable()


async def _cancel_provider_payment(top_up: TopUp) -> None:
    """Cancel the top-up's provider payment, or raise so the caller does NOT cancel locally.

    Any outcome other than "the provider says it is cancelled" must block the local cancel:
    a payment that cannot be proven cancelled may still be confirmed and charged.
    """
    provider = _initiation_provider(top_up.funding_method)
    if provider is None:
        logger.error(
            "Top-up %s has a provider payment but no provider is configured; refusing to cancel",
            top_up.internal_reference,
        )
        raise _provider_unavailable()
    try:
        await asyncio.to_thread(provider.cancel, top_up.provider_transaction_reference)
    except ProviderRejectedError:
        raise TopUpApiError(
            ErrorCode.INVALID_STATE,
            "This payment can no longer be cancelled. It will update once it is confirmed.",
        )
    except (ProviderUnavailableError, ProviderConfigurationError, ProviderNotImplementedError) as exc:
        logger.warning(
            "Top-up %s: could not cancel the provider payment (%s)", top_up.internal_reference, type(exc).__name__
        )
        raise _provider_unavailable()


async def _fail_top_up(db: AsyncSession, top_up: TopUp, code: str, message: str) -> None:
    top_up.status = transition(top_up.status, "Failed")
    top_up.failure_code = code
    top_up.failure_message = message
    top_up.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(top_up)


async def _initiate_with_provider(
    db: AsyncSession, provider: PaymentProvider, top_up: TopUp
) -> TopUpResponse:
    """Create the provider payment for a top-up that is already durably Pending.

    Ordering (Constitution III): the Pending row is committed before this runs, and no
    database transaction is held open across the provider call. The provider
    idempotency key is the top-up's own unique reference, so a retry after a crash or
    timeout can never create a second payment.
    """
    request = InitiationRequest(
        internal_reference=top_up.internal_reference,
        wallet_id=str(top_up.wallet_id),
        gross_amount=Money(top_up.gross_amount, top_up.currency),
        funding_method=top_up.funding_method,
        idempotency_key=top_up.internal_reference,
    )
    try:
        # The provider SDK call blocks; keep it off the event loop.
        result = await asyncio.to_thread(provider.initiate, request)
    except ProviderRejectedError:
        await _fail_top_up(db, top_up, "provider_rejected", "The payment provider rejected this top-up")
        return _top_up_response(top_up)
    except (ProviderUnavailableError, ProviderConfigurationError) as exc:
        # Left Pending with no provider reference; retrying the same request is safe.
        logger.warning("Top-up %s: provider unavailable during initiation (%s)", top_up.internal_reference, type(exc).__name__)
        raise _provider_unavailable()

    top_up.provider_name = provider.name
    top_up.provider_transaction_reference = result.provider_transaction_reference
    top_up.updated_at = datetime.now(timezone.utc)
    # If this commit fails the top-up stays Pending without a reference; a same-key retry
    # re-initiates with the same provider idempotency key and gets the same payment back.
    await db.commit()
    await db.refresh(top_up)
    return _top_up_response(
        top_up,
        next_action=NextAction(
            type="confirm_with_provider", provider=provider.name, client_secret=result.client_secret
        ),
    )


async def _resume_with_provider(provider: PaymentProvider, top_up: TopUp) -> TopUpResponse:
    """Replay for a top-up that already has a provider payment: re-fetch the secret.

    Never calls ``initiate`` again: a second payment could be confirmed by the customer
    and charged with no top-up to credit.
    """
    try:
        secret = await asyncio.to_thread(provider.retrieve_client_secret, top_up.provider_transaction_reference)
    except InvalidProviderStateError:
        # The customer already confirmed; nothing left to do but wait for the webhook.
        return _top_up_response(top_up)
    except (ProviderUnavailableError, ProviderConfigurationError, ProviderRejectedError) as exc:
        logger.warning("Top-up %s: could not re-fetch provider secret (%s)", top_up.internal_reference, type(exc).__name__)
        raise _provider_unavailable()
    return _top_up_response(
        top_up,
        next_action=NextAction(type="confirm_with_provider", provider=provider.name, client_secret=secret),
    )


async def _replay_response(db: AsyncSession, existing: TopUp) -> InitiateTopUpResponse:
    if existing.status == "Pending" and existing.funding_method in PROVIDER_FUNDING_METHODS:
        provider = _initiation_provider(existing.funding_method)
        if provider is not None:
            if existing.provider_transaction_reference is None:
                # An earlier attempt never recorded a provider payment (crash, or the
                # provider was unavailable): finish it now.
                top_up_response = await _initiate_with_provider(db, provider, existing)
            else:
                top_up_response = await _resume_with_provider(provider, existing)
            return InitiateTopUpResponse(top_up=top_up_response, idempotent_replay=True)
    return InitiateTopUpResponse(top_up=_top_up_response(existing), idempotent_replay=True)


def _top_up_response(top_up: TopUp, next_action: Optional[NextAction] = None) -> TopUpResponse:
    if next_action is None and top_up.status == "Pending":
        next_action = NextAction(type="await_provider")
    return TopUpResponse(
        id=top_up.id,
        reference=top_up.internal_reference,
        wallet_id=top_up.wallet_id,
        status=top_up.status,
        gross_amount=top_up.gross_amount,
        fee_amount=top_up.fee_amount,
        net_amount=top_up.net_amount,
        currency=top_up.currency,
        funding=FundingSummary(method=top_up.funding_method, provider=top_up.provider_name),
        next_action=next_action,
        created_at=top_up.created_at,
        updated_at=top_up.updated_at,
        completed_at=top_up.completed_at,
    )


async def _owned_wallet(db: AsyncSession, wallet_id: uuid.UUID, user_id: uuid.UUID) -> Wallet:
    wallet = await db.scalar(
        select(Wallet).where(Wallet.id == wallet_id, Wallet.user_id == user_id)
    )
    if wallet is None:
        raise TopUpApiError(ErrorCode.RESOURCE_NOT_FOUND, "Top-up resource not found")
    return wallet


async def _owned_top_up(
    db: AsyncSession,
    wallet_id: uuid.UUID,
    top_up_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> TopUp:
    statement = (
        select(TopUp)
        .join(Wallet, Wallet.id == TopUp.wallet_id)
        .where(TopUp.id == top_up_id, TopUp.wallet_id == wallet_id, Wallet.user_id == user_id)
    )
    if for_update:
        statement = statement.with_for_update()
    top_up = await db.scalar(statement)
    if top_up is None:
        raise TopUpApiError(ErrorCode.RESOURCE_NOT_FOUND, "Top-up resource not found")
    return top_up


@router.post(
    "/wallets/{wallet_id}/top-ups",
    response_model=InitiateTopUpResponse,
    status_code=201,
)
async def initiate_top_up(
    wallet_id: uuid.UUID,
    request: InitiateTopUpRequest,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=255),
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id_from_token(token)
    wallet = await _owned_wallet(db, wallet_id, user_id)
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    fingerprint = _build_request_fingerprint(request)

    existing = await db.scalar(
        select(TopUp).where(
            TopUp.wallet_id == wallet_id, TopUp.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise TopUpApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was already used for a different request",
                "Idempotency-Key",
            )
        response.status_code = 200
        return await _replay_response(db, existing)

    if str(wallet.status).lower() != "active":
        raise TopUpApiError(ErrorCode.WALLET_INACTIVE, "Wallet is not active")
    if request.currency != wallet.currency:
        raise TopUpApiError(
            ErrorCode.UNSUPPORTED_CURRENCY,
            "Top-up currency must match the wallet currency",
            "currency",
        )
    _enforce_wallet_limits(wallet, request.amount)

    # Resolve the provider BEFORE recording anything, so a misconfiguration answers 503
    # instead of leaving a Pending top-up that can never complete behind it.
    provider = _initiation_provider(request.funding_method.value)

    rules = list((await db.scalars(select(FeeRule).where(FeeRule.is_active.is_(True)))).all())
    gross = Money(request.amount, request.currency)
    fee = calculate_fee(gross, rules)
    net = net_credit(gross, fee)
    now = datetime.now(timezone.utc)
    confirmation_code = None
    confirmation_expires_at = None
    if request.funding_method.value == "agent_cash":
        confirmation_code = _generate_otp()
        confirmation_expires_at = now + timedelta(minutes=5)
        owner = await db.scalar(select(User).where(User.id == wallet.user_id))
        if owner is None:
            raise TopUpApiError(ErrorCode.RESOURCE_NOT_FOUND, "Top-up resource not found")
        _send_otp_sms(owner.phone_number, confirmation_code)

    top_up = TopUp(
        internal_reference=f"tu_{uuid.uuid4().hex}",
        wallet_id=wallet_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        funding_method=request.funding_method.value,
        gross_amount=gross.amount,
        fee_amount=fee.amount,
        net_amount=net.amount,
        currency=gross.currency,
        status="Pending",
        created_at=now,
        updated_at=now,
        confirmation_code_hash=hash_confirmation(confirmation_code) if confirmation_code else None,
        confirmation_expires_at=confirmation_expires_at,
    )
    db.add(top_up)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(TopUp).where(
                TopUp.wallet_id == wallet_id, TopUp.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            raise TopUpApiError(ErrorCode.INTERNAL_ERROR, "Unable to create top-up")
        if existing.request_fingerprint != fingerprint:
            raise TopUpApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was already used for a different request",
                "Idempotency-Key",
            )
        response.status_code = 200
        return await _replay_response(db, existing)
    await db.refresh(top_up)
    if provider is None:
        return InitiateTopUpResponse(top_up=_top_up_response(top_up))
    return InitiateTopUpResponse(top_up=await _initiate_with_provider(db, provider, top_up))


@router.get("/wallets/{wallet_id}/top-ups/{top_up_id}", response_model=TopUpDetailResponse)
async def get_top_up(
    wallet_id: uuid.UUID,
    top_up_id: uuid.UUID,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    top_up = await _owned_top_up(db, wallet_id, top_up_id, _user_id_from_token(token))
    return TopUpDetailResponse(top_up=_top_up_response(top_up))


@router.get("/wallets/{wallet_id}/top-ups", response_model=TopUpHistoryResponse)
async def get_top_up_history(
    wallet_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id_from_token(token)
    await _owned_wallet(db, wallet_id, user_id)
    total = await db.scalar(select(func.count()).select_from(TopUp).where(TopUp.wallet_id == wallet_id))
    rows = (
        await db.scalars(
            select(TopUp)
            .where(TopUp.wallet_id == wallet_id)
            .order_by(TopUp.created_at.desc(), TopUp.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    count = int(total or 0)
    return TopUpHistoryResponse(
        items=[_top_up_response(item) for item in rows],
        page=page,
        page_size=page_size,
        total=count,
        has_next=page * page_size < count,
    )


@router.post(
    "/wallets/{wallet_id}/top-ups/{top_up_id}/cancel",
    response_model=CancelTopUpResponse,
)
async def cancel_top_up(
    wallet_id: uuid.UUID,
    top_up_id: uuid.UUID,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id_from_token(token)
    # Read WITHOUT a row lock first: a provider network call must never run while a lock is held.
    top_up = await _owned_top_up(db, wallet_id, top_up_id, user_id)
    if top_up.status == "Cancelled":
        return CancelTopUpResponse(top_up=_top_up_response(top_up))
    if top_up.status not in CANCELLABLE_STATUSES:
        raise TopUpApiError(ErrorCode.INVALID_STATE, "Top-up cannot be cancelled in its current state")

    if top_up.provider_transaction_reference:
        # The provider payment must be cancelled FIRST. Marking only our record cancelled would
        # leave a payment the customer can still confirm; when it succeeded, the verified success
        # would find a terminal Cancelled top-up and be ignored: charged, never credited.
        await _cancel_provider_payment(top_up)

    # Re-check under the lock: a webhook may have moved the top-up while the provider call ran.
    top_up = await _owned_top_up(db, wallet_id, top_up_id, user_id, for_update=True)
    if top_up.status == "Cancelled":
        return CancelTopUpResponse(top_up=_top_up_response(top_up))
    if top_up.status not in CANCELLABLE_STATUSES:
        await db.rollback()  # release the row lock
        raise TopUpApiError(ErrorCode.INVALID_STATE, "Top-up cannot be cancelled in its current state")
    top_up.status = "Cancelled"
    top_up.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(top_up)
    return CancelTopUpResponse(top_up=_top_up_response(top_up))
