from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class RateOverride(Base):
    """
    Admin-set manual exchange rate for a currency pair.
    When active, this rate replaces the live market rate everywhere in the app.
    """
    __tablename__ = "rate_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_currency: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    to_currency:   Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # Override rate: 1 from_currency = <rate> to_currency
    rate:          Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    # Optional spread markup applied on top of the override rate (e.g. 0.005 = 0.5%)
    spread_pct:    Mapped[float] = mapped_column(Float, server_default="0")
    is_active:     Mapped[bool]  = mapped_column(Boolean, server_default="true")
    note:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
