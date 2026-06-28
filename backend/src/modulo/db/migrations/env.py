import asyncio
import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from modulo.db.models import Base

_log = logging.getLogger(__name__)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow DATABASE_URL env var to override the alembic.ini connection string.
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    # Fly.io Postgres attaches with postgres://...?sslmode=disable.
    # SQLAlchemy async drivers need postgresql+asyncpg:// and ssl=disable.
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    _db_url = _db_url.replace("?sslmode=disable", "?ssl=0")
    config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = Base.metadata


def _detect_backend(url: str) -> str:
    """Return backend name from URL scheme."""
    if url.startswith("postgresql"):
        return "postgresql"
    if url.startswith("mysql"):
        return "mysql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    backend = _detect_backend(url)
    _log.info("Running migrations offline for %s backend", backend)

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite does not support ALTER with ALTER TABLE ADD COLUMN in the
        # same way as Postgres; Alembic handles this with batch mode.
        render_as_batch=backend == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    backend = _detect_backend(str(connection.engine.url))
    _log.info("Running migrations for %s backend", backend)

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=backend == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.run_until_complete(run_async_migrations())
    except RuntimeError:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
