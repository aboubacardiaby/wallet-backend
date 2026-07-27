"""countries: reference table for nationality/country pickers

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-26
"""
import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# code, name, dial, flag, currency
_COUNTRIES = [
    # West Africa (prioritised)
    ("SN", "Senegal",               "+221", "\U0001F1F8\U0001F1F3", "XOF"),
    ("CI", "Côte d'Ivoire",         "+225", "\U0001F1E8\U0001F1EE", "XOF"),
    ("ML", "Mali",                  "+223", "\U0001F1F2\U0001F1F1", "XOF"),
    ("GN", "Guinea",                "+224", "\U0001F1EC\U0001F1F3", "GNF"),
    ("BF", "Burkina Faso",          "+226", "\U0001F1E7\U0001F1EB", "XOF"),
    ("NE", "Niger",                 "+227", "\U0001F1F3\U0001F1EA", "XOF"),
    ("TG", "Togo",                  "+228", "\U0001F1F9\U0001F1EC", "XOF"),
    ("BJ", "Benin",                 "+229", "\U0001F1E7\U0001F1EF", "XOF"),
    ("MR", "Mauritania",            "+222", "\U0001F1F2\U0001F1F7", "MRU"),
    ("GM", "Gambia",                "+220", "\U0001F1EC\U0001F1F2", "GMD"),
    ("GW", "Guinea-Bissau",         "+245", "\U0001F1EC\U0001F1FC", "XOF"),
    ("CV", "Cape Verde",            "+238", "\U0001F1E8\U0001F1FB", "CVE"),
    ("SL", "Sierra Leone",          "+232", "\U0001F1F8\U0001F1F1", "SLL"),
    ("LR", "Liberia",               "+231", "\U0001F1F1\U0001F1F7", "LRD"),
    ("GH", "Ghana",                 "+233", "\U0001F1EC\U0001F1ED", "GHS"),
    ("NG", "Nigeria",               "+234", "\U0001F1F3\U0001F1EC", "NGN"),
    # Rest of Africa
    ("DZ", "Algeria",               "+213", "\U0001F1E9\U0001F1FF", "DZD"),
    ("AO", "Angola",                "+244", "\U0001F1E6\U0001F1F4", "AOA"),
    ("CM", "Cameroon",              "+237", "\U0001F1E8\U0001F1F2", "XAF"),
    ("CF", "Central African Rep.",  "+236", "\U0001F1E8\U0001F1EB", "XAF"),
    ("TD", "Chad",                  "+235", "\U0001F1F9\U0001F1E9", "XAF"),
    ("CG", "Congo",                 "+242", "\U0001F1E8\U0001F1EC", "XAF"),
    ("CD", "DR Congo",              "+243", "\U0001F1E8\U0001F1E9", "CDF"),
    ("EG", "Egypt",                 "+20",  "\U0001F1EA\U0001F1EC", "EGP"),
    ("ET", "Ethiopia",              "+251", "\U0001F1EA\U0001F1F9", "ETB"),
    ("GA", "Gabon",                 "+241", "\U0001F1EC\U0001F1E6", "XAF"),
    ("KE", "Kenya",                 "+254", "\U0001F1F0\U0001F1EA", "KES"),
    ("MG", "Madagascar",            "+261", "\U0001F1F2\U0001F1EC", "MGA"),
    ("MA", "Morocco",               "+212", "\U0001F1F2\U0001F1E6", "MAD"),
    ("MZ", "Mozambique",            "+258", "\U0001F1F2\U0001F1FF", "MZN"),
    ("RW", "Rwanda",                "+250", "\U0001F1F7\U0001F1FC", "RWF"),
    ("ZA", "South Africa",          "+27",  "\U0001F1FF\U0001F1E6", "ZAR"),
    ("SD", "Sudan",                 "+249", "\U0001F1F8\U0001F1E9", "SDG"),
    ("TZ", "Tanzania",              "+255", "\U0001F1F9\U0001F1FF", "TZS"),
    ("TN", "Tunisia",               "+216", "\U0001F1F9\U0001F1F3", "TND"),
    ("UG", "Uganda",                "+256", "\U0001F1FA\U0001F1EC", "UGX"),
    ("ZM", "Zambia",                "+260", "\U0001F1FF\U0001F1F2", "ZMW"),
    ("ZW", "Zimbabwe",              "+263", "\U0001F1FF\U0001F1FC", "ZWL"),
    # Europe & Americas
    ("FR", "France",                "+33",  "\U0001F1EB\U0001F1F7", "EUR"),
    ("BE", "Belgium",               "+32",  "\U0001F1E7\U0001F1EA", "EUR"),
    ("CH", "Switzerland",           "+41",  "\U0001F1E8\U0001F1ED", "CHF"),
    ("DE", "Germany",               "+49",  "\U0001F1E9\U0001F1EA", "EUR"),
    ("ES", "Spain",                 "+34",  "\U0001F1EA\U0001F1F8", "EUR"),
    ("GB", "United Kingdom",        "+44",  "\U0001F1EC\U0001F1E7", "GBP"),
    ("IT", "Italy",                 "+39",  "\U0001F1EE\U0001F1F9", "EUR"),
    ("PT", "Portugal",              "+351", "\U0001F1F5\U0001F1F9", "EUR"),
    ("US", "United States",         "+1",   "\U0001F1FA\U0001F1F8", "USD"),
    ("CA", "Canada",                "+1",   "\U0001F1E8\U0001F1E6", "CAD"),
    # Middle East & Asia
    ("AE", "UAE",                   "+971", "\U0001F1E6\U0001F1EA", "AED"),
    ("SA", "Saudi Arabia",          "+966", "\U0001F1F8\U0001F1E6", "SAR"),
    ("CN", "China",                 "+86",  "\U0001F1E8\U0001F1F3", "CNY"),
    ("IN", "India",                 "+91",  "\U0001F1EE\U0001F1F3", "INR"),
]


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("id",         PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("code",       sa.String(2),   nullable=False, unique=True),
        sa.Column("name",       sa.String(100), nullable=False),
        sa.Column("dial",       sa.String(10),  nullable=True),
        sa.Column("flag",       sa.String(10),  nullable=True),
        sa.Column("currency",   sa.String(10),  nullable=True),
        sa.Column("is_active",  sa.Boolean(),   server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    countries_table = sa.table(
        "countries",
        sa.column("id", PG_UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("dial", sa.String),
        sa.column("flag", sa.String),
        sa.column("currency", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        countries_table,
        [
            {
                "id": uuid.uuid4(), "code": code, "name": name, "dial": dial,
                "flag": flag, "currency": currency, "created_at": now, "updated_at": now,
            }
            for code, name, dial, flag, currency in _COUNTRIES
        ],
    )


def downgrade() -> None:
    op.drop_table("countries")
