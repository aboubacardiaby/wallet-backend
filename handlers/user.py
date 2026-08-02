import uuid
from datetime import datetime
from typing import Any, Dict

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.user import User
from utils import row_to_dict

router = APIRouter(tags=["user"])

# Explicit allowlist of self-service-editable profile fields. Anything not
# listed here — including security-sensitive columns like pin_attempts,
# is_locked, kyc_status, and user_type — is rejected by design.
ALLOWED_PROFILE_FIELDS = {
    "full_name",
    "email",
    "profile_photo",
    "national_id_type",
    "national_id_number",
    "date_of_birth",
    "street",
    "city",
    "region",
    "country",
    "postal_code",
    "home_currency",
    "preferred_lang",
    "biometric_enabled",
    "device_tokens",
}


class SetPINRequest(BaseModel):
    pin: str
    confirm_pin: str

    @field_validator("pin", "confirm_pin")
    @classmethod
    def _validate_pin(cls, v: str) -> str:
        if not v.isdigit() or not (4 <= len(v) <= 6):
            raise ValueError("PIN must be 4-6 digits")
        return v


class VerifyPINRequest(BaseModel):
    pin: str

    @field_validator("pin")
    @classmethod
    def _validate_pin(cls, v: str) -> str:
        if not v.isdigit() or not (4 <= len(v) <= 6):
            raise ValueError("PIN must be 4-6 digits")
        return v


async def _get_user_or_404(user_id: str, db: AsyncSession) -> User:
    user = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("/user/profile")
async def get_profile(token: dict = Depends(verify_token), db: AsyncSession = Depends(get_db)):
    user = await _get_user_or_404(token["user_id"], db)
    return {"user": row_to_dict(user, exclude=("pin", "pin_attempts", "device_tokens"))}


@router.put("/user/profile")
async def update_profile(
    update_data: Dict[str, Any],
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_or_404(token["user_id"], db)

    for key, val in update_data.items():
        if key in ALLOWED_PROFILE_FIELDS:
            setattr(user, key, val)

    user.updated_at = datetime.utcnow()
    await db.commit()
    return {"message": "Profile updated successfully"}


@router.post("/user/pin")
async def set_pin(
    req: SetPINRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    if req.pin != req.confirm_pin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PINs do not match")

    user = await _get_user_or_404(token["user_id"], db)
    user.pin = bcrypt.hashpw(req.pin.encode(), bcrypt.gensalt()).decode()
    user.updated_at = datetime.utcnow()
    await db.commit()
    return {"message": "PIN set successfully"}


@router.post("/user/verify-pin")
async def verify_pin(
    req: VerifyPINRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_or_404(token["user_id"], db)

    if not user.pin or not bcrypt.checkpw(req.pin.encode(), user.pin.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid PIN")

    return {"message": "PIN verified", "valid": True}
