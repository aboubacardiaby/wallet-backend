from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # "card" | "bank_transfer" | "paypal" | "apple_pay" | "google_pay"
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")

    # Card fields
    card_brand: Mapped[str] = mapped_column(String(20), nullable=False, server_default="")   # visa | mastercard | amex | discover
    last4: Mapped[str] = mapped_column(String(4), nullable=False, server_default="")
    expiry_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expiry_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    holder_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")

    # Bank transfer fields
    bank_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False, server_default="")
    routing_number: Mapped[str] = mapped_column(String(20), nullable=False, server_default="")
    account_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="")

    # PayPal
    email: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")

    # Status
    is_default: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_verified: Mapped[bool] = mapped_column(Boolean, server_default="true")   # simulated: always verified

    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
