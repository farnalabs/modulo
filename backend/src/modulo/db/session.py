import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.rls import register_rls_reset_hook, register_tenant_filter
from modulo.settings import get_settings

__all__ = [
    "AsyncSessionLocal",
    "engine",
]

_log = logging.getLogger(__name__)


def _build_engine() -> AsyncEngine:
    """Build and configure the async engine from settings.

    Extracted into a function so tests can replace it without patching
    module-level state.  Called once at module load.
    """
    settings = get_settings()
    db_type = settings.modulo_db.lower()

    kw: dict[str, Any] = {"url": settings.database_url}
    if db_type != "sqlite":
        kw["pool_pre_ping"] = True
        kw["pool_size"] = 20
        kw["max_overflow"] = 10
        kw["pool_recycle"] = 3600
        kw["pool_timeout"] = 30

    if db_type == "postgres":
        kw["connect_args"] = {
            "timeout": 10,
            "ssl": False,
        }

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
    autobegin=False,
)
