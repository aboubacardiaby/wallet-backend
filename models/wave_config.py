from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class WaveConfig(Base):
    __tablename__ = "wave_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    api_base_url: Mapped[str] = mapped_column(String(500), server_default="https://api.wave.com")
    api_key: Mapped[str] = mapped_column(String(500), server_default="")
    business_country: Mapped[str] = mapped_column(String(100), server_default="Senegal")
    business_currency: Mapped[str] = mapped_column(String(10), server_default="XOF")
    aggregated_merchant_id: Mapped[str] = mapped_column(String(100), server_default="")
    verify_recipient: Mapped[bool] = mapped_column(Boolean, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
