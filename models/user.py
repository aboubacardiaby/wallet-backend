from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    country_code: Mapped[str] = mapped_column(String(10), server_default="")
    full_name: Mapped[str] = mapped_column(String(255), server_default="")
    email: Mapped[str] = mapped_column(String(255), server_default="")
    pin: Mapped[str] = mapped_column(String(255), server_default="")
    pin_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    is_locked: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    kyc_status: Mapped[str] = mapped_column(String(50), server_default="pending")
    profile_photo: Mapped[str] = mapped_column(String(500), server_default="")
    national_id_type: Mapped[str] = mapped_column(String(50), server_default="")
    national_id_number: Mapped[str] = mapped_column(String(100), server_default="")
    date_of_birth: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Address (flattened)
    street: Mapped[str] = mapped_column(String(255), server_default="")
    city: Mapped[str] = mapped_column(String(100), server_default="")
    region: Mapped[str] = mapped_column(String(100), server_default="")
    country: Mapped[str] = mapped_column(String(100), server_default="")
    postal_code: Mapped[str] = mapped_column(String(20), server_default="")
    user_type: Mapped[str] = mapped_column(String(20), server_default="receiver")   # sender | receiver
    home_currency: Mapped[str] = mapped_column(String(10), server_default="XOF")
    preferred_lang: Mapped[str] = mapped_column(String(10), server_default="fr")
    biometric_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    device_tokens: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class OTP(Base):
    __tablename__ = "otps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
