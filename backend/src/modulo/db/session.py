"""Async engine + session factory — the single engine factory for a process.

Before PR D (dist/runtime-ops) every DB consumer built its OWN engine: the SAQ
worker (``saq_worker._get_async_engine``, per-worker pool budget), the system
worker's crons (``cron_helpers._get_engine``, no pool knobs), and the web
process (``api.dependencies.get_or_create_engine``) each had an independent
pool — up to three pools per process, each duplicating (or omitting) the
Fly/HAProxy compat knobs. ``get_shared_engine`` is the ONE factory: it bakes in
``pool_pre_ping``, ``statement_cache_size=0`` (the asyncpg prepared-statement
cache is incompatible with HAProxy), and pool sizing from settings, and accepts
pool overrides (the SAQ worker keeps its per-worker budget via
``saq_worker_db_pool_size``). Whichever consumer creates it first fixes the
pool size for the process; later callers share the same engine.
"""

import logging
import threading
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.rls import register_rls_reset_hook, register_tenant_filter
from modulo.settings import get_settings

__all__ = [
    "AsyncSessionLocal",
    "engine",
    "get_shared_engine",
]

_log = logging.getLogger(__name__)

_shared_engine: AsyncEngine | None = None
_shared_engine_lock = threading.Lock()

_GLOBAL_HOOKS_LOCK = threading.Lock()
_GLOBAL_HOOKS_REGISTERED = False


def _register_global_hooks_once() -> None:
    """Register process-global ORM/DB listeners exactly once.

    ``register_append_only_guard`` and ``register_tenant_filter`` attach
    class/session-level listeners that are engine-agnostic — registering them
    for every engine would double-fire the append-only blockers or double-inject
    tenant WHERE clauses on non-Postgres backends. The engine-scoped RLS reset
    hook is engine-specific and stays in :func:`_build_engine`.
    """
    global _GLOBAL_HOOKS_REGISTERED
    if _GLOBAL_HOOKS_REGISTERED:
        return
    with _GLOBAL_HOOKS_LOCK:
        if _GLOBAL_HOOKS_REGISTERED:
            return
        register_append_only_guard()
        register_tenant_filter()
        _GLOBAL_HOOKS_REGISTERED = True


def _build_engine(
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """Build and configure an async engine from settings.

    Extracted into a function so tests can replace it without patching
    module-level state. Fly/HAProxy-compatible knobs are applied for Postgres
    (``pool_pre_ping``, ``statement_cache_size=0``); pool sizing defaults to
    20/10 unless overridden (the SAQ worker passes its per-worker budget).
    """
    settings = get_settings()
    db_type = settings.modulo_db.lower()

    kw: dict[str, Any] = {"url": settings.database_url, "pool_pre_ping": True}
    if db_type == "postgres":
        kw["connect_args"] = {
            "timeout": 10,
            "ssl": False,
            # HAProxy compat: disable the asyncpg prepared-statement cache —
            # the proxy does not reliably support extended-protocol reuse.
            "statement_cache_size": 0,
        }

    if db_type != "sqlite":
        kw["pool_size"] = pool_size if pool_size is not None else 20
        kw["max_overflow"] = max_overflow if max_overflow is not None else 10
        kw["pool_recycle"] = 3600
        kw["pool_timeout"] = 30

    engine = create_async_engine(**kw)

    if db_type == "postgres":
        register_rls_reset_hook(engine)
    else:
        _log.info("Skipping pool-level RLS reset hook — %s backend", db_type)

    _register_global_hooks_once()
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


def get_shared_engine(*, pool_size: int | None = None, max_overflow: int | None = None) -> AsyncEngine:
    """Return the process-global shared async engine, creating it on first use.

    Single engine per process shared by the SAQ worker (runs + system), the
    system worker's crons (cron_helpers), and the per-item fire jobs. The web
    process keeps ``api.dependencies.get_or_create_engine``; both factories
    build via :func:`_build_engine` so the Fly/HAProxy knobs are identical.
    The first caller fixes the pool size for the process (a later caller's
    override is ignored — the engine is already built).
    """
    global _shared_engine
    if _shared_engine is None:
        with _shared_engine_lock:
            if _shared_engine is None:
                _shared_engine = _build_engine(pool_size=pool_size, max_overflow=max_overflow)
    return _shared_engine
