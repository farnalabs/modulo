import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.rls import register_rls_reset_hook
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_settings = get_settings()

_kw: dict[str, Any] = {"url": _settings.database_url}
if _settings.modulo_db.lower() != "sqlite":
    _kw["pool_pre_ping"] = True
    _kw["pool_size"] = 10
    _kw["max_overflow"] = 5

engine = create_async_engine(**_kw)

if _settings.modulo_db.lower() != "sqlite":
    register_rls_reset_hook(engine)
    register_append_only_guard()
else:
    _log.info("Skipping RLS reset hook and connection pool — SQLite mode")

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
        except SQLAlchemyError:
            await session.rollback()
            raise
