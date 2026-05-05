"""ach_config: singleton ACH provider config table

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-30
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ach_config",
        sa.Column("id",                      sa.Integer(),  primary_key=True),
        sa.Column("api_base_url",            sa.String(500), server_default="http://localhost:3000/v1", nullable=False),
        sa.Column("api_key",                 sa.String(500), server_default="", nullable=False),
        sa.Column("platform_account_number", sa.String(50),  server_default="", nullable=False),
        sa.Column("platform_routing_number", sa.String(9),   server_default="", nullable=False),
        sa.Column("platform_account_type",   sa.String(20),  server_default="CHECKING", nullable=False),
        sa.Column("platform_account_name",   sa.String(100), server_default="Kalipeh Platform", nullable=False),
        sa.Column("enabled",                 sa.Boolean(),   server_default="false", nullable=False),
        sa.Column("updated_at",              sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute("INSERT INTO ach_config (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("ach_config")
