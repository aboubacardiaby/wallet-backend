"""Top-up reversal domain rules (T007; FR-013, FR-014) with a pluggable
insufficient-balance policy hook.

Default policy blocks the automatic reversal instead of allowing a negative
balance -- the interim safe default documented in spec.md's "Still needs
clarification" section, kept swappable so a human-approved overdraft policy
can replace it later without changing this module's core posting logic.
"""
from __future__ import annotations

from typing import Callable, Optional

from services.topup.ledger import LedgerEntry, LedgerTransaction, post_ledger_transaction
from services.topup.money import CurrencyMismatchError, Money

OPPOSITE_DIRECTION = {"debit": "credit", "credit": "debit"}


class ReversalBlockedError(ValueError):
    pass


class ReversalMismatchError(ValueError):
    pass


def block_on_insufficient_balance(balance: Money, reversal_amount: Money) -> bool:
    if balance.currency != reversal_amount.currency:
        raise CurrencyMismatchError(
            f"cannot compare balance in {balance.currency} against reversal amount in {reversal_amount.currency}"
        )
    return balance.amount < reversal_amount.amount


def reverse_topup(
    original: LedgerTransaction,
    current_balance: Money,
    policy_hook: Callable[[Money, Money], bool] = block_on_insufficient_balance,
    already_reversed: Optional[LedgerTransaction] = None,
) -> LedgerTransaction:
    if already_reversed is not None:
        if already_reversed.reverses != original.source_reference:
            raise ReversalMismatchError(
                f"{already_reversed.source_reference} does not reverse {original.source_reference}"
            )
        return already_reversed

    wallet_entry = next(e for e in original.entries if e.account == "customer_wallet")
    reversal_amount = wallet_entry.amount

    if current_balance.currency != reversal_amount.currency:
        raise CurrencyMismatchError(
            f"cannot compare balance in {current_balance.currency} "
            f"against reversal amount in {reversal_amount.currency}"
        )

    if policy_hook(current_balance, reversal_amount):
        raise ReversalBlockedError(
            f"insufficient balance to reverse {original.source_reference}: "
            f"balance={current_balance.amount} amount={reversal_amount.amount}"
        )

    compensating_entries = tuple(
        LedgerEntry(account=e.account, direction=OPPOSITE_DIRECTION[e.direction], amount=e.amount)
        for e in original.entries
    )
    return post_ledger_transaction(
        source_reference=f"reversal:{original.source_reference}",
        entries=compensating_entries,
        reverses=original.source_reference,
    )
