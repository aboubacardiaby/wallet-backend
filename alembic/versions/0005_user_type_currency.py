"""add user_type and home_currency to users

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "user_type", sa.String(20), nullable=False, server_default="receiver"
    ))
    op.add_column("users", sa.Column(
        "home_currency", sa.String(10), nullable=False, server_default="XOF"
    ))


def downgrade() -> None:
    op.drop_column("users", "home_currency")
    op.drop_column("users", "user_type")
