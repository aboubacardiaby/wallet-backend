import math
import uuid as uuid_lib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.wallet import Agent, Transaction, Wallet
from utils import row_to_dict

router = APIRouter(tags=["cash"])


class CashInRequest(BaseModel):
    agent_id: str
    amount: float


class CashOutRequest(BaseModel):
    agent_id: str
    amount: float


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


@router.post("/cash/in")
async def cash_in(
    req: CashInRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    if req.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be positive")

    user_id = uuid_lib.UUID(token["user_id"])
    agent = await db.scalar(
        select(Agent).where(Agent.id == uuid_lib.UUID(req.agent_id), Agent.is_active == True)
    )
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or inactive")
    if req.amount > float(agent.cash_in_limit):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount exceeds agent cash-in limit")

    wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    )
    if not wallet or wallet.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wallet unavailable")

    wallet.balance = float(wallet.balance) + req.amount
    wallet.updated_at = datetime.utcnow()

    tx_ref = str(uuid_lib.uuid4())
    now = datetime.utcnow()
    tx = Transaction(
        transaction_ref=tx_ref,
        type="cash_in",
        status="completed",
        to_user_id=user_id,
        to_phone=token["phone_number"],
        amount=req.amount,
        fee=0,
        total_amount=req.amount,
        currency=wallet.currency,
        agent_id=agent.id,
        completed_at=now,
    )
    db.add(tx)
    await db.commit()

    return {"message": "Cash-in successful", "transaction_ref": tx_ref, "amount": req.amount}


@router.post("/cash/out")
async def cash_out(
    req: CashOutRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    if req.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be positive")

    user_id = uuid_lib.UUID(token["user_id"])
    agent = await db.scalar(
        select(Agent).where(Agent.id == uuid_lib.UUID(req.agent_id), Agent.is_active == True)
    )
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or inactive")

    wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    )
    if not wallet or wallet.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wallet unavailable")
    if float(wallet.balance) < req.amount:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient balance")

    wallet.balance = float(wallet.balance) - req.amount
    wallet.daily_spent = float(wallet.daily_spent) + req.amount
    wallet.monthly_spent = float(wallet.monthly_spent) + req.amount
    wallet.updated_at = datetime.utcnow()

    tx_ref = str(uuid_lib.uuid4())
    now = datetime.utcnow()
    tx = Transaction(
        transaction_ref=tx_ref,
        type="cash_out",
        status="completed",
        from_user_id=user_id,
        from_phone=token["phone_number"],
        amount=req.amount,
        fee=0,
        total_amount=req.amount,
        currency=wallet.currency,
        agent_id=agent.id,
        completed_at=now,
    )
    db.add(tx)
    await db.commit()

    return {"message": "Cash-out successful", "transaction_ref": tx_ref, "amount": req.amount}


@router.get("/agents/nearby")
async def get_nearby_agents(
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_km: float = Query(5.0),
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    # Bounding box pre-filter, then exact Haversine sort
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(latitude)))

    result = await db.execute(
        select(Agent).where(
            Agent.is_active == True,
            Agent.latitude.between(latitude - lat_delta, latitude + lat_delta),
            Agent.longitude.between(longitude - lon_delta, longitude + lon_delta),
        )
    )
    agents = result.scalars().all()

    nearby = []
    for a in agents:
        dist = _haversine_km(latitude, longitude, a.latitude, a.longitude)
        if dist <= radius_km:
            d = row_to_dict(a)
            d["distance_km"] = round(dist, 3)
            nearby.append(d)

    nearby.sort(key=lambda x: x["distance_km"])
    return {"agents": nearby[:20]}
