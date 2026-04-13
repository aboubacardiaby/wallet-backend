"""
Admin handler — dashboard stats, user/wallet/bank/KYC management, settings.
All endpoints require a valid admin JWT (is_admin=True claim).
Admin login issues such a token against env-configured credentials.
"""
import os
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from config.rate_config import (
    calculate_fee, clear_rate_override, get_rate_override,
    load_from_db, refresh_fee_rules, set_rate_override,
)
from middleware.auth import verify_admin_token
from models.bank import Bank
from models.fee_rule import FeeRule
from models.kyc import KYCSubmission
from models.rate_override import RateOverride
from models.user import User
from models.wallet import Transaction, Wallet
from utils import row_to_dict

router = APIRouter(tags=["admin"], prefix="/admin")

# ── In-memory app settings (replace with DB table for persistence) ─────────
_settings: dict = {
    "transfer_fee_rate":   0.015,
    "daily_limit_default":   500_000.0,
    "monthly_limit_default": 2_000_000.0,
    "min_transfer_amount":   1.0,
    "max_transfer_amount":   10_000.0,
    "maintenance_mode":      False,
    "kyc_required":          False,
    "support_email":         "support@kalipeh.com",
    "app_name":              "Kalipeh Wallet",
}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str
    password: str


class CreateWalletRequest(BaseModel):
    user_id: str
    currency: str = "XOF"
    initial_balance: float = 0.0
    daily_limit: float = 500_000.0
    monthly_limit: float = 2_000_000.0


class UpdateWalletRequest(BaseModel):
    balance: Optional[float] = None
    status: Optional[str] = None
    daily_limit: Optional[float] = None
    monthly_limit: Optional[float] = None
    currency: Optional[str] = None


class BankRequest(BaseModel):
    name: str
    country: str
    country_code: Optional[str] = None
    swift_code: Optional[str] = None
    logo_url: Optional[str] = None
    currency: Optional[str] = None
    is_active: bool = True


class UpdateUserStatusRequest(BaseModel):
    is_locked: Optional[bool] = None
    kyc_status: Optional[str] = None
    user_type: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    transfer_fee_rate: Optional[float] = None
    daily_limit_default: Optional[float] = None
    monthly_limit_default: Optional[float] = None
    min_transfer_amount: Optional[float] = None
    max_transfer_amount: Optional[float] = None
    maintenance_mode: Optional[bool] = None
    kyc_required: Optional[bool] = None
    support_email: Optional[str] = None
    app_name: Optional[str] = None


# ── Admin login ───────────────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(body: AdminLoginRequest):
    """Issue an admin JWT. Credentials come from env vars ADMIN_USERNAME / ADMIN_PASSWORD."""
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_pass = os.getenv("ADMIN_PASSWORD", "admin123")

    if body.username != expected_user or body.password != expected_pass:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    jwt_secret = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    payload = {
        "sub": body.username,
        "is_admin": True,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": 43200}


# ── Dashboard stats ───────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    total_users       = await db.scalar(select(func.count()).select_from(User))
    total_wallets     = await db.scalar(select(func.count()).select_from(Wallet))
    total_txns        = await db.scalar(select(func.count()).select_from(Transaction))
    pending_kyc       = await db.scalar(
        select(func.count()).select_from(KYCSubmission)
        .where(KYCSubmission.status == "pending")
    )
    under_review_kyc  = await db.scalar(
        select(func.count()).select_from(KYCSubmission)
        .where(KYCSubmission.status == "under_review")
    )
    verified_kyc      = await db.scalar(
        select(func.count()).select_from(KYCSubmission)
        .where(KYCSubmission.status == "verified")
    )
    total_volume      = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.status == "completed")
    )
    senders           = await db.scalar(
        select(func.count()).select_from(User).where(User.user_type == "sender")
    )
    receivers         = await db.scalar(
        select(func.count()).select_from(User).where(User.user_type == "receiver")
    )
    total_banks       = await db.scalar(select(func.count()).select_from(Bank))

    # Recent 7 days transaction counts
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    recent_txns = await db.scalar(
        select(func.count()).select_from(Transaction)
        .where(Transaction.created_at >= seven_days_ago)
    )

    return {
        "total_users": total_users,
        "senders": senders,
        "receivers": receivers,
        "total_wallets": total_wallets,
        "total_transactions": total_txns,
        "recent_transactions_7d": recent_txns,
        "total_volume": float(total_volume or 0),
        "kyc_pending": pending_kyc,
        "kyc_under_review": under_review_kyc,
        "kyc_verified": verified_kyc,
        "total_banks": total_banks,
    }


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    search: Optional[str] = Query(None),
    user_type: Optional[str] = Query(None),
    kyc_status: Optional[str] = Query(None),
    is_locked: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    q = select(User).order_by(User.created_at.desc())
    if search:
        like = f"%{search}%"
        q = q.where(
            User.phone_number.ilike(like) |
            User.full_name.ilike(like) |
            User.email.ilike(like)
        )
    if user_type:
        q = q.where(User.user_type == user_type)
    if kyc_status:
        q = q.where(User.kyc_status == kyc_status)
    if is_locked is not None:
        q = q.where(User.is_locked == is_locked)

    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows  = await db.scalars(q.offset((page - 1) * limit).limit(limit))
    users = [row_to_dict(u, exclude=("pin",)) for u in rows]
    return {"users": users, "total": total, "page": page, "pages": -(-total // limit)}


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.id == uuid_lib.UUID(user_id)))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user.id))
    txn_count = await db.scalar(
        select(func.count()).select_from(Transaction)
        .where((Transaction.from_user_id == user.id) | (Transaction.to_user_id == user.id))
    )
    return {
        "user": row_to_dict(user, exclude=("pin",)),
        "wallet": row_to_dict(wallet) if wallet else None,
        "transaction_count": txn_count,
    }


@router.put("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    body: UpdateUserStatusRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.id == uuid_lib.UUID(user_id)))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.is_locked is not None:
        user.is_locked = body.is_locked
        user.pin_attempts = 0 if not body.is_locked else user.pin_attempts
    if body.kyc_status is not None:
        if body.kyc_status not in ("pending", "under_review", "verified", "rejected"):
            raise HTTPException(status_code=400, detail="Invalid kyc_status")
        user.kyc_status = body.kyc_status
    if body.user_type is not None:
        if body.user_type not in ("sender", "receiver"):
            raise HTTPException(status_code=400, detail="Invalid user_type")
        user.user_type = body.user_type
    user.updated_at = datetime.utcnow()
    await db.commit()
    return {"message": "User updated", "user_id": user_id}


# ── Wallets ───────────────────────────────────────────────────────────────────

@router.get("/wallets")
async def list_wallets(
    search: Optional[str] = Query(None, description="Filter by user phone or name"),
    currency: Optional[str] = Query(None),
    wallet_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    q = select(Wallet).order_by(Wallet.created_at.desc())
    if currency:
        q = q.where(Wallet.currency == currency.upper())
    if wallet_status:
        q = q.where(Wallet.status == wallet_status)

    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows  = await db.scalars(q.offset((page - 1) * limit).limit(limit))
    wallets = []
    for w in rows:
        d = row_to_dict(w)
        # Attach user info
        user = await db.scalar(select(User).where(User.id == w.user_id))
        d["user_phone"]    = user.phone_number if user else None
        d["user_name"]     = user.full_name    if user else None
        d["user_type"]     = user.user_type    if user else None
        wallets.append(d)

    if search:
        s = search.lower()
        wallets = [w for w in wallets if
                   s in (w["user_phone"] or "").lower() or
                   s in (w["user_name"] or "").lower()]
    return {"wallets": wallets, "total": total, "page": page, "pages": -(-total // limit)}


@router.post("/wallets", status_code=status.HTTP_201_CREATED)
async def create_wallet(
    body: CreateWalletRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid_lib.UUID(body.user_id)
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = await db.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if existing:
        raise HTTPException(status_code=400, detail="User already has a wallet")

    now = datetime.utcnow()
    wallet = Wallet(
        user_id=user_id,
        balance=body.initial_balance,
        currency=body.currency.upper(),
        status="active",
        daily_limit=body.daily_limit,
        monthly_limit=body.monthly_limit,
        created_at=now,
        updated_at=now,
    )
    db.add(wallet)
    await db.commit()
    await db.refresh(wallet)
    return {"message": "Wallet created", "wallet": row_to_dict(wallet)}


@router.put("/wallets/{wallet_id}")
async def update_wallet(
    wallet_id: str,
    body: UpdateWalletRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    wallet = await db.scalar(select(Wallet).where(Wallet.id == uuid_lib.UUID(wallet_id)))
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if body.balance is not None:
        wallet.balance = body.balance
    if body.status is not None:
        if body.status not in ("active", "inactive", "frozen"):
            raise HTTPException(status_code=400, detail="Invalid status")
        wallet.status = body.status
    if body.daily_limit is not None:
        wallet.daily_limit = body.daily_limit
    if body.monthly_limit is not None:
        wallet.monthly_limit = body.monthly_limit
    if body.currency is not None:
        wallet.currency = body.currency.upper()
    wallet.updated_at = datetime.utcnow()
    await db.commit()
    return {"message": "Wallet updated", "wallet": row_to_dict(wallet)}


# ── Banks ─────────────────────────────────────────────────────────────────────

@router.get("/banks")
async def list_banks(
    country: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    q = select(Bank).order_by(Bank.name)
    if country:
        q = q.where(Bank.country.ilike(f"%{country}%"))
    if is_active is not None:
        q = q.where(Bank.is_active == is_active)
    rows = await db.scalars(q)
    return {"banks": [row_to_dict(b) for b in rows]}


@router.post("/banks", status_code=status.HTTP_201_CREATED)
async def create_bank(
    body: BankRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    bank = Bank(
        name=body.name,
        country=body.country,
        country_code=body.country_code,
        swift_code=body.swift_code,
        logo_url=body.logo_url,
        currency=body.currency,
        is_active=body.is_active,
        created_at=now,
        updated_at=now,
    )
    db.add(bank)
    await db.commit()
    await db.refresh(bank)
    return {"message": "Bank created", "bank": row_to_dict(bank)}


@router.put("/banks/{bank_id}")
async def update_bank(
    bank_id: str,
    body: BankRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    bank = await db.scalar(select(Bank).where(Bank.id == uuid_lib.UUID(bank_id)))
    if not bank:
        raise HTTPException(status_code=404, detail="Bank not found")
    bank.name        = body.name
    bank.country     = body.country
    bank.country_code = body.country_code
    bank.swift_code  = body.swift_code
    bank.logo_url    = body.logo_url
    bank.currency    = body.currency
    bank.is_active   = body.is_active
    bank.updated_at  = datetime.utcnow()
    await db.commit()
    return {"message": "Bank updated", "bank": row_to_dict(bank)}


@router.delete("/banks/{bank_id}")
async def delete_bank(
    bank_id: str,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    bank = await db.scalar(select(Bank).where(Bank.id == uuid_lib.UUID(bank_id)))
    if not bank:
        raise HTTPException(status_code=404, detail="Bank not found")
    await db.delete(bank)
    await db.commit()
    return {"message": "Bank deleted"}


# ── Transactions ──────────────────────────────────────────────────────────────

@router.get("/transactions")
async def list_transactions(
    search: Optional[str] = Query(None, description="Filter by phone or ref"),
    tx_type: Optional[str] = Query(None, alias="type"),
    tx_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    q = select(Transaction).order_by(Transaction.created_at.desc())
    if tx_type:
        q = q.where(Transaction.type == tx_type)
    if tx_status:
        q = q.where(Transaction.status == tx_status)
    if search:
        like = f"%{search}%"
        q = q.where(
            Transaction.transaction_ref.ilike(like) |
            Transaction.from_phone.ilike(like) |
            Transaction.to_phone.ilike(like)
        )
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows  = await db.scalars(q.offset((page - 1) * limit).limit(limit))
    return {
        "transactions": [row_to_dict(t) for t in rows],
        "total": total,
        "page": page,
        "pages": -(-total // limit),
    }


# ── KYC (admin) ───────────────────────────────────────────────────────────────

@router.get("/kyc")
async def list_kyc(
    kyc_status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    q = select(KYCSubmission).order_by(KYCSubmission.submitted_at.desc())
    if kyc_status:
        q = q.where(KYCSubmission.status == kyc_status)
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows  = await db.scalars(q.offset((page - 1) * limit).limit(limit))
    submissions = []
    for s in rows:
        d = row_to_dict(s)
        user = await db.scalar(select(User).where(User.id == s.user_id))
        d["user_phone"] = user.phone_number if user else None
        d["user_name"]  = user.full_name    if user else None
        submissions.append(d)
    return {"submissions": submissions, "total": total, "page": page, "pages": -(-total // limit)}


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(token: dict = Depends(verify_admin_token)):
    return _settings


@router.put("/settings")
async def update_settings(
    body: SettingsUpdateRequest,
    token: dict = Depends(verify_admin_token),
):
    for field, value in body.model_dump(exclude_none=True).items():
        if field in _settings:
            _settings[field] = value
    return _settings


# ── Rate override schemas ─────────────────────────────────────────────────────

class RateOverrideRequest(BaseModel):
    from_currency: str
    to_currency:   str
    rate:          float
    spread_pct:    float = 0.0
    note:          Optional[str] = None
    is_active:     bool = True


# ── Exchange rate management ──────────────────────────────────────────────────

RATE_PAIRS = [
    ("USD", "XOF"), ("USD", "XAF"), ("USD", "NGN"), ("USD", "GHS"),
    ("USD", "KES"), ("USD", "MAD"), ("USD", "GNF"), ("USD", "GMD"),
    ("EUR", "XOF"), ("EUR", "XAF"), ("EUR", "NGN"), ("EUR", "GHS"),
    ("EUR", "KES"), ("EUR", "MAD"),
    ("GBP", "XOF"), ("GBP", "XAF"), ("GBP", "NGN"),
    ("CAD", "XOF"), ("CHF", "XOF"),
]


@router.get("/exchange-rates")
async def list_exchange_rates(
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Return live rates for standard remittance corridors, annotated with any
    active admin overrides.
    """
    import time as _t
    from handlers.exchange import _fetch_rates    # reuse cached fetch

    # Fetch USD and EUR base rates (covers all our corridors)
    results = []
    for base in ("USD", "EUR", "GBP", "CAD", "CHF"):
        try:
            data = await _fetch_rates(base)
            base_rates = data["rates"]
            fetched_at = data["fetched_at"]
        except Exception:
            base_rates = {}
            fetched_at = None

        for (fc, tc) in RATE_PAIRS:
            if fc != base:
                continue
            live_rate = base_rates.get(tc)
            if live_rate is None:
                continue
            override = get_rate_override(fc, tc)
            results.append({
                "from_currency": fc,
                "to_currency":   tc,
                "live_rate":     live_rate,
                "effective_rate": override if override is not None else live_rate,
                "has_override":  override is not None,
                "fetched_at":    fetched_at,
            })

    # Fetch all saved overrides from DB
    db_overrides = await db.scalars(
        select(RateOverride).order_by(RateOverride.from_currency, RateOverride.to_currency)
    )
    overrides = [row_to_dict(o) for o in db_overrides]

    return {"pairs": results, "overrides": overrides}


@router.post("/exchange-rates/override", status_code=status.HTTP_201_CREATED)
async def set_override(
    body: RateOverrideRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Create or replace a rate override for a currency pair."""
    from_ccy = body.from_currency.upper()
    to_ccy   = body.to_currency.upper()

    # Upsert: delete any existing override for this pair, then insert new
    existing = await db.scalar(
        select(RateOverride)
        .where(RateOverride.from_currency == from_ccy, RateOverride.to_currency == to_ccy)
    )
    if existing:
        await db.delete(existing)

    now = datetime.utcnow()
    override = RateOverride(
        from_currency=from_ccy,
        to_currency=to_ccy,
        rate=body.rate,
        spread_pct=body.spread_pct,
        is_active=body.is_active,
        note=body.note,
        created_at=now,
        updated_at=now,
    )
    db.add(override)
    await db.commit()
    await db.refresh(override)

    # Update live cache
    if body.is_active:
        set_rate_override(from_ccy, to_ccy, body.rate, body.spread_pct)
    else:
        clear_rate_override(from_ccy, to_ccy)

    return {"message": "Rate override saved", "override": row_to_dict(override)}


@router.put("/exchange-rates/override/{override_id}")
async def update_override(
    override_id: str,
    body: RateOverrideRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    o = await db.scalar(select(RateOverride).where(RateOverride.id == uuid_lib.UUID(override_id)))
    if not o:
        raise HTTPException(status_code=404, detail="Override not found")
    o.rate       = body.rate
    o.spread_pct = body.spread_pct
    o.is_active  = body.is_active
    o.note       = body.note
    o.updated_at = datetime.utcnow()
    await db.commit()
    # Refresh live cache
    if body.is_active:
        set_rate_override(o.from_currency, o.to_currency, body.rate, body.spread_pct)
    else:
        clear_rate_override(o.from_currency, o.to_currency)
    return {"message": "Override updated", "override": row_to_dict(o)}


@router.delete("/exchange-rates/override/{override_id}")
async def delete_override(
    override_id: str,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    o = await db.scalar(select(RateOverride).where(RateOverride.id == uuid_lib.UUID(override_id)))
    if not o:
        raise HTTPException(status_code=404, detail="Override not found")
    clear_rate_override(o.from_currency, o.to_currency)
    await db.delete(o)
    await db.commit()
    return {"message": "Override deleted"}


@router.post("/exchange-rates/refresh-cache")
async def refresh_rate_cache(token: dict = Depends(verify_admin_token)):
    """Force the exchange-rate cache to expire so the next request fetches fresh data."""
    from handlers.exchange import _cache
    _cache.clear()
    return {"message": "Rate cache cleared — next request will fetch fresh rates"}


# ── Fee rule schemas ──────────────────────────────────────────────────────────

class FeeRuleRequest(BaseModel):
    name:          str
    from_currency: Optional[str] = None
    to_currency:   Optional[str] = None
    fee_rate:      float = 0.015
    fee_flat:      float = 0.0
    min_fee:       Optional[float] = None
    max_fee:       Optional[float] = None
    min_amount:    Optional[float] = None
    max_amount:    Optional[float] = None
    priority:      int  = 0
    is_active:     bool = True
    note:          Optional[str] = None


# ── Fee rule management ───────────────────────────────────────────────────────

@router.get("/fees")
async def list_fee_rules(
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(select(FeeRule).order_by(FeeRule.priority.desc(), FeeRule.created_at))
    rules = [row_to_dict(r) for r in rows]
    return {"rules": rules}


@router.post("/fees", status_code=status.HTTP_201_CREATED)
async def create_fee_rule(
    body: FeeRuleRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    rule = FeeRule(
        name=body.name,
        from_currency=body.from_currency.upper() if body.from_currency else None,
        to_currency=body.to_currency.upper()     if body.to_currency   else None,
        fee_rate=body.fee_rate,
        fee_flat=body.fee_flat,
        min_fee=body.min_fee,
        max_fee=body.max_fee,
        min_amount=body.min_amount,
        max_amount=body.max_amount,
        priority=body.priority,
        is_active=body.is_active,
        note=body.note,
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    # Refresh in-memory cache
    await _reload_fee_rules(db)
    return {"message": "Fee rule created", "rule": row_to_dict(rule)}


@router.put("/fees/{rule_id}")
async def update_fee_rule(
    rule_id: str,
    body: FeeRuleRequest,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    rule = await db.scalar(select(FeeRule).where(FeeRule.id == uuid_lib.UUID(rule_id)))
    if not rule:
        raise HTTPException(status_code=404, detail="Fee rule not found")
    rule.name          = body.name
    rule.from_currency = body.from_currency.upper() if body.from_currency else None
    rule.to_currency   = body.to_currency.upper()   if body.to_currency   else None
    rule.fee_rate      = body.fee_rate
    rule.fee_flat      = body.fee_flat
    rule.min_fee       = body.min_fee
    rule.max_fee       = body.max_fee
    rule.min_amount    = body.min_amount
    rule.max_amount    = body.max_amount
    rule.priority      = body.priority
    rule.is_active     = body.is_active
    rule.note          = body.note
    rule.updated_at    = datetime.utcnow()
    await db.commit()
    await _reload_fee_rules(db)
    return {"message": "Fee rule updated", "rule": row_to_dict(rule)}


@router.delete("/fees/{rule_id}")
async def delete_fee_rule(
    rule_id: str,
    token: dict = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    rule = await db.scalar(select(FeeRule).where(FeeRule.id == uuid_lib.UUID(rule_id)))
    if not rule:
        raise HTTPException(status_code=404, detail="Fee rule not found")
    await db.delete(rule)
    await db.commit()
    await _reload_fee_rules(db)
    return {"message": "Fee rule deleted"}


@router.get("/fees/preview")
async def preview_fee(
    from_currency: str = Query(...),
    to_currency:   str = Query(...),
    amount:        float = Query(..., gt=0),
    token: dict = Depends(verify_admin_token),
):
    """Preview what fee would be charged for a given transfer."""
    result = calculate_fee(from_currency.upper(), to_currency.upper(), amount)
    return result


async def _reload_fee_rules(db: AsyncSession) -> None:
    """Refresh the in-memory fee rule cache from DB."""
    rows = await db.scalars(select(FeeRule))
    refresh_fee_rules([row_to_dict(r) for r in rows])
