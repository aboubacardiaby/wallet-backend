"""add routing_number and account_type to payment_methods

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_methods", sa.Column("routing_number", sa.String(20), nullable=False, server_default=""))
    op.add_column("payment_methods", sa.Column("account_type",   sa.String(20), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("payment_methods", "account_type")
    op.drop_column("payment_methods", "routing_number")
