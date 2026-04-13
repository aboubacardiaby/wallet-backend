"""add payment_methods table

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-11
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_methods",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_payment_methods_user_id"),
            nullable=False,
        ),
        sa.Column("type",          sa.String(30),  nullable=False),
        sa.Column("label",         sa.String(255), nullable=False, server_default=""),
        sa.Column("card_brand",    sa.String(20),  nullable=False, server_default=""),
        sa.Column("last4",         sa.String(4),   nullable=False, server_default=""),
        sa.Column("expiry_month",  sa.Integer(),   nullable=True),
        sa.Column("expiry_year",   sa.Integer(),   nullable=True),
        sa.Column("holder_name",   sa.String(255), nullable=False, server_default=""),
        sa.Column("bank_name",     sa.String(255), nullable=False, server_default=""),
        sa.Column("account_last4", sa.String(4),   nullable=False, server_default=""),
        sa.Column("email",         sa.String(255), nullable=False, server_default=""),
        sa.Column("is_default",    sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("is_verified",   sa.Boolean(),   nullable=False, server_default="true"),
        sa.Column("metadata",      postgresql.JSONB(), nullable=True),
        sa.Column("created_at",    sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_payment_methods_user_id", "payment_methods", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_methods_user_id", table_name="payment_methods")
    op.drop_table("payment_methods")
