"""Unit tests for the age-based ``trigger_events`` retention passes.

Covers ``cleanup_old_webhook_events`` (30-day window over ``created_at``) and
``cleanup_old_trigger_events`` (FAR-167; 90-day window over ``received_at``).
The two passes share one select-then-delete shape, so every test is
parameterised over both jobs rather than duplicated per module — including the
timestamp column each one filters on, which the two copies previously did not
both pin.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.cleanup_jobs.helpers import JOB_CASES, JobCase, make_session


class TestCleanupOldEvents:
    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_deletes_old_events(self, case: JobCase) -> None:
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        session = make_session(ids)

        count = await case.cleanup_fn(session)

        assert count == len(ids)
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_skips_when_no_old_events(self, case: JobCase) -> None:
        session = make_session()

        count = await case.cleanup_fn(session)

        assert count == 0
        session.execute.assert_awaited_once()
        session.commit.assert_not_awaited()

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_filters_on_own_timestamp_column(self, case: JobCase) -> None:
        """SELECT must filter THIS job's retention column below now - retention_days.

        The webhook pass ages events out on ``created_at`` and the trigger-events
        pass on ``received_at``; asserting the column (not just the cutoff)
        catches a pass that silently switched to the other timestamp.
        """
        session = make_session([uuid.uuid4()])
        retention_days = 7

        await case.cleanup_fn(session, retention_days=retention_days)

        stmt = session.execute.call_args_list[0][0][0]
        compiled = str(stmt)
        assert f"trigger_events.{case.filter_column}" in compiled
        assert f"trigger_events.{case.other_timestamp_column}" not in compiled
        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        expected = datetime.now(UTC) - timedelta(days=retention_days)
        assert abs((cutoff - expected).total_seconds()) < 5

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_uses_default_retention_when_unspecified(self, case: JobCase) -> None:
        session = make_session([uuid.uuid4()])

        await case.cleanup_fn(session)

        stmt = session.execute.call_args_list[0][0][0]
        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        expected = datetime.now(UTC) - timedelta(days=case.retention_days)
        assert abs((cutoff - expected).total_seconds()) < 5

    @pytest.mark.parametrize("case", JOB_CASES)
    @pytest.mark.parametrize("retention_days", [0, -1, -365])
    async def test_rejects_invalid_retention_days(self, case: JobCase, retention_days: int) -> None:
        session = make_session()

        with pytest.raises(ValueError, match="retention_days"):
            await case.cleanup_fn(session, retention_days=retention_days)

        session.execute.assert_not_awaited()

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_respects_batch_size_limit(self, case: JobCase) -> None:
        """SELECT must be capped at the job's BATCH_SIZE rows per pass."""
        session = make_session([uuid.uuid4() for _ in range(case.batch_size)])

        await case.cleanup_fn(session)

        stmt = session.execute.call_args_list[0][0][0]
        limit = next(v for v in stmt.compile().params.values() if isinstance(v, int))
        assert limit == case.batch_size

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_delete_targets_returned_ids(self, case: JobCase) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        session = make_session(ids)

        await case.cleanup_fn(session)

        delete_stmt = session.execute.call_args_list[1][0][0]
        assert delete_stmt.table.name == "trigger_events"
        target_ids = next(v for v in delete_stmt.compile().params.values() if isinstance(v, list))
        assert set(target_ids) == set(ids)

    @pytest.mark.parametrize("case", JOB_CASES)
    async def test_commit_failure_propagates(self, case: JobCase) -> None:
        session = make_session([uuid.uuid4()])
        session.commit.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            await case.cleanup_fn(session)
