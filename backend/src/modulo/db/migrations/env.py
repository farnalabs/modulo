import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from modulo.db.models import Base

_log = logging.getLogger(__name__)

target_metadata = Base.metadata

# Module-level Alembic setup — only safe when context is properly configured
# (i.e. when env.py is executed via command.upgrade, not imported as a module).
config = context.config  # type: ignore[has-type]
if config is not None:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

    # Allow DATABASE_URL env var to override the alembic.ini connection string.
    _db_url = os.environ.get("DATABASE_URL")
    if _db_url:
        # Convert any async driver prefix to a sync driver prefix.
        # Alembic env.py runs in a sync context (thread pool or CLI),
        # so a sync engine avoids all event-loop conflicts.
        if _db_url.startswith("postgresql+asyncpg://"):
            _db_url = _db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        elif _db_url.startswith("mysql+asyncmy://"):
            _db_url = _db_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
        elif _db_url.startswith("postgresql://"):
            _db_url = _db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        elif _db_url.startswith("postgres://"):
            _db_url = _db_url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif _db_url.startswith("mysql://"):
            _db_url = _db_url.replace("mysql://", "mysql+pymysql://", 1)
        _db_url = _db_url.replace("?sslmode=disable", "")
        _db_url = _db_url.replace("&sslmode=disable", "")
        config.set_main_option("sqlalchemy.url", _db_url)


def _detect_backend(url: str) -> str:
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
        render_as_batch=backend == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    backend = _detect_backend(str(connection.engine.url))
    _log.info("Running migrations for %s backend", backend)

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=backend == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations via a sync engine — no event loop needed.

    env.py runs in a thread pool (asyncio.to_thread), so a sync engine
    avoids all event-loop conflicts with the main async context.
    """
    url = config.get_main_option("sqlalchemy.url")
    backend = _detect_backend(url)

    sync_url = url
    if "+async" in sync_url:
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        elif sync_url.startswith("mysql+asyncmy://"):
            sync_url = sync_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)

    engine = create_engine(sync_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            do_run_migrations(connection)
    finally:
        engine.dispose()


if config is not None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
