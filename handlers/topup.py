"""Authenticated customer top-up endpoints (T013; FR-001--FR-005, FR-015, FR-020)."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from datetime import timedelta
from decimal import Decimal
from typing import Callable, Coroutine

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
from services.topup.state_machine import transition

logger = logging.getLogger(__name__)
CANCELLABLE_STATUSES = {"Pending", "RequiresAction"}


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


def _top_up_response(top_up: TopUp) -> TopUpResponse:
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
        next_action=NextAction(type="await_provider") if top_up.status == "Pending" else None,
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
        return InitiateTopUpResponse(top_up=_top_up_response(existing), idempotent_replay=True)

    if str(wallet.status).lower() != "active":
        raise TopUpApiError(ErrorCode.WALLET_INACTIVE, "Wallet is not active")
    if request.currency != wallet.currency:
        raise TopUpApiError(
            ErrorCode.UNSUPPORTED_CURRENCY,
            "Top-up currency must match the wallet currency",
            "currency",
        )
    _enforce_wallet_limits(wallet, request.amount)

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
        return InitiateTopUpResponse(top_up=_top_up_response(existing), idempotent_replay=True)
    await db.refresh(top_up)
    return InitiateTopUpResponse(top_up=_top_up_response(top_up))


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
    top_up = await _owned_top_up(
        db, wallet_id, top_up_id, _user_id_from_token(token), for_update=True
    )
    if top_up.status == "Cancelled":
        return CancelTopUpResponse(top_up=_top_up_response(top_up))
    if top_up.status not in CANCELLABLE_STATUSES:
        raise TopUpApiError(ErrorCode.INVALID_STATE, "Top-up cannot be cancelled in its current state")
    top_up.status = "Cancelled"
    top_up.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(top_up)
    return CancelTopUpResponse(top_up=_top_up_response(top_up))
