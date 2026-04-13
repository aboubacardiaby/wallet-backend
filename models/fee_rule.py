from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class FeeRule(Base):
    """
    Fee rule for transfers. Rules are matched in descending priority order.
    The first matching active rule wins; falls back to the global default (1.5 %).

    Matching logic:
      - from_currency = None  → matches any sender currency
      - to_currency   = None  → matches any recipient currency
      - min_amount / max_amount → restrict rule to an amount range (in sender currency)
    """
    __tablename__ = "fee_rules"

    id:            Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name:          Mapped[str]       = mapped_column(String(100), nullable=False)
    # Currency filters — None means "any"
    from_currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)
    to_currency:   Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)
    # Fee components (fee = fee_rate * amount + fee_flat, clamped to [min_fee, max_fee])
    fee_rate:      Mapped[float] = mapped_column(Numeric(8, 6), server_default="0.015")   # 1.5 %
    fee_flat:      Mapped[float] = mapped_column(Numeric(18, 2), server_default="0")       # flat addition
    min_fee:       Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)  # floor
    max_fee:       Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)  # cap
    # Amount range filter (in sender currency)
    min_amount:    Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)
    max_amount:    Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)
    # Higher priority = checked first
    priority:      Mapped[int]  = mapped_column(default=0)
    is_active:     Mapped[bool] = mapped_column(Boolean, server_default="true")
    note:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
