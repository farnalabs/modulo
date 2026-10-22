"""Unit tests for the SAQ cron wrappers around the trigger_events cleanup jobs.

``webhook_dedup_cleanup`` and ``trigger_events_cleanup`` in
``modulo.core.saq_worker`` each wrap one cleanup pass in a drain loop and
report the total deleted count, and both must pick the session factory by
dialect: on PostgreSQL the purge is cross-org by design and must drain on the
SYSTEM session factory (``_cleanup_session_factory`` →
``_make_system_session_factory``, FAR-523) because the plain ``modulo_app``
factory is NOBYPASSRLS and silently matched zero rows; on non-PostgreSQL
backends (no RLS) the plain factory is correct.

The two wrappers differ only in the cleanup callable they drive, so the
contract is pinned once per behaviour and parameterised over both jobs.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import modulo.core.saq_worker as sw
from modulo.db.models.trigger_event import TriggerEvent
from tests.unit.cleanup_jobs.helpers import (
    JOB_CASES,
    JobCase,
    make_factory_with_session,
    make_trigger_event,
    patch_settings,
)


async def _select_all(factory) -> list[TriggerEvent]:
    async with factory() as session, session.begin():
        return list((await session.execute(select(TriggerEvent))).scalars().all())


class TestCleanupCronDrainLoop:
    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_drains_multiple_batches(self, case: JobCase) -> None:
        """Keep deleting until a pass returns fewer than BATCH_SIZE rows."""
        factory, _ = make_factory_with_session()

        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=factory) as mock_factory,
            patch(
                case.patch_target,
                new_callable=AsyncMock,
                side_effect=[case.batch_size, case.batch_size, 3],
            ) as mock_cleanup,
        ):
            result = await case.cron_fn({})

        mock_factory.assert_called()
        assert result == {"deleted": case.batch_size * 2 + 3}
        assert mock_cleanup.await_count == 3

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_single_pass_when_below_threshold(self, case: JobCase) -> None:
        """A full-threshold-minus-one pass must stop the loop after one batch."""
        factory, _ = make_factory_with_session()

        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=factory) as mock_factory,
            patch(case.patch_target, new_callable=AsyncMock, return_value=case.batch_size - 1) as mock_cleanup,
        ):
            result = await case.cron_fn({})

        mock_factory.assert_called()
        assert result == {"deleted": case.batch_size - 1}
        mock_cleanup.assert_awaited_once()

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_propagates_cleanup_error(self, case: JobCase) -> None:
        factory, _ = make_factory_with_session()

        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=factory) as mock_factory,
            patch(case.patch_target, new_callable=AsyncMock, side_effect=RuntimeError("db down")),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await case.cron_fn({})

        mock_factory.assert_called()

    @pytest.mark.parametrize("case", JOB_CASES)
    @pytest.mark.parametrize("non_pg_db", ["sqlite", "mariadb", "mysql"])
    async def test_non_postgres_uses_plain_factory(self, case: JobCase, non_pg_db: str) -> None:
        """On non-PostgreSQL backends (no RLS, no modulo_system role) the job
        must drain on the PLAIN factory and never touch the system engine."""
        factory, _ = make_factory_with_session()

        with (
            patch_settings(non_pg_db),
            patch.object(sw, "_make_session_factory", return_value=factory) as mock_plain,
            patch.object(sw, "_make_system_session_factory") as mock_system,
            patch(case.patch_target, new_callable=AsyncMock, return_value=0),
        ):
            result = await case.cron_fn({})

        mock_plain.assert_called()
        mock_system.assert_not_called()
        assert result == {"deleted": 0}


class TestCleanupCronAutobeginTransaction:
    """Regression: the system session factory is ``autobegin=False`` (F1
    convention), so every cron MUST open an explicit per-batch transaction.

    Without it the first ``session.execute`` inside the cleanup pass raises
    ``InvalidRequestError: Autobegin is disabled on this Session`` and the
    hourly retention cron fails on every tick. These tests drive the REAL
    autobegin=False session factory and the REAL cleanup pass against an
    in-memory SQLite schema — the mocked drain-loop tests above cannot see the
    transaction handling at all.
    """

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_cron_deletes_old_rows_against_autobegin_false_session(
        self, case: JobCase, autobegin_false_factory
    ) -> None:
        async with autobegin_false_factory() as session, session.begin():
            session.add(make_trigger_event(case.seed_age_days))

        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory) as mock_factory,
        ):
            result = await case.cron_fn({})

        mock_factory.assert_called()
        assert result == {"deleted": 1}
        assert not await _select_all(autobegin_false_factory)

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_keeps_recent_rows(self, case: JobCase, autobegin_false_factory) -> None:
        """Events inside the retention window must NOT be purged."""
        async with autobegin_false_factory() as session, session.begin():
            session.add(make_trigger_event(age_days=0))

        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory) as mock_factory,
        ):
            result = await case.cron_fn({})

        mock_factory.assert_called()
        assert result == {"deleted": 0}
        assert len(await _select_all(autobegin_false_factory)) == 1

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_cron_zero_batch_against_autobegin_false_session(
        self, case: JobCase, autobegin_false_factory
    ) -> None:
        """Even an empty table must not raise: the SELECT needs an active
        transaction on an autobegin=False session."""
        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory) as mock_factory,
        ):
            result = await case.cron_fn({})

        mock_factory.assert_called()
        assert result == {"deleted": 0}

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_cron_reopens_transaction_for_each_batch(self, case: JobCase, autobegin_false_factory) -> None:
        """The drain loop must open a transaction PER BATCH, not one around
        the loop.

        The cleanup pass commits at the end of every batch, so on an
        autobegin=False session the transaction is closed again between
        passes: batching more than BATCH_SIZE rows requires a fresh
        ``session.begin()`` per pass. A single transaction wrapped around the
        loop leaves the second pass with no active transaction and raises
        ``InvalidRequestError: Can't operate on closed transaction inside
        context manager`` on the real cron.
        """
        async with autobegin_false_factory() as session, session.begin():
            session.add_all([make_trigger_event(case.seed_age_days) for _ in range(3)])

        # BATCH_SIZE is re-imported by the cron (and read by the cleanup pass)
        # on every call, so patching the module attribute shrinks the batch to
        # 2 and forces 2+ passes without seeding BATCH_SIZE rows.
        with (
            patch_settings(),
            patch.object(sw, "_make_system_session_factory", return_value=autobegin_false_factory),
            patch.object(case.module, "BATCH_SIZE", 2),
        ):
            result = await case.cron_fn({})

        assert result == {"deleted": 3}
        assert not await _select_all(autobegin_false_factory)
