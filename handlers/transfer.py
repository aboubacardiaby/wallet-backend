"""
Transfer handler — supports both same-currency and cross-currency remittance.
Sender (diaspora, e.g. USD/EUR) → Receiver (Africa, e.g. XOF/NGN).
"""
import os
import random
import re
import uuid as uuid_lib
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.user import User
from models.wallet import MoneyRequest, Transaction, Wallet
from utils import row_to_dict

# ── Static agent locations ────────────────────────────────────────────────────
AGENT_LOCATIONS = [
    # Senegal
    {"id": "ag_sn_01", "name": "Orange Money – Dakar Plateau",     "city": "Dakar",       "country": "Senegal",        "address": "12 Rue Carnot, Plateau, Dakar",          "phone": "+221 77 100 0001"},
    {"id": "ag_sn_02", "name": "Wave Agent – Pikine",              "city": "Pikine",       "country": "Senegal",        "address": "Marché Tilène, Pikine",                  "phone": "+221 77 100 0002"},
    {"id": "ag_sn_03", "name": "Free Money – Thiès",               "city": "Thiès",        "country": "Senegal",        "address": "Ave Lamine Guèye, Thiès",                "phone": "+221 77 100 0003"},
    # Gambia
    {"id": "ag_gm_01", "name": "Trust Bank Agent – Banjul",        "city": "Banjul",       "country": "Gambia",         "address": "14 Ecowas Ave, Banjul",                  "phone": "+220 990 0001"},
    {"id": "ag_gm_02", "name": "QCell Money – Serekunda",          "city": "Serekunda",    "country": "Gambia",         "address": "Westfield Junction, Serekunda",           "phone": "+220 990 0002"},
    {"id": "ag_gm_03", "name": "Africell Money – Brikama",         "city": "Brikama",      "country": "Gambia",         "address": "Market Road, Brikama",                   "phone": "+220 990 0003"},
    # Côte d'Ivoire
    {"id": "ag_ci_01", "name": "MTN Mobile Money – Abidjan",       "city": "Abidjan",      "country": "Côte d'Ivoire",  "address": "Plateau, Blvd de la République, Abidjan","phone": "+225 07 100 0001"},
    {"id": "ag_ci_02", "name": "Wave Agent – Bouaké",              "city": "Bouaké",       "country": "Côte d'Ivoire",  "address": "Marché Central, Bouaké",                 "phone": "+225 07 100 0002"},
    # Mali
    {"id": "ag_ml_01", "name": "Orange Money – Bamako ACI 2000",   "city": "Bamako",       "country": "Mali",           "address": "ACI 2000, Rue 310, Bamako",               "phone": "+223 70 100 001"},
    {"id": "ag_ml_02", "name": "Moov Money – Bamako Medina",       "city": "Bamako",       "country": "Mali",           "address": "Medina Coura, Bamako",                   "phone": "+223 70 100 002"},
    # Guinea
    {"id": "ag_gn_01", "name": "Orange Money – Conakry Kaloum",    "city": "Conakry",      "country": "Guinea",         "address": "Avenue de la République, Kaloum",        "phone": "+224 62 100 001"},
    # Nigeria
    {"id": "ag_ng_01", "name": "OPay Agent – Lagos Island",        "city": "Lagos",        "country": "Nigeria",        "address": "25 Broad St, Lagos Island",               "phone": "+234 80 100 0001"},
    {"id": "ag_ng_02", "name": "Moniepoint – Abuja Wuse",          "city": "Abuja",        "country": "Nigeria",        "address": "Wuse 2, Abuja FCT",                      "phone": "+234 80 100 0002"},
    # Ghana
    {"id": "ag_gh_01", "name": "MTN Mobile Money – Accra",         "city": "Accra",        "country": "Ghana",          "address": "Ring Road Central, Accra",               "phone": "+233 24 100 0001"},
    # Kenya
    {"id": "ag_ke_01", "name": "M-Pesa Agent – Nairobi CBD",       "city": "Nairobi",      "country": "Kenya",          "address": "Kenyatta Ave, Nairobi CBD",               "phone": "+254 70 100 0001"},
    # Morocco
    {"id": "ag_ma_01", "name": "CashPlus – Casablanca Maarif",     "city": "Casablanca",   "country": "Morocco",        "address": "Blvd Zerktouni, Maarif, Casablanca",     "phone": "+212 52 100 0001"},
    # Burkina Faso
    {"id": "ag_bf_01", "name": "Orange Money – Ouagadougou",       "city": "Ouagadougou",  "country": "Burkina Faso",   "address": "Ave Kwame Nkrumah, Ouagadougou",         "phone": "+226 70 100 001"},
    # Cameroon
    {"id": "ag_cm_01", "name": "MTN MoMo – Douala Akwa",           "city": "Douala",       "country": "Cameroon",       "address": "Rue de la Joie, Akwa, Douala",            "phone": "+237 67 100 0001"},
]

router = APIRouter(tags=["transfer"])

TRANSFER_FEE_RATE = 0.015   # 1.5 % — global fallback (overridden by fee rules in DB)
XOF_EUR_RATE = 655.957

# Countries where Wave operates (used for simulated Wave registration check)
WAVE_COUNTRIES = {"Senegal", "Côte d'Ivoire", "Mali", "Burkina Faso", "Guinea", "Uganda", "Tanzania"}


# ── Phone helpers ────────────────────────────────────────────────────────────

def _normalise_phone(raw: str) -> str:
    cleaned = re.sub(r"[\s\-().]+", "", raw)
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    # Require E.164: + followed by 7–15 digits
    if not re.fullmatch(r"\+\d{7,15}", cleaned):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid phone number '{raw}'. Use E.164 format, e.g. +221778689865.",
        )
    return cleaned


async def _find_user_by_phone(phone_raw: str, db: AsyncSession):
    phone = _normalise_phone(phone_raw)

    # 1. Exact match (normalised)
    user = await db.scalar(select(User).where(User.phone_number == phone))
    if user:
        return user

    # 2. Exact match on raw input (handles phones stored without + prefix)
    raw_stripped = phone_raw.strip()
    if raw_stripped != phone:
        user = await db.scalar(select(User).where(User.phone_number == raw_stripped))
        if user:
            return user

    # 3. Suffix match via SQL LIKE — digits only, lengths 10 down to 6
    digits = re.sub(r"\D", "", phone)
    for suffix_len in range(min(10, len(digits)), 5, -1):
        suffix = digits[-suffix_len:]
        user = await db.scalar(
            select(User).where(User.phone_number.like(f"%{suffix}"))
        )
        if user:
            return user

    return None


# ── Exchange rate helper ─────────────────────────────────────────────────────

import time as _time
_rate_cache: dict = {}
_CACHE_TTL = 3600


async def _get_rate(from_ccy: str, to_ccy: str) -> float:
    """Return how many `to_ccy` units equal 1 `from_ccy`."""
    if from_ccy == to_ccy:
        return 1.0

    # Check admin rate override first
    from config.rate_config import get_rate_override
    override = get_rate_override(from_ccy, to_ccy)
    if override is not None:
        return override

    cache_key = f"{from_ccy}_{to_ccy}"
    now = _time.time()
    if cache_key in _rate_cache and now - _rate_cache[cache_key]["ts"] < _CACHE_TTL:
        return _rate_cache[cache_key]["rate"]

    # XOF peg derived from EUR
    fetch_base = "EUR" if from_ccy == "XOF" else from_ccy
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"https://api.exchangerate-api.com/v4/latest/{fetch_base}")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Exchange rate service unavailable: {e}")

    rates = data.get("rates", {})
    if "XOF" not in rates and "EUR" in rates:
        rates["XOF"] = round(rates["EUR"] * XOF_EUR_RATE, 4)

    if from_ccy == "XOF":
        # rates are EUR-based → invert to XOF-based
        eur_to_xof = XOF_EUR_RATE
        xof_rates = {ccy: round(v / eur_to_xof, 8) for ccy, v in rates.items()}
        xof_rates["XOF"] = 1.0
        rate = xof_rates.get(to_ccy)
    else:
        rate = rates.get(to_ccy)
        if rate is None and to_ccy == "XOF" and "EUR" in rates:
            rate = round(rates["EUR"] * XOF_EUR_RATE, 4)

    if rate is None:
        raise HTTPException(status_code=400, detail=f"Unsupported currency pair: {from_ccy}→{to_ccy}")

    _rate_cache[cache_key] = {"rate": rate, "ts": now}
    return rate


# ── Pydantic schemas ─────────────────────────────────────────────────────────

class CashPickupRequest(BaseModel):
    to_phone: str
    recipient_name: str
    amount: float
    recv_currency: str
    agent_id: str
    description: str = ""


class WaveTransferRequest(BaseModel):
    to_phone: str          # recipient's Wave-registered phone
    amount: float          # in sender's currency
    recv_currency: str     # destination currency (XOF, XAF, GMD, etc.)
    description: str = ""
    recipient_name: Optional[str] = None  # sender-supplied name for unregistered recipients


class SendMoneyRequest(BaseModel):
    to_phone: str
    amount: float                          # in sender's currency
    description: str = ""
    recv_currency: Optional[str] = None   # destination currency hint (informational; wallet currency takes precedence)
    payment_method_id: Optional[str] = None  # reserved for future card-funded transfers


class RequestMoneyRequest(BaseModel):
    from_phone: str
    amount: float
    description: str = ""


# ── Quote endpoint ───────────────────────────────────────────────────────────

@router.get("/transfer/quote")
async def get_quote(
    to_phone: str = Query(...),
    amount: float = Query(..., gt=0),
    recv_currency: str = Query(None),   # optional: override with destination country currency
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """Return a live rate preview: fee, received amount, exchange rate."""
    sender_id = uuid_lib.UUID(token["user_id"])
    sender_wallet = await db.scalar(select(Wallet).where(Wallet.user_id == sender_id))
    if not sender_wallet:
        raise HTTPException(status_code=404, detail="Sender wallet not found")

    recipient = await _find_user_by_phone(to_phone, db)
    recipient_wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == recipient.id)
    ) if recipient else None

    send_ccy = sender_wallet.currency
    # Priority: recipient's actual wallet currency > caller-supplied override > XOF default
    if recipient_wallet:
        recv_ccy = recipient_wallet.currency
    elif recv_currency:
        recv_ccy = recv_currency.upper()
    else:
        recv_ccy = "XOF"

    from config.rate_config import calculate_fee
    fee_info = calculate_fee(send_ccy, recv_ccy, amount)
    fee      = fee_info["fee"]
    net_send = round(amount - fee, 2)

    if send_ccy == recv_ccy:
        rate = 1.0
        received = net_send
    else:
        rate = await _get_rate(send_ccy, recv_ccy)
        received = round(net_send * rate, 2)

    return {
        "send_currency": send_ccy,
        "recv_currency": recv_ccy,
        "send_amount": amount,
        "fee": fee,
        "fee_rate_pct": fee_info["fee_rate"] * 100,
        "fee_rule": fee_info.get("rule_name"),
        "net_send_amount": net_send,
        "exchange_rate": rate,
        "received_amount": received,
        "recipient_found": recipient is not None,
        "recipient_name": recipient.full_name if recipient else None,
    }


# ── Send money ────────────────────────────────────────────────────────────────

@router.post("/transfer/send")
async def send_money(
    req: SendMoneyRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    sender_id = uuid_lib.UUID(token["user_id"])

    recipient = await _find_user_by_phone(req.to_phone, db)
    if not recipient:
        raise HTTPException(
            status_code=404,
            detail=f"No account found for '{req.to_phone}'. Check the number and try again.",
        )
    if recipient.id == sender_id:
        raise HTTPException(status_code=400, detail="Cannot send money to yourself")

    sender_wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == sender_id).with_for_update()
    )
    if not sender_wallet:
        raise HTTPException(status_code=404, detail="Sender wallet not found")
    if sender_wallet.status != "active":
        raise HTTPException(status_code=403, detail="Wallet is not active")
    if float(sender_wallet.balance) < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    recipient_wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == recipient.id).with_for_update()
    )
    if not recipient_wallet or recipient_wallet.status != "active":
        raise HTTPException(status_code=400, detail="Recipient wallet is unavailable")

    send_ccy = sender_wallet.currency
    recv_ccy = recipient_wallet.currency

    from config.rate_config import calculate_fee
    fee_info = calculate_fee(send_ccy, recv_ccy, req.amount)
    fee      = fee_info["fee"]
    net_send = round(req.amount - fee, 2)

    if send_ccy == recv_ccy:
        rate = 1.0
        received = net_send
    else:
        rate = await _get_rate(send_ccy, recv_ccy)
        received = round(net_send * rate, 2)

    # Debit sender (full amount including fee)
    sender_wallet.balance    = float(sender_wallet.balance) - req.amount
    sender_wallet.daily_spent   = float(sender_wallet.daily_spent) + req.amount
    sender_wallet.monthly_spent = float(sender_wallet.monthly_spent) + req.amount
    sender_wallet.updated_at = datetime.utcnow()

    # Credit receiver (after conversion)
    recipient_wallet.balance   = float(recipient_wallet.balance) + received
    recipient_wallet.updated_at = datetime.utcnow()

    now = datetime.utcnow()
    tx = Transaction(
        transaction_ref=str(uuid_lib.uuid4()),
        type="remittance" if send_ccy != recv_ccy else "send",
        status="completed",
        from_user_id=sender_id,
        to_user_id=recipient.id,
        from_phone=token["phone_number"],
        to_phone=_normalise_phone(req.to_phone),
        amount=req.amount,
        fee=fee,
        total_amount=req.amount,
        currency=send_ccy,
        description=req.description,
        completed_at=now,
        extra_data={
            "send_amount": req.amount,
            "send_currency": send_ccy,
            "recv_currency": recv_ccy,
            "exchange_rate": rate,
            "net_send_amount": net_send,
            "received_amount": received,
            "recipient_name": recipient.full_name or req.to_phone,
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "Transfer successful",
        "transaction_ref": tx.transaction_ref,
        "send_amount": req.amount,
        "send_currency": send_ccy,
        "fee": fee,
        "exchange_rate": rate,
        "received_amount": received,
        "recv_currency": recv_ccy,
        "recipient_name": recipient.full_name or req.to_phone,
        "to_phone": _normalise_phone(req.to_phone),
    }


# ── Request money ─────────────────────────────────────────────────────────────

@router.post("/transfer/request")
async def request_money(
    req: RequestMoneyRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    requester_id = uuid_lib.UUID(token["user_id"])
    payer = await _find_user_by_phone(req.from_phone, db)
    if not payer:
        raise HTTPException(status_code=404, detail="User not found")

    money_req = MoneyRequest(
        from_user_id=payer.id,
        to_user_id=requester_id,
        amount=req.amount,
        description=req.description,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(money_req)
    await db.commit()
    return {"message": "Money request sent", "request_id": str(money_req.id)}


# ── Pending requests ──────────────────────────────────────────────────────────

@router.get("/transfer/pending")
async def get_pending_requests(
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid_lib.UUID(token["user_id"])
    rows = await db.scalars(
        select(MoneyRequest)
        .where(
            or_(MoneyRequest.from_user_id == user_id, MoneyRequest.to_user_id == user_id),
            MoneyRequest.status == "pending",
        )
        .order_by(MoneyRequest.created_at.desc())
    )
    return {"pending_requests": [row_to_dict(r) for r in rows]}


# ── Wave registration check ───────────────────────────────────────────────────

@router.get("/transfer/wave/check")
async def check_wave_user(
    phone: str = Query(...),
    country: str = Query(None, description="Destination country name (for eligibility)"),
    token: dict = Depends(verify_token),
):
    """
    Check whether a phone number is registered on Wave mobile money.

    Calls the Wave Merchant API when WAVE_API_KEY is set:
        GET https://api.wave.com/v1/check-user?mobile={phone}
        Authorization: Bearer {WAVE_API_KEY}

    Falls back to simulation (country-based) when no key is configured.
    """
    normalised = _normalise_phone(phone)
    wave_api_key = os.getenv("WAVE_API_KEY", "")

    if wave_api_key:
        try:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {wave_api_key}"},
                timeout=10,
            ) as client:
                resp = await client.get(
                    f"https://api.wave.com/v1/check-user",
                    params={"mobile": normalised},
                )
                if resp.status_code == 404:
                    return {"phone": normalised, "has_wave": False, "country": country, "wave_name": None}
                resp.raise_for_status()
                data = resp.json()
                return {
                    "phone": normalised,
                    "has_wave": True,
                    "country": country,
                    "wave_name": data.get("name"),
                }
        except httpx.HTTPStatusError:
            return {"phone": normalised, "has_wave": False, "country": country, "wave_name": None}
        except Exception:
            raise HTTPException(status_code=503, detail="Wave API unreachable — cannot verify recipient.")

    # Simulation: any Wave-operating country is treated as registered
    has_wave = bool(country and country in WAVE_COUNTRIES)
    return {
        "phone": normalised,
        "has_wave": has_wave,
        "country": country,
        "wave_name": None,
    }


# ── Agent locations ───────────────────────────────────────────────────────────

@router.get("/transfer/agents")
async def list_agents(
    country: str = Query(None, description="Filter by country name"),
):
    """Return pickup agent locations, optionally filtered by country."""
    agents = AGENT_LOCATIONS
    if country:
        agents = [a for a in agents if a["country"].lower() == country.lower()]
    return {"agents": agents}


# ── Cash pickup transfer ──────────────────────────────────────────────────────

@router.post("/transfer/cash-pickup")
async def cash_pickup(
    req: CashPickupRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Send money to an unregistered recipient via a physical agent.
    Debits sender wallet and returns a 6-digit pickup PIN.
    """
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    # Validate agent
    agent = next((a for a in AGENT_LOCATIONS if a["id"] == req.agent_id), None)
    if not agent:
        raise HTTPException(status_code=400, detail="Invalid agent location")

    sender_id = uuid_lib.UUID(token["user_id"])
    sender_wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == sender_id).with_for_update()
    )
    if not sender_wallet:
        raise HTTPException(status_code=404, detail="Sender wallet not found")
    if sender_wallet.status != "active":
        raise HTTPException(status_code=403, detail="Wallet is not active")
    if float(sender_wallet.balance) < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    send_ccy = sender_wallet.currency
    recv_ccy = req.recv_currency.upper()

    from config.rate_config import calculate_fee
    fee_info = calculate_fee(send_ccy, recv_ccy, req.amount)
    fee      = fee_info["fee"]
    net_send = round(req.amount - fee, 2)

    if send_ccy == recv_ccy:
        rate = 1.0
        received = net_send
    else:
        rate = await _get_rate(send_ccy, recv_ccy)
        received = round(net_send * rate, 2)

    # Resolve recipient name: DB lookup → frontend-supplied name → phone fallback
    cash_recipient = await _find_user_by_phone(req.to_phone, db)
    resolved_recipient_name = (
        cash_recipient.full_name
        if cash_recipient and cash_recipient.full_name
        else (req.recipient_name or _normalise_phone(req.to_phone))
    )

    # Generate 6-digit pickup PIN
    pickup_code = f"{random.randint(0, 999999):06d}"

    # Debit sender wallet
    sender_wallet.balance       = float(sender_wallet.balance) - req.amount
    sender_wallet.daily_spent   = float(sender_wallet.daily_spent) + req.amount
    sender_wallet.monthly_spent = float(sender_wallet.monthly_spent) + req.amount
    sender_wallet.updated_at    = datetime.utcnow()

    tx = Transaction(
        transaction_ref=str(uuid_lib.uuid4()),
        type="cash_pickup",
        status="pending",
        from_user_id=sender_id,
        from_phone=token["phone_number"],
        to_phone=_normalise_phone(req.to_phone),
        amount=req.amount,
        fee=fee,
        total_amount=req.amount,
        currency=send_ccy,
        description=req.description or f"Cash pickup for {resolved_recipient_name or req.to_phone}",
        extra_data={
            "send_amount": req.amount,
            "pickup_code": pickup_code,
            "recipient_name": resolved_recipient_name,
            "recv_currency": recv_ccy,
            "exchange_rate": rate,
            "received_amount": received,
            "send_currency": send_ccy,
            "agent_id": agent["id"],
            "agent_name": agent["name"],
            "agent_city": agent["city"],
            "agent_country": agent["country"],
            "agent_address": agent["address"],
            "agent_phone": agent["phone"],
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "Cash pickup transfer created",
        "transaction_ref": tx.transaction_ref,
        "pickup_code": pickup_code,
        "send_amount": req.amount,
        "send_currency": send_ccy,
        "fee": fee,
        "exchange_rate": rate,
        "received_amount": received,
        "recv_currency": recv_ccy,
        "recipient_name": resolved_recipient_name,
        "agent": agent,
    }


# ── Wave mobile money transfer ────────────────────────────────────────────────

@router.post("/transfer/wave")
async def wave_transfer(
    req: WaveTransferRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Queue a Wave mobile money transfer.
    Debits the sender wallet immediately and saves the transaction as 'pending'.
    The Wave B2C API call is handled by the processor service.
    """
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    sender_id = uuid_lib.UUID(token["user_id"])
    sender_wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == sender_id).with_for_update()
    )
    if not sender_wallet:
        raise HTTPException(status_code=404, detail="Sender wallet not found")
    if sender_wallet.status != "active":
        raise HTTPException(status_code=403, detail="Wallet is not active")
    if float(sender_wallet.balance) < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    send_ccy = sender_wallet.currency
    recv_ccy = req.recv_currency.upper()

    from config.rate_config import calculate_fee
    fee_info = calculate_fee(send_ccy, recv_ccy, req.amount)
    fee      = fee_info["fee"]
    net_send = round(req.amount - fee, 2)

    if send_ccy == recv_ccy:
        rate = 1.0
        received = net_send
    else:
        rate = await _get_rate(send_ccy, recv_ccy)
        received = round(net_send * rate, 2)

    recipient_phone = _normalise_phone(req.to_phone)

    # Resolve name: registered full_name → sender-supplied name → phone fallback
    wave_recipient = await _find_user_by_phone(req.to_phone, db)
    wave_recipient_name = (
        wave_recipient.full_name
        if wave_recipient and wave_recipient.full_name
        else (req.recipient_name.strip() if req.recipient_name and req.recipient_name.strip() else recipient_phone)
    )

    # Debit sender wallet
    sender_wallet.balance       = float(sender_wallet.balance) - req.amount
    sender_wallet.daily_spent   = float(sender_wallet.daily_spent) + req.amount
    sender_wallet.monthly_spent = float(sender_wallet.monthly_spent) + req.amount
    sender_wallet.updated_at    = datetime.utcnow()

    tx = Transaction(
        transaction_ref=str(uuid_lib.uuid4()),
        type="wave_transfer",
        status="pending",
        from_user_id=sender_id,
        from_phone=token["phone_number"],
        to_phone=recipient_phone,
        amount=req.amount,
        fee=fee,
        total_amount=req.amount,
        currency=send_ccy,
        description=req.description or f"Wave transfer to {recipient_phone}",
        extra_data={
            "send_amount": req.amount,
            "wave_phone": recipient_phone,
            "send_currency": send_ccy,
            "recv_currency": recv_ccy,
            "exchange_rate": rate,
            "net_send_amount": net_send,
            "received_amount": received,
            "recipient_name": wave_recipient_name,
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "Wave transfer queued",
        "transaction_ref": tx.transaction_ref,
        "send_amount": req.amount,
        "send_currency": send_ccy,
        "fee": fee,
        "exchange_rate": rate,
        "received_amount": received,
        "recv_currency": recv_ccy,
        "recipient_name": wave_recipient_name,
        "to_phone": recipient_phone,
    }
