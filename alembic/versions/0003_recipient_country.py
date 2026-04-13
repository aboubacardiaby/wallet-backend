"""add country columns to recipients

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recipients", sa.Column("country_code", sa.String(10), nullable=False, server_default="SN"))
    op.add_column("recipients", sa.Column("country_name", sa.String(100), nullable=False, server_default="Senegal"))


def downgrade() -> None:
    op.drop_column("recipients", "country_name")
    op.drop_column("recipients", "country_code")
