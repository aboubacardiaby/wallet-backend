"""
Shared in-memory caches for rate overrides and fee rules.

Both are loaded from the DB on startup (via load_from_db) and refreshed
whenever the admin saves a change. The transfer handler reads these caches
to avoid a DB hit on every transfer.
"""
from __future__ import annotations

DEFAULT_FEE_RATE = 0.015   # 1.5 % global fallback


# ── Rate override cache ───────────────────────────────────────────────────────
# key: (from_ccy, to_ccy)   value: effective rate (override * (1 + spread))
_rate_overrides: dict[tuple[str, str], float] = {}


def set_rate_override(from_ccy: str, to_ccy: str, rate: float, spread_pct: float = 0.0) -> None:
    effective = round(float(rate) * (1 + float(spread_pct)), 8)
    _rate_overrides[(from_ccy.upper(), to_ccy.upper())] = effective


def clear_rate_override(from_ccy: str, to_ccy: str) -> None:
    _rate_overrides.pop((from_ccy.upper(), to_ccy.upper()), None)


def get_rate_override(from_ccy: str, to_ccy: str) -> float | None:
    return _rate_overrides.get((from_ccy.upper(), to_ccy.upper()))


# ── Fee rule cache ────────────────────────────────────────────────────────────
# Each entry is a dict with keys matching FeeRule columns
_fee_rules: list[dict] = []


def refresh_fee_rules(rules: list[dict]) -> None:
    global _fee_rules
    _fee_rules = sorted(
        [r for r in rules if r.get("is_active")],
        key=lambda r: -(r.get("priority") or 0),
    )


def calculate_fee(from_ccy: str, to_ccy: str, amount: float) -> dict:
    """
    Return {'fee_rate', 'fee_flat', 'fee', 'rule_name'} for the given corridor/amount.
    Checks fee rules in priority order; falls back to DEFAULT_FEE_RATE.
    """
    from_ccy = from_ccy.upper()
    to_ccy   = to_ccy.upper()

    for rule in _fee_rules:
        # Currency match
        if rule.get("from_currency") and rule["from_currency"].upper() != from_ccy:
            continue
        if rule.get("to_currency") and rule["to_currency"].upper() != to_ccy:
            continue
        # Amount range match
        if rule.get("min_amount") and amount < float(rule["min_amount"]):
            continue
        if rule.get("max_amount") and amount > float(rule["max_amount"]):
            continue

        fee_rate = float(rule.get("fee_rate") or 0)
        fee_flat = float(rule.get("fee_flat") or 0)
        fee      = round(amount * fee_rate + fee_flat, 2)
        if rule.get("min_fee") and fee < float(rule["min_fee"]):
            fee = round(float(rule["min_fee"]), 2)
        if rule.get("max_fee") and fee > float(rule["max_fee"]):
            fee = round(float(rule["max_fee"]), 2)

        return {
            "fee_rate": fee_rate,
            "fee_flat": fee_flat,
            "fee":      fee,
            "rule_name": rule.get("name"),
            "rule_id":   str(rule.get("id")),
        }

    # Default fallback
    return {
        "fee_rate":  DEFAULT_FEE_RATE,
        "fee_flat":  0.0,
        "fee":       round(amount * DEFAULT_FEE_RATE, 2),
        "rule_name": "default",
        "rule_id":   None,
    }


# ── DB loader (called at startup and after admin saves) ───────────────────────

async def load_from_db(db) -> None:
    """Populate both caches from the database."""
    from sqlalchemy import select
    from models.rate_override import RateOverride
    from models.fee_rule import FeeRule
    from utils import row_to_dict

    # Rate overrides
    overrides = await db.scalars(
        select(RateOverride).where(RateOverride.is_active == True)  # noqa: E712
    )
    for o in overrides:
        set_rate_override(o.from_currency, o.to_currency, float(o.rate), float(o.spread_pct))

    # Fee rules
    rules = await db.scalars(select(FeeRule))
    refresh_fee_rules([row_to_dict(r) for r in rules])
