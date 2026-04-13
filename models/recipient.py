from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Recipient(Base):
    __tablename__ = "recipients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    nickname: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    avatar_color: Mapped[str] = mapped_column(String(20), nullable=False, server_default="#6366f1")
    country_code: Mapped[str] = mapped_column(String(10), nullable=False, server_default="SN")   # ISO 3166-1 alpha-2
    country_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="Senegal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
