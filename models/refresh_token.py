import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from models.base import Base


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @staticmethod
    def create(user_id: uuid.UUID, ttl_days: int = 30) -> tuple[str, "RefreshToken"]:
        plain = secrets.token_urlsafe(64)
        return plain, RefreshToken(
            user_id=user_id,
            token_hash=_token_hash(plain),
            expires_at=datetime.utcnow() + timedelta(days=ttl_days),
        )
