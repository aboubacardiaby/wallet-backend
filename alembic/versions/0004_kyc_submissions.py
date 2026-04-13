"""add kyc_submissions table

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-11
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kyc_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_kyc_submissions_user_id"),
            nullable=False,
        ),
        sa.Column("full_name",    sa.String(255), nullable=False, server_default=""),
        sa.Column("date_of_birth",sa.String(20),  nullable=False, server_default=""),
        sa.Column("nationality",  sa.String(100), nullable=False, server_default=""),
        sa.Column("address",      sa.String(500), nullable=False, server_default=""),
        sa.Column("city",         sa.String(100), nullable=False, server_default=""),
        sa.Column("country",      sa.String(100), nullable=False, server_default=""),
        sa.Column("id_type",      sa.String(50),  nullable=False, server_default=""),
        sa.Column("id_number",    sa.String(100), nullable=False, server_default=""),
        sa.Column("id_expiry",    sa.String(20),  nullable=False, server_default=""),
        sa.Column("id_front_url", sa.Text(),      nullable=False, server_default=""),
        sa.Column("id_back_url",  sa.Text(),      nullable=False, server_default=""),
        sa.Column("selfie_url",   sa.Text(),      nullable=False, server_default=""),
        sa.Column("status",       sa.String(20),  nullable=False, server_default="pending"),
        sa.Column("rejection_reason", sa.Text(),  nullable=True),
        sa.Column("reviewed_by",  sa.String(255), nullable=True),
        sa.Column("reviewed_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra",        postgresql.JSONB(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at",   sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_kyc_submissions_user_id", "kyc_submissions", ["user_id"])
    op.create_index("ix_kyc_submissions_status",  "kyc_submissions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_kyc_submissions_status",  table_name="kyc_submissions")
    op.drop_index("ix_kyc_submissions_user_id", table_name="kyc_submissions")
    op.drop_table("kyc_submissions")
