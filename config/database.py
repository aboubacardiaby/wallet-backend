import os
import ssl
from typing import AsyncGenerator
from urllib.parse import quote, urlparse, urlunparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine = None
_SessionLocal = None


def _get_url() -> str:
    url = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/kalipeh")
    parsed = urlparse(url)
    if parsed.password:
        encoded_pw = quote(parsed.password, safe="")
        netloc = f"{parsed.username}:{encoded_pw}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        url = urlunparse(parsed._replace(netloc=netloc))
    return url


def _is_supabase(url: str) -> bool:
    return "supabase.co" in url or "supabase.com" in url


async def connect_db():
    global _engine, _SessionLocal

    url = _get_url()
    connect_args = {}

    if _is_supabase(url):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_ctx

    _engine = create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    _SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)

    try:
        async with _engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        print("Connected to PostgreSQL!")
    except Exception as e:
        print(f"WARNING: Could not connect to PostgreSQL: {e}")
        print("App will start, but database operations will fail until PostgreSQL is available.")
        return

    # Auto-create any missing tables (safe — does not drop or alter existing ones)
    try:
        from models.base import Base
        import models.bank          # noqa: F401
        import models.rate_override # noqa: F401
        import models.fee_rule      # noqa: F401
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"WARNING: Could not auto-create tables: {e}")


async def disconnect_db():
    global _engine
    if _engine:
        await _engine.dispose()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _SessionLocal() as session:
        yield session
