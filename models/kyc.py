from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class KYCSubmission(Base):
    __tablename__ = "kyc_submissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Personal info snapshot at submission time
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    date_of_birth: Mapped[str] = mapped_column(String(20), nullable=False, server_default="")   # stored as YYYY-MM-DD string
    nationality: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    address: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    city: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    country: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")

    # Document
    id_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default="")    # national_id | passport | drivers_license | residence_permit
    id_number: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    id_expiry: Mapped[str] = mapped_column(String(20), nullable=False, server_default="")  # YYYY-MM-DD
    id_front_url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")     # base64 data-URI or storage URL
    id_back_url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    selfie_url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # Review
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")  # pending | under_review | verified | rejected
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)    # admin identifier
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
