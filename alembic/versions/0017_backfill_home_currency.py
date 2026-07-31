"""backfill home_currency: correct users/wallets that were hard-defaulted
to XOF at registration regardless of their actual country (see the
/auth/verify-otp fix in handlers/auth.py — it used to ignore the
home_currency the client sent).

Derives the correct currency from each user's phone number country-code
prefix, matched against the seeded `countries` table (longest-dial-prefix
wins). Dial codes shared by more than one currency (e.g. +1 for both the
US and Canada) can't be disambiguated from the phone number alone, so
those are left untouched and reported instead of guessed.

Wallets with a nonzero balance are also left untouched — relabeling the
currency of money that's already moved requires a business decision
(convert the number or just relabel it) that this migration shouldn't
make silently. Those are reported for manual review too.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _build_dial_map(rows):
    """dial -> currency, or None if that dial prefix maps to >1 currency."""
    by_dial = {}
    for dial, currency in rows:
        by_dial.setdefault(dial, set()).add(currency)
    return {dial: (next(iter(ccys)) if len(ccys) == 1 else None) for dial, ccys in by_dial.items()}


def _match_currency(phone: str, dials_longest_first: list[str], dial_map: dict) -> tuple[str | None, bool]:
    """Returns (currency, ambiguous). currency is None if no dial matched."""
    for dial in dials_longest_first:
        if phone.startswith(dial):
            ccy = dial_map[dial]
            return (ccy, ccy is None)
    return (None, False)


def upgrade() -> None:
    conn = op.get_bind()

    country_rows = conn.execute(
        sa.text("SELECT dial, currency FROM countries WHERE dial IS NOT NULL AND currency IS NOT NULL")
    ).fetchall()
    dial_map = _build_dial_map(country_rows)
    dials_longest_first = sorted(dial_map.keys(), key=len, reverse=True)

    users = conn.execute(
        sa.text("""
            SELECT u.id AS user_id, u.phone_number, u.home_currency,
                   w.id AS wallet_id, w.balance, w.currency AS wallet_currency
            FROM users u
            JOIN wallets w ON w.user_id = u.id
        """)
    ).fetchall()

    fixed, skipped_nonzero, ambiguous, unmatched = [], [], [], []

    for u in users:
        correct_ccy, is_ambiguous = _match_currency(u.phone_number, dials_longest_first, dial_map)

        if is_ambiguous:
            ambiguous.append((u.phone_number, u.wallet_currency))
            continue
        if correct_ccy is None:
            unmatched.append((u.phone_number, u.wallet_currency))
            continue
        if correct_ccy == u.wallet_currency:
            continue
        if float(u.balance) != 0:
            skipped_nonzero.append((u.phone_number, u.wallet_currency, correct_ccy, u.balance))
            continue

        conn.execute(
            sa.text("UPDATE users SET home_currency = :ccy, updated_at = now() WHERE id = :id"),
            {"ccy": correct_ccy, "id": u.user_id},
        )
        conn.execute(
            sa.text("UPDATE wallets SET currency = :ccy, updated_at = now() WHERE id = :id"),
            {"ccy": correct_ccy, "id": u.wallet_id},
        )
        fixed.append((u.phone_number, u.wallet_currency, correct_ccy))

    if fixed:
        print(f"[0017] Backfilled {len(fixed)} user(s):")
        for phone, old, new in fixed:
            print(f"  - {phone}: {old} -> {new}")

    if skipped_nonzero:
        print(f"[0017] SKIPPED {len(skipped_nonzero)} user(s) with a nonzero balance — needs manual review:")
        for phone, old, new, bal in skipped_nonzero:
            print(f"  - {phone}: currently {old}, phone suggests {new}, balance={bal} {old}")

    if ambiguous:
        print(f"[0017] SKIPPED {len(ambiguous)} user(s) with an ambiguous dial code (e.g. +1):")
        for phone, old in ambiguous:
            print(f"  - {phone}: currently {old}")

    if unmatched:
        print(f"[0017] SKIPPED {len(unmatched)} user(s) whose phone matched no known country:")
        for phone, old in unmatched:
            print(f"  - {phone}: currently {old}")


def downgrade() -> None:
    # Not reversible — the prior (incorrect) values aren't recorded anywhere.
    pass
