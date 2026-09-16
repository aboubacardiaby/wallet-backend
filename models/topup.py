from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


TOP_UP_STATUSES = (
    "Created",
    "Pending",
    "Processing",
    "RequiresAction",
    "Completed",
    "Failed",
    "Expired",
    "Cancelled",
    "Reversed",
    "UnderReview",
)
FUNDING_METHODS = ("card", "bank_transfer", "mobile_money", "agent_cash")


class TopUp(Base):
    __tablename__ = "top_ups"
    __table_args__ = (
        UniqueConstraint("wallet_id", "idempotency_key", name="uq_top_ups_wallet_idempotency"),
        UniqueConstraint(
            "provider_name",
            "provider_transaction_reference",
            name="uq_top_ups_provider_transaction",
        ),
        CheckConstraint("gross_amount > 0", name="gross_amount_positive"),
        CheckConstraint("fee_amount >= 0", name="fee_amount_nonnegative"),
        CheckConstraint("net_amount >= 0", name="net_amount_nonnegative"),
        CheckConstraint("net_amount = gross_amount - fee_amount", name="amounts_reconcile"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("version_id > 0", name="version_positive"),
        CheckConstraint(
            "status IN ('Created','Pending','Processing','RequiresAction','Completed',"
            "'Failed','Expired','Cancelled','Reversed','UnderReview')",
            name="status_valid",
        ),
        CheckConstraint(
            "funding_method IN ('card','bank_transfer','mobile_money','agent_cash')",
            name="funding_method_valid",
        ),
        Index("ix_top_ups_wallet_created", "wallet_id", "created_at"),
        Index("ix_top_ups_status_updated", "status", "updated_at"),
        Index("ix_top_ups_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    internal_reference: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    funding_method: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    provider_transaction_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default="0")
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="Pending")
    failure_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    failure_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmation_code_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    confirmation_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": version_id}


class ProviderEvent(Base):
    __tablename__ = "provider_events"
    __table_args__ = (
        UniqueConstraint("provider_name", "provider_event_id", name="uq_provider_events_identity"),
        CheckConstraint(
            "processing_status IN ('received','processed','ignored','failed')",
            name="processing_status_valid",
        ),
        Index("ix_provider_events_status_received", "processing_status", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    top_up_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("top_ups.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sanitized_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    processing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="received"
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerTransactionRecord(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "(is_posted AND posted_at IS NOT NULL) OR (NOT is_posted AND posted_at IS NULL)",
            name="posted_state_consistent",
        ),
        Index("ix_ledger_transactions_top_up", "top_up_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_reference: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    top_up_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("top_ups.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_posted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerEntryRecord(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("direction IN ('debit','credit')", name="direction_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_ledger_entries_transaction", "ledger_transaction_id"),
        Index("ix_ledger_entries_account", "account_code", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class TopUpReversal(Base):
    __tablename__ = "top_up_reversals"
    __table_args__ = (
        UniqueConstraint("original_top_up_id", name="uq_top_up_reversals_original"),
        UniqueConstraint("reversal_top_up_id", name="uq_top_up_reversals_reversal"),
        CheckConstraint("original_top_up_id <> reversal_top_up_id", name="different_top_ups"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_top_up_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("top_ups.id", ondelete="RESTRICT"), nullable=False
    )
    reversal_top_up_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("top_ups.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class AgentFloatAccount(Base):
    __tablename__ = "agent_float_accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="balance_nonnegative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("version_id > 0", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __mapper_args__ = {"version_id_col": version_id}
