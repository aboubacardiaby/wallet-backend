import uuid as uuid_lib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.notification import Notification
from utils import row_to_dict

router = APIRouter(tags=["notifications"])


@router.get("/notifications")
async def get_notifications(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid_lib.UUID(token["user_id"])

    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    rows = result.scalars().all()

    unread_count = await db.scalar(
        select(func.count()).where(Notification.user_id == user_id, Notification.is_read == False)
    )

    return {"notifications": [row_to_dict(n) for n in rows], "unread_count": unread_count, "page": page, "limit": limit}


@router.put("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid_lib.UUID(token["user_id"])
    notification = await db.scalar(
        select(Notification).where(
            Notification.id == uuid_lib.UUID(notification_id),
            Notification.user_id == user_id,
        )
    )
    if not notification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")

    notification.is_read = True
    notification.read_at = datetime.utcnow()
    await db.commit()
    return {"message": "Notification marked as read"}
