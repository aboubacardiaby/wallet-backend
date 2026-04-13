import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.wallet import Transaction, Wallet
from utils import row_to_dict

router = APIRouter(tags=["wallet"])


@router.get("/wallet/balance")
async def get_balance(token: dict = Depends(verify_token), db: AsyncSession = Depends(get_db)):
    wallet = await db.scalar(
        select(Wallet).where(Wallet.user_id == uuid.UUID(token["user_id"]))
    )
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    return row_to_dict(wallet, exclude=("id",))


@router.get("/wallet/transactions")
async def get_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    result = await db.execute(
        select(Transaction)
        .where(or_(Transaction.from_user_id == user_id, Transaction.to_user_id == user_id))
        .order_by(Transaction.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    rows = result.scalars().all()
    return {"transactions": [row_to_dict(tx) for tx in rows], "page": page, "limit": limit}


@router.get("/wallet/transaction/{transaction_id}")
async def get_transaction_detail(
    transaction_id: str,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    tx = await db.scalar(
        select(Transaction).where(Transaction.id == uuid.UUID(transaction_id))
    )
    if not tx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    if tx.from_user_id != user_id and tx.to_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return {"transaction": row_to_dict(tx)}
