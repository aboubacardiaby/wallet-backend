import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from models.base import Base


class RateLimit(Base):
    __tablename__ = "rate_limits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ip_address = Column(String(64), unique=True, nullable=False, index=True)
    count = Column(Integer, default=1, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    window_start = Column(DateTime, default=datetime.utcnow, nullable=False)
