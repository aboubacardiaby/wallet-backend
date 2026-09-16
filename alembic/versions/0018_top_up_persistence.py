"""add top-up, provider-event, immutable-ledger, reversal, and agent-float storage

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-18

Downgrade is structural and destructive: it drops only the new T009 objects.
Production rollback after top-up data exists must use a backup/forward fix.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "top_ups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("internal_reference", sa.String(80), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("funding_method", sa.String(32), nullable=False),
        sa.Column("provider_name", sa.String(80), nullable=True),
        sa.Column("provider_transaction_reference", sa.String(255), nullable=True),
        sa.Column("gross_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("fee_amount", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("net_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), server_default="Pending", nullable=False),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("confirmation_code_hash", sa.String(64), nullable=True),
        sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version_id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("gross_amount > 0", name=op.f("ck_top_ups_gross_amount_positive")),
        sa.CheckConstraint("fee_amount >= 0", name=op.f("ck_top_ups_fee_amount_nonnegative")),
        sa.CheckConstraint("net_amount >= 0", name=op.f("ck_top_ups_net_amount_nonnegative")),
        sa.CheckConstraint("net_amount = gross_amount - fee_amount", name=op.f("ck_top_ups_amounts_reconcile")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_top_ups_currency_format")),
        sa.CheckConstraint("version_id > 0", name=op.f("ck_top_ups_version_positive")),
        sa.CheckConstraint("status IN ('Created','Pending','Processing','RequiresAction','Completed','Failed','Expired','Cancelled','Reversed','UnderReview')", name=op.f("ck_top_ups_status_valid")),
        sa.CheckConstraint("funding_method IN ('card','bank_transfer','mobile_money','agent_cash')", name=op.f("ck_top_ups_funding_method_valid")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name=op.f("fk_top_ups_agent_id_agents"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], name=op.f("fk_top_ups_wallet_id_wallets"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_top_ups")),
        sa.UniqueConstraint("internal_reference", name=op.f("uq_top_ups_internal_reference")),
        sa.UniqueConstraint("wallet_id", "idempotency_key", name="uq_top_ups_wallet_idempotency"),
        sa.UniqueConstraint("provider_name", "provider_transaction_reference", name="uq_top_ups_provider_transaction"),
    )
    op.create_index("ix_top_ups_wallet_created", "top_ups", ["wallet_id", "created_at"])
    op.create_index("ix_top_ups_status_updated", "top_ups", ["status", "updated_at"])
    op.create_index("ix_top_ups_agent_created", "top_ups", ["agent_id", "created_at"])

    op.create_table(
        "provider_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_name", sa.String(80), nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("top_up_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("sanitized_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("processing_status", sa.String(20), server_default="received", nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("processing_status IN ('received','processed','ignored','failed')", name=op.f("ck_provider_events_processing_status_valid")),
        sa.ForeignKeyConstraint(["top_up_id"], ["top_ups.id"], name=op.f("fk_provider_events_top_up_id_top_ups"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_events")),
        sa.UniqueConstraint("provider_name", "provider_event_id", name="uq_provider_events_identity"),
    )
    op.create_index(op.f("ix_provider_events_top_up_id"), "provider_events", ["top_up_id"])
    op.create_index("ix_provider_events_status_received", "provider_events", ["processing_status", "received_at"])

    op.create_table(
        "ledger_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_reference", sa.String(120), nullable=False),
        sa.Column("top_up_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("is_posted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_ledger_transactions_currency_format")),
        sa.CheckConstraint("(is_posted AND posted_at IS NOT NULL) OR (NOT is_posted AND posted_at IS NULL)", name=op.f("ck_ledger_transactions_posted_state_consistent")),
        sa.ForeignKeyConstraint(["top_up_id"], ["top_ups.id"], name=op.f("fk_ledger_transactions_top_up_id_top_ups"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_transactions")),
        sa.UniqueConstraint("source_reference", name=op.f("uq_ledger_transactions_source_reference")),
        sa.UniqueConstraint("top_up_id", name=op.f("uq_ledger_transactions_top_up_id")),
    )
    op.create_index("ix_ledger_transactions_top_up", "ledger_transactions", ["top_up_id"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_code", sa.String(100), nullable=False),
        sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("direction IN ('debit','credit')", name=op.f("ck_ledger_entries_direction_valid")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_ledger_entries_amount_positive")),
        sa.ForeignKeyConstraint(["ledger_transaction_id"], ["ledger_transactions.id"], name=op.f("fk_ledger_entries_ledger_transaction_id_ledger_transactions"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
    )
    op.create_index("ix_ledger_entries_transaction", "ledger_entries", ["ledger_transaction_id"])
    op.create_index("ix_ledger_entries_account", "ledger_entries", ["account_code", "created_at"])

    op.create_table(
        "top_up_reversals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_top_up_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reversal_top_up_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("original_top_up_id <> reversal_top_up_id", name=op.f("ck_top_up_reversals_different_top_ups")),
        sa.ForeignKeyConstraint(["original_top_up_id"], ["top_ups.id"], name=op.f("fk_top_up_reversals_original_top_up_id_top_ups"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reversal_top_up_id"], ["top_ups.id"], name=op.f("fk_top_up_reversals_reversal_top_up_id_top_ups"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_top_up_reversals")),
        sa.UniqueConstraint("original_top_up_id", name="uq_top_up_reversals_original"),
        sa.UniqueConstraint("reversal_top_up_id", name="uq_top_up_reversals_reversal"),
    )

    op.create_table(
        "agent_float_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("version_id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("balance >= 0", name=op.f("ck_agent_float_accounts_balance_nonnegative")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_agent_float_accounts_currency_format")),
        sa.CheckConstraint("version_id > 0", name=op.f("ck_agent_float_accounts_version_positive")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name=op.f("fk_agent_float_accounts_agent_id_agents"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_float_accounts")),
        sa.UniqueConstraint("agent_id", name=op.f("uq_agent_float_accounts_agent_id")),
    )

    op.execute("""
        CREATE FUNCTION guard_ledger_entry_mutation() RETURNS trigger AS $$
        DECLARE posted boolean; target_id uuid;
        BEGIN
            target_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.ledger_transaction_id
                              ELSE NEW.ledger_transaction_id END;
            SELECT is_posted INTO posted FROM ledger_transactions WHERE id = target_id;
            IF posted THEN
                RAISE EXCEPTION 'posted ledger entries are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_ledger_entry_mutation
        BEFORE INSERT OR UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION guard_ledger_entry_mutation();
    """)
    op.execute("""
        CREATE FUNCTION guard_ledger_transaction_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.is_posted THEN
                RAISE EXCEPTION 'posted ledger transactions are immutable';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.is_posted THEN
                RAISE EXCEPTION 'posted ledger transactions are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_ledger_transaction_mutation
        BEFORE UPDATE OR DELETE ON ledger_transactions
        FOR EACH ROW EXECUTE FUNCTION guard_ledger_transaction_mutation();
    """)
    op.execute("""
        CREATE FUNCTION validate_posted_ledger_balance() RETURNS trigger AS $$
        DECLARE target_id uuid; posted boolean; entry_count integer; debits numeric; credits numeric;
        BEGIN
            -- Get the target ledger transaction ID based on table and operation
            IF TG_TABLE_NAME = 'ledger_transactions' THEN
                IF TG_OP = 'DELETE' THEN
                    target_id := OLD.id;
                ELSE
                    target_id := NEW.id;
                END IF;
            ELSIF TG_TABLE_NAME = 'ledger_entries' THEN
                IF TG_OP = 'DELETE' THEN
                    target_id := OLD.ledger_transaction_id;
                ELSE
                    target_id := NEW.ledger_transaction_id;
                END IF;
            ELSE
                RETURN NULL;
            END IF;
            
            -- Check if the transaction is posted
            SELECT is_posted INTO posted FROM ledger_transactions WHERE id = target_id;
            IF NOT COALESCE(posted, false) THEN RETURN NULL; END IF;
            
            -- Validate balance for posted transactions
            SELECT COUNT(*),
                   COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0),
                   COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)
              INTO entry_count, debits, credits
              FROM ledger_entries WHERE ledger_transaction_id = target_id;
            IF entry_count < 2 OR debits <> credits THEN
                RAISE EXCEPTION 'posted ledger transaction % is unbalanced: debits %, credits %', target_id, debits, credits;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_validate_ledger_transaction_balance
        AFTER INSERT OR UPDATE ON ledger_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_posted_ledger_balance();
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_validate_ledger_entry_balance
        AFTER INSERT OR UPDATE OR DELETE ON ledger_entries DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_posted_ledger_balance();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_validate_ledger_entry_balance ON ledger_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_ledger_transaction_balance ON ledger_transactions")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_ledger_entry_mutation ON ledger_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_ledger_transaction_mutation ON ledger_transactions")
    op.execute("DROP FUNCTION IF EXISTS validate_posted_ledger_balance()")
    op.execute("DROP FUNCTION IF EXISTS guard_ledger_entry_mutation()")
    op.execute("DROP FUNCTION IF EXISTS guard_ledger_transaction_mutation()")
    op.drop_table("agent_float_accounts")
    op.drop_table("top_up_reversals")
    op.drop_index("ix_ledger_entries_account", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_transaction", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("ix_ledger_transactions_top_up", table_name="ledger_transactions")
    op.drop_table("ledger_transactions")
    op.drop_index("ix_provider_events_status_received", table_name="provider_events")
    op.drop_index(op.f("ix_provider_events_top_up_id"), table_name="provider_events")
    op.drop_table("provider_events")
    op.drop_index("ix_top_ups_agent_created", table_name="top_ups")
    op.drop_index("ix_top_ups_status_updated", table_name="top_ups")
    op.drop_index("ix_top_ups_wallet_created", table_name="top_ups")
    op.drop_table("top_ups")
