"""Regression coverage for mixed-suite async-engine cache isolation."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration import conftest as integration_conftest


class _PollutedSQLiteEngine:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1
        await self.engine.dispose()


@pytest.mark.asyncio
async def test_integration_boundary_clears_polluted_sqlite_engine_caches() -> None:
    """A SQLite engine left by an earlier suite cannot survive the boundary."""
    from modulo.api import dependencies as api_dependencies
    from modulo.cli import break_glass
    from modulo.core import cron_helpers, saq_worker
    from modulo.core.error_tracking import saq_hooks
    from modulo.core.reports import scheduler
    from modulo.db import session as db_session
    from modulo.db.crud import pipeline_snapshot

    polluted_engine = _PollutedSQLiteEngine(create_async_engine("sqlite+aiosqlite:///:memory:"))

    single_engine_caches = (
        (db_session, "_shared_engine"),
        (api_dependencies, "_engine"),
        (api_dependencies, "_SYSTEM_ASYNC_ENGINE"),
        (cron_helpers, "_ENGINE"),
        (cron_helpers, "_SYSTEM_ENGINE"),
        (saq_hooks, "_ENGINE"),
        (saq_worker, "_ASYNC_ENGINE"),
        (saq_worker, "_SYSTEM_ASYNC_ENGINE"),
        (scheduler, "_ENGINE"),
        (scheduler, "_TEST_ENGINE"),
        (break_glass, "_bg_engine"),
    )
    for module, attr in single_engine_caches:
        setattr(module, attr, polluted_engine)
    pipeline_snapshot._SNAPSHOT_LOCK_ENGINES["sqlite-cache"] = polluted_engine

    api_dependencies._session_factory = object()
    api_dependencies._SYSTEM_SESSION_FACTORY = object()
    break_glass._bg_engine_url = "sqlite+aiosqlite:///:memory:"
    break_glass._bg_factory = object()
    break_glass._bg_factory_url = "sqlite+aiosqlite:///:memory:"

    await integration_conftest.dispose_process_global_engines()

    assert polluted_engine.dispose_calls == 1
    for module, attr in single_engine_caches:
        assert getattr(module, attr) is None
    assert not pipeline_snapshot._SNAPSHOT_LOCK_ENGINES
    assert api_dependencies._session_factory is None
    assert api_dependencies._SYSTEM_SESSION_FACTORY is None
    assert break_glass._bg_engine_url is None
    assert break_glass._bg_factory is None
    assert break_glass._bg_factory_url is None
