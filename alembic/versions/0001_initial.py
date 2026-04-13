"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-11

"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("country_code", sa.String(10), server_default="", nullable=False),
        sa.Column("full_name", sa.String(255), server_default="", nullable=False),
        sa.Column("email", sa.String(255), server_default="", nullable=False),
        sa.Column("pin", sa.String(255), server_default="", nullable=False),
        sa.Column("pin_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_locked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("kyc_status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("profile_photo", sa.String(500), server_default="", nullable=False),
        sa.Column("national_id_type", sa.String(50), server_default="", nullable=False),
        sa.Column("national_id_number", sa.String(100), server_default="", nullable=False),
        sa.Column("date_of_birth", sa.DateTime(timezone=True), nullable=True),
        sa.Column("street", sa.String(255), server_default="", nullable=False),
        sa.Column("city", sa.String(100), server_default="", nullable=False),
        sa.Column("region", sa.String(100), server_default="", nullable=False),
        sa.Column("country", sa.String(100), server_default="", nullable=False),
        sa.Column("postal_code", sa.String(20), server_default="", nullable=False),
        sa.Column("preferred_lang", sa.String(10), server_default="fr", nullable=False),
        sa.Column("biometric_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("device_tokens", postgresql.ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("phone_number", name="uq_users_phone_number"),
    )
    op.create_index("ix_users_phone_number", "users", ["phone_number"])

    # --- otps ---
    op.create_table(
        "otps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("purpose", sa.String(50), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_otps"),
    )
    op.create_index("ix_otps_phone_number", "otps", ["phone_number"])

    # --- wallets ---
    op.create_table(
        "wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("currency", sa.String(10), server_default="XOF", nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("daily_limit", sa.Numeric(18, 2), server_default="500000", nullable=False),
        sa.Column("monthly_limit", sa.Numeric(18, 2), server_default="2000000", nullable=False),
        sa.Column("daily_spent", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("monthly_spent", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("last_reset_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_wallets_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_wallets"),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
    )
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])

    # --- agents ---
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_name", sa.String(255), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("address", sa.String(500), server_default="", nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("rating", sa.Float(), server_default="0", nullable=False),
        sa.Column("total_ratings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cash_in_limit", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("cash_out_limit", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("commission", sa.Float(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_agents_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
    )
    op.create_index("ix_agents_user_id", "agents", ["user_id"])
    op.create_index("ix_agents_location", "agents", ["latitude", "longitude"])

    # --- transactions ---
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("transaction_ref", sa.String(100), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("from_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_phone", sa.String(20), server_default="", nullable=False),
        sa.Column("to_phone", sa.String(20), server_default="", nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("fee", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(10), server_default="XOF", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("extra_data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"], name="fk_transactions_from_user_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"], name="fk_transactions_to_user_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name="fk_transactions_agent_id_agents", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
        sa.UniqueConstraint("transaction_ref", name="uq_transactions_transaction_ref"),
    )
    op.create_index("ix_transactions_transaction_ref", "transactions", ["transaction_ref"])
    op.create_index("ix_transactions_from_user_id", "transactions", ["from_user_id"])
    op.create_index("ix_transactions_to_user_id", "transactions", ["to_user_id"])
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])

    # --- money_requests ---
    op.create_table(
        "money_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("from_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"], name="fk_money_requests_from_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"], name="fk_money_requests_to_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_money_requests"),
    )
    op.create_index("ix_money_requests_from_user_id", "money_requests", ["from_user_id"])
    op.create_index("ix_money_requests_to_user_id", "money_requests", ["to_user_id"])

    # --- notifications ---
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("is_read", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notifications_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("money_requests")
    op.drop_table("transactions")
    op.drop_table("agents")
    op.drop_table("wallets")
    op.drop_table("otps")
    op.drop_table("users")
