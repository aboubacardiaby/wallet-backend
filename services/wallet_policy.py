from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException


MONEY_QUANTUM = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def reset_spending_periods(wallet, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    last = wallet.last_reset_date
    if last is None:
        wallet.daily_spent = money(0)
        wallet.monthly_spent = money(0)
        wallet.last_reset_date = now
        return
    last_date = last.date()
    if last_date != now.date():
        wallet.daily_spent = money(0)
    if (last_date.year, last_date.month) != (now.year, now.month):
        wallet.monthly_spent = money(0)
    if last_date != now.date():
        wallet.last_reset_date = now


def ensure_can_spend(wallet, amount) -> Decimal:
    amount = money(amount)
    reset_spending_periods(wallet)
    balance = money(wallet.balance)
    daily_spent = money(wallet.daily_spent)
    monthly_spent = money(wallet.monthly_spent)
    if balance < amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    if daily_spent + amount > money(wallet.daily_limit):
        raise HTTPException(status_code=400, detail="Daily spending limit exceeded")
    if monthly_spent + amount > money(wallet.monthly_limit):
        raise HTTPException(status_code=400, detail="Monthly spending limit exceeded")
    return amount


def debit(wallet, amount) -> Decimal:
    amount = ensure_can_spend(wallet, amount)
    wallet.balance = money(wallet.balance) - amount
    wallet.daily_spent = money(wallet.daily_spent) + amount
    wallet.monthly_spent = money(wallet.monthly_spent) + amount
    wallet.updated_at = datetime.now(timezone.utc)
    return amount


def credit(wallet, amount) -> Decimal:
    amount = money(amount)
    wallet.balance = money(wallet.balance) + amount
    wallet.updated_at = datetime.now(timezone.utc)
    return amount
