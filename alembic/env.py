import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import create_engine, pool
from alembic import context

load_dotenv()

# Import all models so Alembic can detect them
from models import Base  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Return a synchronous psycopg2 URL for Alembic migrations."""
    from urllib.parse import quote, urlparse, urlunparse
    url = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/kalipeh")

    # URL-encode special characters in the password
    parsed = urlparse(url)
    if parsed.password:
        encoded_pw = quote(parsed.password, safe="")
        netloc = f"{parsed.username}:{encoded_pw}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        url = urlunparse(parsed._replace(netloc=netloc))

    # Replace asyncpg driver with psycopg2 for sync migrations
    url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = get_url()
    connect_args = {}
    if "supabase.co" in url or "supabase.com" in url:
        connect_args["sslmode"] = "require"

    engine = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
