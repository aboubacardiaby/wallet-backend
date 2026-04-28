"""otp: store SHA-256 hash instead of plaintext code

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "otps",
        "code",
        type_=sa.String(64),
        existing_type=sa.String(10),
        nullable=False,
    )
    # Invalidate all existing plaintext OTPs — they can no longer be verified
    op.execute("DELETE FROM otps WHERE verified = false")


def downgrade() -> None:
    op.execute("DELETE FROM otps WHERE verified = false")
    op.alter_column(
        "otps",
        "code",
        type_=sa.String(10),
        existing_type=sa.String(64),
        nullable=False,
    )
