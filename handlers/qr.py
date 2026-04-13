import base64
import io
import json

import qrcode
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.user import User

router = APIRouter(tags=["qr"])


class GenerateQRRequest(BaseModel):
    amount: float = 0.0
    description: str = ""


class ScanQRRequest(BaseModel):
    qr_data: str


@router.post("/qr/generate")
async def generate_qr_code(req: GenerateQRRequest, token: dict = Depends(verify_token)):
    payload = {
        "user_id": token["user_id"],
        "phone_number": token["phone_number"],
        "amount": req.amount,
        "description": req.description,
    }
    img = qrcode.make(json.dumps(payload))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    return {"qr_code": qr_base64, "payload": payload, "format": "image/png"}


@router.post("/qr/scan")
async def scan_qr_code(
    req: ScanQRRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = json.loads(req.qr_data)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid QR code data")

    to_phone = payload.get("phone_number")
    if not to_phone:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid QR code: missing phone number")

    recipient = await db.scalar(select(User).where(User.phone_number == to_phone))
    if not recipient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipient not found")

    return {
        "recipient": {
            "user_id": str(recipient.id),
            "phone_number": recipient.phone_number,
            "full_name": recipient.full_name,
        },
        "amount": payload.get("amount", 0.0),
        "description": payload.get("description", ""),
    }
