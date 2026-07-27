"""add Wave payout configuration

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wave_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("api_base_url", sa.String(500), nullable=False, server_default="https://api.wave.com"),
        sa.Column("api_key", sa.String(500), nullable=False, server_default=""),
        sa.Column("business_country", sa.String(100), nullable=False, server_default="Senegal"),
        sa.Column("business_currency", sa.String(10), nullable=False, server_default="XOF"),
        sa.Column("aggregated_merchant_id", sa.String(100), nullable=False, server_default=""),
        sa.Column("verify_recipient", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("wave_config")
