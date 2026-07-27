"""add region to kyc_submissions

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kyc_submissions",
        sa.Column("region", sa.String(100), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("kyc_submissions", "region")
