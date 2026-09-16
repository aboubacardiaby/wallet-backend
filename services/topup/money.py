"""Top-up domain Money and fee calculation (T007; FR-003).

Implements the contract pinned by tests/test_topup_money.py. Quantization
reuses services/wallet_policy.py's Numeric(18,2) half-up convention; the fee
shape mirrors models/fee_rule.py (rate + flat, clamped to [min_fee, max_fee],
highest-priority active matching rule wins, 1.5% documented default), per the
T002 documented assumption in spec.md that top-up fees reuse the existing
transfer fee-rule mechanism.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

MONEY_QUANTUM = Decimal("0.01")
DEFAULT_FEE_RATE = Decimal("0.015")
CURRENCY_CODE_PATTERN = re.compile(r"[A-Z]{3}")


class CurrencyMismatchError(ValueError):
    pass


class InvalidCurrencyError(ValueError):
    pass


class InvalidFeeError(ValueError):
    pass


def _quantize(value) -> Decimal:
    if isinstance(value, float):
        raise TypeError(
            "Money must not be constructed from a binary float; pass a str, int, or Decimal"
        )
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if not CURRENCY_CODE_PATTERN.fullmatch(self.currency):
            raise InvalidCurrencyError(f"invalid currency code: {self.currency!r}")
        object.__setattr__(self, "amount", _quantize(self.amount))

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} and {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} and {other.currency}")
        return Money(self.amount - other.amount, self.currency)


def net_credit(gross: Money, fee: Money) -> Money:
    if fee.amount < 0 or fee.amount > gross.amount:
        raise InvalidFeeError(f"fee {fee.amount} must be between 0 and gross amount {gross.amount}")
    return gross - fee


def _matches(rule, gross: Money) -> bool:
    if not rule.is_active:
        return False
    if rule.from_currency is not None and rule.from_currency != gross.currency:
        return False
    if rule.to_currency is not None and rule.to_currency != gross.currency:
        return False
    if rule.min_amount is not None and gross.amount < _quantize(rule.min_amount):
        return False
    if rule.max_amount is not None and gross.amount > _quantize(rule.max_amount):
        return False
    return True


def _validate_rule_components(rule) -> None:
    if Decimal(str(rule.fee_rate)) < 0:
        raise InvalidFeeError(f"fee_rate must not be negative: {rule.fee_rate}")
    if _quantize(rule.fee_flat or 0) < 0:
        raise InvalidFeeError(f"fee_flat must not be negative: {rule.fee_flat}")
    if rule.min_fee is not None and _quantize(rule.min_fee) < 0:
        raise InvalidFeeError(f"min_fee must not be negative: {rule.min_fee}")
    if rule.max_fee is not None and _quantize(rule.max_fee) < 0:
        raise InvalidFeeError(f"max_fee must not be negative: {rule.max_fee}")
    if (
        rule.min_fee is not None
        and rule.max_fee is not None
        and _quantize(rule.min_fee) > _quantize(rule.max_fee)
    ):
        raise InvalidFeeError(f"min_fee {rule.min_fee} must not exceed max_fee {rule.max_fee}")


def calculate_fee(gross: Money, rules: Optional[Iterable] = None) -> Money:
    matching = sorted(
        (r for r in (rules or []) if _matches(r, gross)),
        key=lambda r: r.priority,
        reverse=True,
    )
    if matching:
        rule = matching[0]
        _validate_rule_components(rule)
        rate = Decimal(str(rule.fee_rate))
        flat = _quantize(rule.fee_flat or 0)
        fee = _quantize(gross.amount * rate + flat)
        if rule.min_fee is not None:
            fee = max(fee, _quantize(rule.min_fee))
        if rule.max_fee is not None:
            fee = min(fee, _quantize(rule.max_fee))
    else:
        fee = _quantize(gross.amount * DEFAULT_FEE_RATE)
    if fee < 0:
        raise InvalidFeeError(f"fee must not be negative: {fee}")
    if fee > gross.amount:
        raise InvalidFeeError(f"fee {fee} must not exceed gross amount {gross.amount}")
    return Money(fee, gross.currency)
