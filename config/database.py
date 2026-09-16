import os
import ssl
from typing import AsyncGenerator
from urllib.parse import quote, urlparse, urlunparse
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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
    engine_kwargs = {}

    if _is_supabase(url):
        ssl_ctx = ssl.create_default_context()
        supabase_ca = os.path.join(os.path.dirname(__file__), "certs", "supabase-root-ca.pem")
        if os.path.exists(supabase_ca):
            ssl_ctx.load_verify_locations(cafile=supabase_ca)
        connect_args["ssl"] = ssl_ctx
        if "pooler.supabase.com" in url:
            # Supabase's pooler runs pgbouncer in transaction mode: backend sessions are
            # shared across clients, so SQLAlchemy's asyncpg dialect must not reuse cached
            # prepared-statement names (they can collide with another client's on the same
            # backend session). See SQLAlchemy asyncpg dialect docs, "Prepared Statement
            # Name with PGBouncer".
            connect_args["statement_cache_size"] = 0
            connect_args["prepared_statement_cache_size"] = 0
            connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"
            engine_kwargs["poolclass"] = NullPool

    _engine = create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", 3600)),
        connect_args=connect_args,
        **(
            engine_kwargs
            if "poolclass" in engine_kwargs
            else {
                "pool_size": int(os.getenv("DB_POOL_SIZE", 20)),
                "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", 10)),
                "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", 30)),
            }
        ),
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
        import models.app_settings  # noqa: F401
        import models.audit_log      # noqa: F401
        import models.bank           # noqa: F401
        import models.rate_limit     # noqa: F401
        import models.rate_override  # noqa: F401
        import models.refresh_token  # noqa: F401
        import models.fee_rule       # noqa: F401
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"WARNING: Could not auto-create tables: {e}")


async def disconnect_db():
    global _engine
    if _engine:
        await _engine.dispose()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    if _SessionLocal is None:
        raise RuntimeError("Database session factory is unavailable")
    async with _SessionLocal() as session:
        yield session


async def database_ready() -> bool:
    if _engine is None:
        return False
    try:
        async with _engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        return True
    except Exception:
        return False
