import hashlib
import logging
import os
import random
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

_DEBUG_OTP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_otp.txt")

import bcrypt
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from config.runtime import is_production, jwt_secret
from models.refresh_token import RefreshToken
from models.user import OTP, User
from models.wallet import Wallet
from utils import normalise_phone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    phone_number: str
    country_code: str
    full_name: str


class VerifyOTPRequest(BaseModel):
    phone_number: str
    code: str
    user_type: str = "receiver"
    home_currency: str = "XOF"
    full_name: str = ""
    home_country: str = ""


# Wallet limits per currency
_DAILY_LIMITS = {
    "USD": 5_000,   "CAD": 7_000,   "EUR": 5_000,   "GBP": 4_000,   "CHF": 5_000,
    "XOF": 500_000, "XAF": 500_000, "NGN": 2_000_000, "GHS": 30_000,
    "KES": 500_000, "MAD": 50_000,  "ZAR": 90_000,
}
_MONTHLY_LIMITS = {k: v * 10 for k, v in _DAILY_LIMITS.items()}
_DEFAULT_DAILY   = 500_000
_DEFAULT_MONTHLY = 5_000_000


class LoginRequest(BaseModel):
    phone_number: str
    pin: str


def _send_otp_sms(phone_number: str, otp_code: str) -> None:
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_ = os.getenv("TWILIO_FROM_NUMBER", "")

    if sid and token and from_:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException
        try:
            Client(sid, token).messages.create(
                body=f"Your Kalipeh verification code is {otp_code}. It expires in 5 minutes.",
                from_=from_,
                to=phone_number,
            )
        except TwilioRestException as exc:
            logger.error("Twilio SMS failed for %s: %s", phone_number, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not send OTP. Please try again.",
            )
    else:
        # Twilio not configured — log for local development only
        logger.warning("[DEV] OTP for %s: %s", phone_number, otp_code)


def _generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"


def _hash_otp(code: str) -> str:
    pepper = os.getenv("OTP_PEPPER", "")
    return hashlib.sha256(f"{pepper}{code}".encode()).hexdigest()


def _generate_token(user_id: str, phone_number: str) -> str:
    payload = {
        "user_id": user_id,
        "phone_number": phone_number,
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


@router.post("/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    print(f"[DEBUG] /register hit with phone={req.phone_number}, country={req.country_code}", flush=True)
    raw_phone = req.phone_number if req.phone_number.startswith("+") else f"{req.country_code}{req.phone_number}"
    try:
        phone = normalise_phone(raw_phone)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    existing = await db.scalar(select(User).where(User.phone_number == phone))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    otp_code = _generate_otp()
    otp = OTP(
        phone_number=phone,
        code=_hash_otp(otp_code),
        purpose="registration",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(otp)
    await db.commit()

    # DEBUG: print the OTP code so it is visible during development/testing.
    print(f"[DEBUG] OTP code for {phone}: {otp_code}", flush=True)
    logger.info(f"[DEBUG] OTP code for {phone}: {otp_code}")

    # Also write to a debug file so it can be found even if uvicorn swallows stdout.
    try:
        with open(_DEBUG_OTP_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} - {phone} - {otp_code}\n")
    except Exception:
        pass

    _send_otp_sms(phone, otp_code)

    # DEBUG: always return the OTP for visibility during local testing.
    return {"message": "OTP sent successfully", "debug_otp": otp_code}


@router.post("/verify-otp")
async def verify_otp(req: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    try:
        phone = normalise_phone(req.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    otp_record = await db.scalar(
        select(OTP).where(
            OTP.phone_number == phone,
            OTP.purpose == "registration",
            OTP.verified == False,
        ).order_by(OTP.created_at.desc())
    )

    if not otp_record or otp_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP expired or not found.")

    if otp_record.code != _hash_otp(req.code.strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect OTP code.")

    # Delete the OTP record so it cannot be replayed.
    await db.delete(otp_record)

    user = User(
        phone_number=otp_record.phone_number,
        is_verified=True,
        kyc_status="pending",
        preferred_lang="fr",
        full_name=req.full_name,
        user_type=req.user_type,
        home_currency=req.home_currency.upper(),
        country=req.home_country,
    )
    db.add(user)
    await db.flush()

    wallet = Wallet(
        user_id=user.id,
        balance=0,
        currency=user.home_currency,
        status="active",
        daily_limit=_DAILY_LIMITS.get(user.home_currency, _DEFAULT_DAILY),
        monthly_limit=_MONTHLY_LIMITS.get(user.home_currency, _DEFAULT_MONTHLY),
    )
    db.add(wallet)
    await db.commit()

    token = _generate_token(str(user.id), user.phone_number)
    refresh_plain, refresh_record = RefreshToken.create(user.id)
    db.add(refresh_record)
    await db.commit()

    return {"message": "Registration successful", "token": token, "refresh_token": refresh_plain, "user_id": str(user.id)}


@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    try:
        phone = normalise_phone(req.phone_number)
    except ValueError:
        phone = req.phone_number
    user = await db.scalar(select(User).where(User.phone_number == phone))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.is_locked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is locked. Contact support.")

    if not user.pin or not bcrypt.checkpw(req.pin.encode(), user.pin.encode()):
        user.pin_attempts += 1
        if user.pin_attempts >= 3:
            user.is_locked = True
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user.pin_attempts = 0
    user.last_login_at = datetime.utcnow()
    await db.commit()

    token = _generate_token(str(user.id), user.phone_number)
    refresh_plain, refresh_record = RefreshToken.create(user.id)
    db.add(refresh_record)
    await db.commit()

    return {
        "message": "Login successful",
        "token": token,
        "refresh_token": refresh_plain,
        "user": {
            "id": str(user.id),
            "phone_number": user.phone_number,
            "full_name": user.full_name,
            "kyc_status": user.kyc_status,
            "is_verified": user.is_verified,
        },
    }


@router.post("/refresh")
async def refresh_token(
    refresh_token: str = Header(..., alias="X-Refresh-Token"),
    db: AsyncSession = Depends(get_db),
):
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    row = await db.scalar(
        select(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.used_at.is_(None),
            RefreshToken.expires_at > datetime.utcnow(),
        )
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    user = await db.scalar(select(User).where(User.id == row.user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    row.used_at = datetime.utcnow()
    access_token = _generate_token(str(user.id), user.phone_number)
    new_refresh_plain, new_refresh_record = RefreshToken.create(user.id)
    db.add(new_refresh_record)
    await db.commit()

    return {
        "message": "Token refreshed",
        "token": access_token,
        "refresh_token": new_refresh_plain,
        "user_id": str(user.id),
    }
