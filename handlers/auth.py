import hashlib
import os
import random
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from models.user import OTP, User
from models.wallet import Wallet

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    phone_number: str
    country_code: str
    full_name: str


class VerifyOTPRequest(BaseModel):
    phone_number: str
    code: str
    # Collected during registration wizard; used when the user record is created
    user_type: str = "receiver"       # "sender" | "receiver"
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


def _generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"


def _hash_otp(code: str) -> str:
    pepper = os.getenv("OTP_PEPPER", "")
    return hashlib.sha256(f"{pepper}{code}".encode()).hexdigest()


def _generate_token(user_id: str, phone_number: str) -> str:
    jwt_secret = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    payload = {
        "user_id": user_id,
        "phone_number": phone_number,
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, jwt_secret, algorithm="HS256")


@router.post("/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(User).where(User.phone_number == req.phone_number))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    # Rate limit: max 3 OTP requests per phone per 10 minutes
    window = datetime.utcnow() - timedelta(minutes=10)
    recent_count = await db.scalar(
        select(func.count()).select_from(OTP).where(
            OTP.phone_number == req.phone_number,
            OTP.created_at > window,
        )
    )
    if recent_count >= 3:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Please wait before trying again.",
        )

    # Invalidate any existing unverified OTPs for this phone
    await db.execute(
        delete(OTP).where(
            OTP.phone_number == req.phone_number,
            OTP.verified == False,
        )
    )

    otp_code = _generate_otp()
    otp = OTP(
        phone_number=req.phone_number,
        code=_hash_otp(otp_code),
        purpose="registration",
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.add(otp)
    await db.commit()

    # TODO: send OTP via SMS (Twilio) — print only for local dev
    print(f"[DEV] OTP for {req.phone_number}: {otp_code}")

    return {"message": "OTP sent successfully"}


@router.post("/verify-otp")
async def verify_otp(req: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    otp = await db.scalar(
        select(OTP).where(
            OTP.phone_number == req.phone_number,
            OTP.code == _hash_otp(req.code),
            OTP.verified == False,
            OTP.expires_at > datetime.utcnow(),
        )
    )
    if not otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")

    otp.verified = True

    currency = req.home_currency.upper() if req.home_currency else "XOF"

    user = User(
        phone_number=req.phone_number,
        full_name=req.full_name or "",
        country=req.home_country or "",
        is_verified=True,
        kyc_status="pending",
        preferred_lang="fr",
        user_type=req.user_type or "receiver",
        home_currency=currency,
    )
    db.add(user)
    await db.flush()  # get user.id before commit

    wallet = Wallet(
        user_id=user.id,
        balance=0,
        currency=currency,
        status="active",
        daily_limit=_DAILY_LIMITS.get(currency, _DEFAULT_DAILY),
        monthly_limit=_MONTHLY_LIMITS.get(currency, _DEFAULT_MONTHLY),
    )
    db.add(wallet)
    await db.commit()

    token = _generate_token(str(user.id), req.phone_number)
    return {"message": "Registration successful", "token": token, "user_id": str(user.id)}


@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.phone_number == req.phone_number))
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
    return {
        "message": "Login successful",
        "token": token,
        "user": {
            "id": str(user.id),
            "phone_number": user.phone_number,
            "full_name": user.full_name,
            "kyc_status": user.kyc_status,
            "is_verified": user.is_verified,
        },
    }


@router.post("/refresh")
async def refresh_token():
    return {"message": "Token refreshed"}
