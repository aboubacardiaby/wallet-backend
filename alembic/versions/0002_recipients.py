"""add recipients table

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_recipients_user_id"),
            nullable=False,
        ),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("nickname", sa.String(100), nullable=False, server_default=""),
        sa.Column("avatar_color", sa.String(20), nullable=False, server_default="#6366f1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_recipients_user_id", "recipients", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_recipients_user_id", table_name="recipients")
    op.drop_table("recipients")
