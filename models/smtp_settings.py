from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class SmtpConfig(Base):
    __tablename__ = "smtp_settings"

    # Singleton row — always id=1
    id:           Mapped[int]  = mapped_column(Integer, primary_key=True, default=1)
    host:         Mapped[str]  = mapped_column(String(200), server_default="")
    port:         Mapped[int]  = mapped_column(Integer,     server_default="587")
    username:     Mapped[str]  = mapped_column(String(200), server_default="")
    password:     Mapped[str]  = mapped_column(String(500), server_default="")
    from_email:   Mapped[str]  = mapped_column(String(200), server_default="")
    from_name:    Mapped[str]  = mapped_column(String(100), server_default="Kalipeh")
    use_tls:      Mapped[bool] = mapped_column(Boolean, server_default="true")
    use_ssl:      Mapped[bool] = mapped_column(Boolean, server_default="false")
    enabled:      Mapped[bool] = mapped_column(Boolean, server_default="false")
    updated_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
