import uuid
from datetime import datetime
from typing import Any, Dict

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.user import User
from utils import row_to_dict

router = APIRouter(tags=["user"])


class SetPINRequest(BaseModel):
    pin: str
    confirm_pin: str


class VerifyPINRequest(BaseModel):
    pin: str


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

    protected = ("phone_number", "pin", "is_verified")
    allowed_cols = {c.name for c in User.__table__.columns} - set(protected)

    for key, val in update_data.items():
        if key in allowed_cols:
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
    if len(req.pin) != 4 or not req.pin.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PIN must be 4 digits")
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
