from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class AchConfig(Base):
    __tablename__ = "ach_config"

    # Singleton row — always id=1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    api_base_url: Mapped[str] = mapped_column(String(500), server_default="http://localhost:3000/v1")
    api_key: Mapped[str] = mapped_column(String(500), server_default="")

    # Platform's own bank account (the other side of every transfer)
    platform_account_number: Mapped[str] = mapped_column(String(50),  server_default="")
    platform_routing_number: Mapped[str] = mapped_column(String(9),   server_default="")
    platform_account_type:   Mapped[str] = mapped_column(String(20),  server_default="CHECKING")
    platform_account_name:   Mapped[str] = mapped_column(String(100), server_default="Kalipeh Platform")

    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
