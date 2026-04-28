import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import verify_token
from models.user import User
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

    # Batch-resolve names: by user ID (wallet transfers) and by phone (wave/cash)
    recipient_ids = {tx.to_user_id for tx in rows if tx.to_user_id and tx.to_user_id != user_id}
    name_by_id: dict[uuid.UUID, str] = {}
    if recipient_ids:
        id_users = await db.scalars(select(User).where(User.id.in_(recipient_ids)))
        for u in id_users:
            if u.full_name:
                name_by_id[u.id] = u.full_name

    # Phones where stored recipient_name is missing or equals the phone itself
    phones_to_lookup: set[str] = set()
    for tx in rows:
        if tx.to_user_id and tx.to_user_id != user_id:
            continue  # already covered by name_by_id
        if tx.to_phone:
            extra = tx.extra_data or {}
            stored_name = extra.get("recipient_name", "")
            if not stored_name or stored_name == tx.to_phone:
                phones_to_lookup.add(tx.to_phone)

    name_by_phone: dict[str, str] = {}
    if phones_to_lookup:
        phone_users = await db.scalars(
            select(User).where(User.phone_number.in_(phones_to_lookup))
        )
        for u in phone_users:
            if u.full_name:
                name_by_phone[u.phone_number] = u.full_name

    def enrich(tx):
        d = row_to_dict(tx)
        extra = d.get("extra_data") or {}
        stored_name = extra.get("recipient_name", "")

        # Upgrade phone-as-name or missing name with the real full name when available
        if not stored_name or stored_name == tx.to_phone:
            resolved = (
                name_by_id.get(tx.to_user_id)
                or name_by_phone.get(tx.to_phone)
            )
            if resolved:
                extra["recipient_name"] = resolved
            elif not stored_name and tx.to_phone:
                # Ensure the field is always present — use phone as last resort
                extra["recipient_name"] = tx.to_phone
            d["extra_data"] = extra
        return d

    return {"transactions": [enrich(tx) for tx in rows], "page": page, "limit": limit}


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
