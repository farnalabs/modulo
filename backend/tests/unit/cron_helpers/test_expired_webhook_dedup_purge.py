"""Unit tests for the FAR-661 expired webhook dedup-hash purge.

Covers the three layers the purge spans:

* ``purge_expired_dedup_hashes`` (``modulo.core.cleanup_jobs.webhook_dedup_cleanup``)
  — one batched pass that delegates to ``TriggerEngine.cleanup_expired_dedup_hashes``
  (reusing its Postgres advisory lock, key=20250601) and commits.
* the ``limit`` batching extension on the engine method itself.
* the ``expired_webhook_dedup_purge`` SAQ system cron in
  ``modulo.core.saq_worker`` — on PostgreSQL it must drain on the SYSTEM
  session factory (cross-org purge: the org-scoped RLS policy on
  ``webhook_dedup_hashes`` makes a plain-factory session silently match zero
  rows), on other backends the plain factory. The system factory is
  ``autobegin=False``, so the cron must open an explicit per-batch
  transaction.

The end-to-end tests run the real purge against in-memory SQLite (no RLS
there — the RLS trap itself is pinned by the factory-selection tests) and
assert the housekeeping scanner's ``expired_webhook_dedups`` candidate count
drops to zero once the purge runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import modulo.core.saq_worker as sw
from modulo.core.cleanup_jobs.webhook_dedup_cleanup import purge_expired_dedup_hashes
from modulo.core.housekeeping import _scan_expired_webhook_dedups
from modulo.core.trigger_engine import TriggerEngine
from modulo.db.models.base import Base
from modulo.db.models.webhook import WebhookDedupHash

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()


def _dedup_hash(
    *,
    org_id: uuid.UUID,
    payload_hash: str,
    expired: bool,
) -> WebhookDedupHash:
    """Build a dedup hash row; ``expired=True`` stamps expires_at an hour ago."""
    now = datetime.now(UTC)
    return WebhookDedupHash(
        id=uuid.uuid4(),
        organisation_id=org_id,
        trigger_id=uuid.uuid4(),
        payload_hash=payload_hash,
        expires_at=now - timedelta(hours=1) if expired else now + timedelta(hours=1),
    )


def _sqlite_session(ids: list[uuid.UUID]) -> AsyncMock:
    """Mock async session bound to the sqlite dialect (advisory lock skipped).

    The first ``execute`` returns the SELECT result wrapping *ids*; when *ids*
    is non-empty a second ``execute`` returns the DELETE result.
    """
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = ids
    if ids:
        delete_result = MagicMock()
        delete_result.rowcount = len(ids)
        session.execute = AsyncMock(side_effect=[select_result, delete_result])
    else:
        session.execute = AsyncMock(return_value=select_result)
    session.commit = AsyncMock()
    return session


class TestPurgeExpiredDedupHashes:
    async def test_delegates_to_engine_with_limit_and_commits(self) -> None:
        """The purge reuses the engine method (advisory lock) with the batch
        cap and commits the pass."""
        session = AsyncMock()
        session.commit = AsyncMock()

        with (
            patch.object(TriggerEngine, "cleanup_expired_dedup_hashes", new_callable=AsyncMock) as mock_engine,
        ):
            mock_engine.return_value = 7
            deleted = await purge_expired_dedup_hashes(session, batch_size=250)

        assert deleted == 7
        mock_engine.assert_awaited_once_with(session, limit=250)
        session.commit.assert_awaited_once()

    async def test_no_commit_when_nothing_expired(self) -> None:
        """An empty batch (or a contended advisory lock — the engine returns 0
        in both cases) must not commit and must report 0."""
        session = AsyncMock()
        session.commit = AsyncMock()

        with patch.object(TriggerEngine, "cleanup_expired_dedup_hashes", new_callable=AsyncMock) as mock_engine:
            mock_engine.return_value = 0
            deleted = await purge_expired_dedup_hashes(session)

        assert deleted == 0
        session.commit.assert_not_awaited()

    async def test_commit_failure_propagates(self) -> None:
        session = AsyncMock()
        session.commit = AsyncMock(side_effect=RuntimeError("db down"))

        with patch.object(TriggerEngine, "cleanup_expired_dedup_hashes", new_callable=AsyncMock) as mock_engine:
            mock_engine.return_value = 3
            with pytest.raises(RuntimeError, match="db down"):
                await purge_expired_dedup_hashes(session)

    @pytest.mark.parametrize("batch_size", [0, -1])
    async def test_rejects_invalid_batch_size(self, batch_size: int) -> None:
        session = AsyncMock()
        session.execute = AsyncMock()

        with pytest.raises(ValueError, match="batch_size"):
            await purge_expired_dedup_hashes(session, batch_size=batch_size)

        session.execute.assert_not_awaited()


class TestEngineLimitBatching:
    """The ``limit`` extension on ``TriggerEngine.cleanup_expired_dedup_hashes``."""

    def _sqlite_session(self) -> AsyncMock:
        session = AsyncMock()
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        session.get_bind = MagicMock(return_value=bind)
        return session

    async def _capture_select(self, session: AsyncMock, ids: list[uuid.UUID], **kwargs: int | None) -> object:
        """Run the cleanup with *kwargs* and return the SELECT statement."""
        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = ids
        session.execute = AsyncMock(return_value=select_result)

        await TriggerEngine.cleanup_expired_dedup_hashes(session, **kwargs)

        return session.execute.call_args_list[0][0][0]

    async def test_limit_caps_select(self) -> None:
        session = self._sqlite_session()

        stmt = await self._capture_select(session, [uuid.uuid4()], limit=250)

        int_params = [v for v in stmt.compile().params.values() if isinstance(v, int)]
        assert int_params == [250]

    async def test_default_none_has_no_limit(self) -> None:
        """The default (manual API route behaviour) must stay unbatched."""
        session = self._sqlite_session()

        stmt = await self._capture_select(session, [uuid.uuid4()])

        int_params = [v for v in stmt.compile().params.values() if isinstance(v, int)]
        assert not int_params

    async def test_expiry_filter(self) -> None:
        """SELECT must filter ``expires_at`` at (or before) now."""
        session = self._sqlite_session()

        stmt = await self._capture_select(session, [uuid.uuid4()])

        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        assert abs((cutoff - datetime.now(UTC)).total_seconds()) < 5

    async def test_delete_targets_returned_ids(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        session = self._sqlite_session()
        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = ids
        delete_result = MagicMock()
        delete_result.rowcount = len(ids)
        session.execute = AsyncMock(side_effect=[select_result, delete_result])

        count = await TriggerEngine.cleanup_expired_dedup_hashes(session, limit=10)

        assert count == 2
        delete_stmt = session.execute.call_args_list[1][0][0]
        assert delete_stmt.table.name == "webhook_dedup_hashes"
        target_ids = next(v for v in delete_stmt.compile().params.values() if isinstance(v, list))
        assert set(target_ids) == set(ids)

    @pytest.mark.parametrize("limit", [0, -5])
    async def test_rejects_non_positive_limit(self, limit: int) -> None:
        session = self._sqlite_session()
        session.execute = AsyncMock()

        with pytest.raises(ValueError, match="limit"):
            await TriggerEngine.cleanup_expired_dedup_hashes(session, limit=limit)

        session.execute.assert_not_awaited()


def _make_factory_with_session() -> tuple[MagicMock, MagicMock]:
    """Return a mock sessionmaker (via ``async with``) plus its session.

    ``session`` is a plain MagicMock so ``session.begin()`` returns the
    context-manager mock synchronously — the cron opens an explicit per-batch
    transaction (an AsyncMock's ``begin()`` would return a bare coroutine).
    """
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    factory = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    factory.return_value = context
    return factory, session


class TestExpiredWebhookDedupPurgeCron:
    """SAQ wiring for ``expired_webhook_dedup_purge``."""

    async def test_drains_multiple_batches_on_system_factory(self) -> None:
        """PostgreSQL: keep purging until a pass returns fewer than the batch
        size — on the SYSTEM session factory (RLS-safe cross-org purge)."""
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=factory) as mock_system,
            patch.object(sw, "_make_session_factory") as mock_plain,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.purge_expired_dedup_hashes",
                new_callable=AsyncMock,
                side_effect=[1000, 1000, 3],
            ) as mock_purge,
        ):
            result = await sw.expired_webhook_dedup_purge({})

        mock_system.assert_called()
        mock_plain.assert_not_called()
        assert result == {"deleted": 2003}
        assert mock_purge.await_count == 3

    async def test_single_pass_when_below_threshold(self) -> None:
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.purge_expired_dedup_hashes",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_purge,
        ):
            result = await sw.expired_webhook_dedup_purge({})

        assert result == {"deleted": 0}
        mock_purge.assert_awaited_once()

    async def test_propagates_purge_error(self) -> None:
        """A DB failure inside the drain loop must propagate to the caller."""
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.purge_expired_dedup_hashes",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await sw.expired_webhook_dedup_purge({})

    @pytest.mark.parametrize("non_pg_db", ["sqlite", "mariadb", "mysql"])
    async def test_non_postgres_uses_plain_factory(self, non_pg_db: str) -> None:
        """On non-PostgreSQL backends (no RLS, no modulo_system role) the job
        must drain on the PLAIN factory and never touch the system engine."""
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db=non_pg_db)),
            patch.object(sw, "_make_session_factory", return_value=factory) as mock_plain,
            patch.object(sw, "_make_system_session_factory") as mock_system,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.purge_expired_dedup_hashes",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            result = await sw.expired_webhook_dedup_purge({})

        mock_plain.assert_called()
        mock_system.assert_not_called()
        assert result == {"deleted": 0}


class TestExpiredWebhookDedupPurgeEndToEnd:
    """The real cron + real purge against an in-memory SQLite schema.

    Exercises the ``autobegin=False`` factory (the cron must open an explicit
    per-batch transaction), the batching drain, the expiry filter, the
    cross-org reach, and the housekeeping interplay: the
    ``expired_webhook_dedups`` scanner count must drop to zero once the purge
    runs.
    """

    @pytest.fixture
    async def autobegin_false_factory(self) -> AsyncGenerator[async_sessionmaker, None]:
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[WebhookDedupHash.__table__])
            )
        factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        try:
            yield factory
        finally:
            await engine.dispose()

    async def _seed(self, factory: async_sessionmaker, rows: list[WebhookDedupHash]) -> None:
        async with factory() as session, session.begin():
            session.add_all(rows)

    async def _remaining(self, factory: async_sessionmaker) -> tuple[int, int]:
        """Return (total rows, expired rows) still in the table."""
        async with factory() as session, session.begin():
            total = (await session.execute(select(func.count()).select_from(WebhookDedupHash))).scalar_one()
            expired = (
                await session.execute(
                    select(func.count())
                    .select_from(WebhookDedupHash)
                    .where(WebhookDedupHash.expires_at <= datetime.now(UTC))
                )
            ).scalar_one()
        return total, expired

    async def test_cron_purges_expired_across_orgs(self, autobegin_false_factory: async_sessionmaker) -> None:
        await self._seed(
            autobegin_false_factory,
            [
                _dedup_hash(org_id=_ORG_A, payload_hash="a" * 64, expired=True),
                _dedup_hash(org_id=_ORG_B, payload_hash="b" * 64, expired=True),
                _dedup_hash(org_id=_ORG_A, payload_hash="c" * 64, expired=False),
            ],
        )

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory),
        ):
            result = await sw.expired_webhook_dedup_purge({})

        assert result == {"deleted": 2}
        total, expired = await self._remaining(autobegin_false_factory)
        assert (total, expired) == (1, 0)

    async def test_cron_drains_multiple_real_batches(self, autobegin_false_factory: async_sessionmaker) -> None:
        """1001 expired rows against the real 1000-row default cap drain in
        two passes (the second selecting the remainder)."""
        await self._seed(
            autobegin_false_factory,
            [_dedup_hash(org_id=_ORG_A, payload_hash=f"{i}" * 64, expired=True) for i in range(1001)],
        )

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory),
        ):
            result = await sw.expired_webhook_dedup_purge({})

        assert result == {"deleted": 1001}
        total, expired = await self._remaining(autobegin_false_factory)
        assert (total, expired) == (0, 0)

    async def test_scanner_reports_zero_after_purge(self, autobegin_false_factory: async_sessionmaker) -> None:
        """The housekeeping ``expired_webhook_dedups`` scanner must report no
        candidates once the purge has run."""
        await self._seed(
            autobegin_false_factory,
            [
                _dedup_hash(org_id=_ORG_A, payload_hash="a" * 64, expired=True),
                _dedup_hash(org_id=_ORG_A, payload_hash="b" * 64, expired=True),
                _dedup_hash(org_id=_ORG_A, payload_hash="c" * 64, expired=False),
            ],
        )

        async with autobegin_false_factory() as session, session.begin():
            before = await _scan_expired_webhook_dedups(session, _ORG_A)

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory),
        ):
            await sw.expired_webhook_dedup_purge({})

        async with autobegin_false_factory() as session, session.begin():
            after = await _scan_expired_webhook_dedups(session, _ORG_A)

        assert len(before) == 2
        assert not after
