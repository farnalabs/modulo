import logging
import os
from logging.config import fileConfig

from alembic import context
from alembic.config import Config
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool

from modulo.db.models import Base

_log = logging.getLogger(__name__)


def _to_sync_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    elif url.startswith("mysql+asyncmy://"):
        url = url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("mysql://"):
        url = url.replace("mysql://", "mysql+pymysql://", 1)
    url = url.replace("?sslmode=disable", "").replace("&sslmode=disable", "")
    url = url.replace("?ssl=disable", "").replace("&ssl=disable", "")
    return url

target_metadata = Base.metadata

# Module-level Alembic setup — only safe when context is properly configured
# (i.e. when env.py is executed via command.upgrade, not imported as a module).
try:
    config: Config | None = context.config
except AttributeError:
    config = None
if config is not None:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

    # Allow DATABASE_URL env var to override the alembic.ini connection string.
    _db_url = os.environ.get("DATABASE_URL")
    if _db_url:
        config.set_main_option("sqlalchemy.url", _to_sync_url(_db_url))


def _detect_backend(url: str) -> str:
    if url.startswith("postgresql"):
        return "postgresql"
    if url.startswith("mysql"):
        return "mysql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"


def run_migrations_offline() -> None:
    assert config is not None
    url = config.get_main_option("sqlalchemy.url") or ""
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


def do_run_migrations(connection: Connection) -> None:
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
    assert config is not None
    url = config.get_main_option("sqlalchemy.url") or ""
    sync_url = _to_sync_url(url)

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
