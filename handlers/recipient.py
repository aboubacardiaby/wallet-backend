import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.recipient import Recipient
from utils import row_to_dict

router = APIRouter(tags=["recipients"])

AVATAR_COLORS = [
    "#6366f1", "#8b5cf6", "#ec4899", "#f59e0b",
    "#10b981", "#3b82f6", "#ef4444", "#14b8a6",
]


class RecipientCreate(BaseModel):
    phone_number: str
    full_name: str
    nickname: Optional[str] = ""
    country_code: Optional[str] = "SN"
    country_name: Optional[str] = "Senegal"


class RecipientUpdate(BaseModel):
    full_name: Optional[str] = None
    nickname: Optional[str] = None
    country_code: Optional[str] = None
    country_name: Optional[str] = None


@router.get("/user/recipients")
async def list_recipients(
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(Recipient)
        .where(Recipient.user_id == uuid.UUID(token["user_id"]))
        .order_by(Recipient.created_at.desc())
    )
    return {"recipients": [row_to_dict(r) for r in rows]}


@router.post("/user/recipients", status_code=status.HTTP_201_CREATED)
async def add_recipient(
    body: RecipientCreate,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])

    # prevent duplicates for this user
    existing = await db.scalar(
        select(Recipient).where(
            and_(
                Recipient.user_id == user_id,
                Recipient.phone_number == body.phone_number,
            )
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recipient already saved",
        )

    # pick a deterministic color from the phone number
    color_idx = sum(ord(c) for c in body.phone_number) % len(AVATAR_COLORS)

    r = Recipient(
        id=uuid.uuid4(),
        user_id=user_id,
        phone_number=body.phone_number,
        full_name=body.full_name,
        nickname=body.nickname or "",
        avatar_color=AVATAR_COLORS[color_idx],
        country_code=body.country_code or "SN",
        country_name=body.country_name or "Senegal",
        created_at=datetime.utcnow(),
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return {"recipient": row_to_dict(r), "message": "Recipient saved"}


@router.put("/user/recipients/{recipient_id}")
async def update_recipient(
    recipient_id: str,
    body: RecipientUpdate,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    r = await db.scalar(
        select(Recipient).where(
            and_(
                Recipient.id == uuid.UUID(recipient_id),
                Recipient.user_id == uuid.UUID(token["user_id"]),
            )
        )
    )
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipient not found")

    if body.full_name is not None:
        r.full_name = body.full_name
    if body.nickname is not None:
        r.nickname = body.nickname
    if body.country_code is not None:
        r.country_code = body.country_code
    if body.country_name is not None:
        r.country_name = body.country_name
    await db.commit()
    return {"message": "Recipient updated"}


@router.delete("/user/recipients/{recipient_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recipient(
    recipient_id: str,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    r = await db.scalar(
        select(Recipient).where(
            and_(
                Recipient.id == uuid.UUID(recipient_id),
                Recipient.user_id == uuid.UUID(token["user_id"]),
            )
        )
    )
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipient not found")
    await db.delete(r)
    await db.commit()
