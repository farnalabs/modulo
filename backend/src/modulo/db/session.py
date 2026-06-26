import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.rls import register_rls_reset_hook, register_tenant_filter
from modulo.settings import get_settings

_log = logging.getLogger(__name__)


def _build_engine() -> Any:
    """Build and configure the async engine from settings.

    Extracted into a function so tests can replace it without patching
    module-level state.  Called once at module load.
    """
    settings = get_settings()
    db_type = settings.modulo_db.lower()

    kw: dict[str, Any] = {"url": settings.database_url}
    if db_type != "sqlite":
        kw["pool_pre_ping"] = True
        kw["pool_size"] = 10
        kw["max_overflow"] = 5

    engine = create_async_engine(**kw)

    if db_type == "postgres":
        register_rls_reset_hook(engine)
        register_append_only_guard()
    else:
        _log.info("Skipping pool-level RLS reset hook — %s backend", db_type)

    register_tenant_filter()
    return engine


engine = _build_engine()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
