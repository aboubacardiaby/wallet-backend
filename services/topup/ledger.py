"""Top-up domain ledger primitives (T007): balanced, immutable double-entry
posting (Constitution I; FR-009, FR-010, FR-011).

Entries and transactions are frozen dataclasses, so "posted entries MUST NOT
be edited or deleted" is an executable property (dataclasses.FrozenInstanceError
on mutation) rather than a convention callers must remember. A correction is
only reachable by posting a new, separately linked transaction -- see
services/topup/reversal.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from services.topup.money import Money


class UnbalancedLedgerError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerEntry:
    account: str
    direction: str
    amount: Money

    def __post_init__(self):
        if self.direction not in ("debit", "credit"):
            raise ValueError(f"invalid ledger entry direction: {self.direction!r}")
        if self.amount.amount <= 0:
            raise ValueError(f"ledger entry amount must be positive: {self.amount.amount}")


@dataclass(frozen=True)
class LedgerTransaction:
    source_reference: str
    entries: Tuple[LedgerEntry, ...]
    reverses: Optional[str] = None


def _validate_balance(entries: Tuple[LedgerEntry, ...]) -> None:
    if not entries:
        raise UnbalancedLedgerError("a ledger transaction requires at least one entry")
    currencies = {e.amount.currency for e in entries}
    if len(currencies) != 1:
        raise UnbalancedLedgerError("ledger entries must share a single currency")
    debits = sum((e.amount.amount for e in entries if e.direction == "debit"), Decimal("0"))
    credits = sum((e.amount.amount for e in entries if e.direction == "credit"), Decimal("0"))
    if debits != credits:
        raise UnbalancedLedgerError(f"unbalanced ledger transaction: debits={debits} credits={credits}")


def post_ledger_transaction(
    source_reference: str,
    entries,
    reverses: Optional[str] = None,
) -> LedgerTransaction:
    entries = tuple(entries)
    _validate_balance(entries)
    return LedgerTransaction(source_reference=source_reference, entries=entries, reverses=reverses)
