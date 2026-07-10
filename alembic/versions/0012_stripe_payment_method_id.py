"""add stripe_payment_method_id to payment_methods

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_methods",
        sa.Column("stripe_payment_method_id", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_methods", "stripe_payment_method_id")
