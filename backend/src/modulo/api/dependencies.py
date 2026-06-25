"""Shared FastAPI dependencies and utilities.

NOTE: Module-level globals `_engine` and `_session_factory` are used here
to cache a single engine + session-factory across the process lifetime.
This is thread-safe for async (single event-loop) usage but creates a
singleton that persists across tests — override via `app.dependency_overrides`
if test isolation is needed.
"""

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from modulo.settings import Settings, get_settings


def pg_connection_string(database_url: str) -> str:
    """Strip SQLAlchemy+asyncpg prefix to get a psycopg-compatible URL."""
    return (
        database_url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
        .split("?")[0]
    )


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_or_create_engine(settings: Settings) -> AsyncEngine:
    """Return the process-global engine, creating it if necessary.

    This is the non-Depends version — use it outside FastAPI route handlers
    (e.g. in the MCP sub-app or background tasks) to share the same connection
    pool used by the main API.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_or_create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return the process-global session factory, creating it if necessary."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    return _session_factory


def _get_engine(settings: Settings = Depends(get_settings)) -> AsyncEngine:
    return get_or_create_engine(settings)


def _get_session_factory(
    engine: AsyncEngine = Depends(_get_engine),
) -> async_sessionmaker[AsyncSession]:
    return get_or_create_session_factory(engine)


async def get_db_session(
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession. Transaction management is left to the caller."""
    async with factory() as session:
        yield session
