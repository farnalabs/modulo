import logging
import os
from logging.config import fileConfig
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import sqlalchemy as sa
from alembic import context
from alembic.config import Config
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool

from modulo.db.models import Base

_log = logging.getLogger(__name__)

_DRIVER_MAP: dict[str, str] = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
    "mysql+aiomysql": "mysql+pymysql",
    "mysql+asyncmy": "mysql+pymysql",
    "postgresql": "postgresql+psycopg",
    "postgres": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
}


def _to_sync_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme in _DRIVER_MAP:
        parsed = parsed._replace(scheme=_DRIVER_MAP[parsed.scheme])
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if qs.get("sslmode") == ["disable"]:
        del qs["sslmode"]
    if qs.get("ssl") == ["disable"]:
        del qs["ssl"]
    new_query = urlencode(qs, doseq=True) if qs else ""
    return urlunparse(parsed._replace(query=new_query))


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

    # Allow DATABASE_ADMIN_URL env var to override the alembic.ini connection string.
    # Migrations connect with the owner role (not modulo_app runtime role) to
    # run DDL without RLS interference. Falls back to DATABASE_URL for compat.
    _db_url = os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL")
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

    # Alembic creates alembic_version with VARCHAR(32) by default, but
    # post-squash branch migration IDs exceed 32 chars (e.g.
    # 0006_post_squash_pipeline_archived_at is 44 chars).  Widen the column
    # before any migration runs so the version UPDATE never truncates.
    if backend == "postgresql":
        from sqlalchemy import inspect as sa_inspect

        if sa_inspect(connection).has_table("alembic_version"):
            connection.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))
            _log.info("Widened alembic_version.version_num to VARCHAR(255)")

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
        with engine.begin() as connection:
            do_run_migrations(connection)
    finally:
        engine.dispose()


if config is not None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
