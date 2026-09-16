"""Atomic agent cash top-up application service (FR-012/FR-021)."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import os
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.topup import AgentFloatAccount, TopUp
from models.topup import LedgerEntryRecord, LedgerTransactionRecord
from models.wallet import Agent, Transaction, Wallet
from services.wallet_policy import credit, money
from services.topup.ledger import LedgerEntry, post_ledger_transaction
from services.topup.money import Money
from services.topup.state_machine import transition


def hash_confirmation(code: str) -> str:
    return hashlib.sha256(f"{os.getenv('OTP_PEPPER', '')}{code.strip()}".encode()).hexdigest()


async def apply_agent_cash_topup(db: AsyncSession, *, agent_user_id: uuid.UUID,
                                 top_up_id: uuid.UUID, confirmation_code: str) -> TopUp:
    """Validate the agent and one-time confirmation, then move float atomically."""
    agent = await db.scalar(select(Agent).where(
        Agent.user_id == agent_user_id, Agent.is_active.is_(True), Agent.status == "active"
    ).with_for_update())
    if agent is None:
        raise HTTPException(403, "Authenticated user is not an active agent")
    top_up = await db.scalar(select(TopUp).where(TopUp.id == top_up_id).with_for_update())
    if top_up is None or top_up.funding_method != "agent_cash":
        raise HTTPException(404, "Agent cash top-up not found")
    if top_up.status in {"Completed", "Cancelled", "Failed", "Expired", "Reversed", "UnderReview"}:
        return top_up
    now = datetime.now(timezone.utc)
    if not top_up.confirmation_code_hash or not top_up.confirmation_expires_at or top_up.confirmation_expires_at <= now:
        top_up.status = "Cancelled"
        top_up.updated_at = now
        await db.commit()
        raise HTTPException(409, "Customer confirmation expired")
    if not hmac.compare_digest(top_up.confirmation_code_hash, hash_confirmation(confirmation_code)):
        raise HTTPException(403, "Invalid customer confirmation")
    wallet = await db.scalar(select(Wallet).where(Wallet.id == top_up.wallet_id).with_for_update())
    float_account = await db.scalar(select(AgentFloatAccount).where(
        AgentFloatAccount.agent_id == agent.id
    ).with_for_update())
    if wallet is None or wallet.status != "active":
        raise HTTPException(409, "Wallet unavailable")
    if float_account is None or float_account.currency != wallet.currency or float_account.currency != top_up.currency:
        raise HTTPException(409, "Agent float currency mismatch")
    gross = money(top_up.gross_amount)
    net = money(top_up.net_amount)
    if money(float_account.balance) < gross:
        raise HTTPException(409, "Insufficient agent float")
    if top_up.status == "Pending":
        top_up.status = transition(top_up.status, "Processing")
    ledger = post_ledger_transaction(
        source_reference=f"ledger_{top_up.internal_reference}",
        entries=(LedgerEntry("agent_float", "debit", Money(gross, top_up.currency)),
                 LedgerEntry("customer_wallet", "credit", Money(net, top_up.currency)),
                 *(([LedgerEntry("fee_income", "credit", Money(gross-net, top_up.currency))]
                    if gross > net else []))),
    )
    ledger_record = LedgerTransactionRecord(source_reference=ledger.source_reference,
        top_up_id=top_up.id, currency=top_up.currency, is_posted=False)
    db.add(ledger_record)
    await db.flush()
    for entry in ledger.entries:
        db.add(LedgerEntryRecord(ledger_transaction_id=ledger_record.id,
            account_code=entry.account, direction=entry.direction, amount=entry.amount.amount))
    await db.flush()
    ledger_record.is_posted = True
    ledger_record.posted_at = now
    float_account.balance = money(float_account.balance) - gross
    credit(wallet, net)
    top_up.agent_id = agent.id
    top_up.confirmed_at = now
    top_up.confirmation_code_hash = None
    top_up.confirmation_expires_at = None
    top_up.status = transition(top_up.status, "Completed")
    top_up.completed_at = now
    top_up.updated_at = now
    tx = Transaction(transaction_ref=f"agent_{uuid.uuid4().hex}", type="cash_in", status="completed",
                     to_user_id=wallet.user_id, amount=net, fee=top_up.fee_amount or 0,
                     total_amount=top_up.gross_amount, currency=wallet.currency, agent_id=agent.id,
                     completed_at=now)
    db.add(tx)
    await db.commit()
    await db.refresh(top_up)
    return top_up
