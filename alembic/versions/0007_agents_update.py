"""agents: add country/status columns, make user_id nullable

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-24
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make user_id nullable and change ON DELETE to SET NULL
    op.drop_constraint("fk_agents_user_id_users", "agents", type_="foreignkey")
    op.alter_column("agents", "user_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_foreign_key(
        "fk_agents_user_id_users",
        "agents", "users",
        ["user_id"], ["id"],
        ondelete="SET NULL",
    )

    # Make latitude/longitude nullable-friendly with a server default
    op.alter_column("agents", "latitude",
                    existing_type=sa.Float(),
                    server_default="0",
                    existing_nullable=False)
    op.alter_column("agents", "longitude",
                    existing_type=sa.Float(),
                    server_default="0",
                    existing_nullable=False)

    # Add country column
    op.add_column("agents", sa.Column("country", sa.String(100), nullable=False, server_default=""))

    # Add status column
    op.add_column("agents", sa.Column("status", sa.String(20), nullable=False, server_default="active"))


def downgrade() -> None:
    op.drop_column("agents", "status")
    op.drop_column("agents", "country")

    op.drop_constraint("fk_agents_user_id_users", "agents", type_="foreignkey")
    op.alter_column("agents", "user_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.create_foreign_key(
        "fk_agents_user_id_users",
        "agents", "users",
        ["user_id"], ["id"],
        ondelete="CASCADE",
    )
