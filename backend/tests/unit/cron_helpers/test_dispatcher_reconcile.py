"""Unit tests for dispatcher_reconcile (plan F3c) — predicate matrix, partial
eviction, Redis-error fail-safe, re-enqueue gate-on-return, discriminator.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch

ORG = uuid.uuid4()
RUN_PENDING_UNDISPATCHED = uuid.uuid4()
RUN_PENDING_DISPATCHED = uuid.uuid4()
RUN_RUNNING = uuid.uuid4()
RUN_AWAITING = uuid.uuid4()
RUN_WITH_JOB = uuid.uuid4()
RUN_EVICTED = uuid.uuid4()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[tuple[Any, Any]] = []
        self.begin_cm = _MockBegin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return self.begin_cm

    def get_bind(self) -> Any:
        return self._get_bind()

    async def get(self, model: Any, pk: Any) -> SimpleNamespace:
        return SimpleNamespace(max_concurrent_runs=5, status="running")

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        if "set_config" in str(stmt):
            return MagicMock()
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _run_row(
    run_id: uuid.UUID,
    status: str,
    *,
    dispatched: bool = True,
    stale: bool = True,
    nodeless: bool = False,
) -> SimpleNamespace:
    heartbeat = datetime.now(UTC) - timedelta(minutes=30) if stale else datetime.now(UTC)
    return SimpleNamespace(
        id=run_id,
        pipeline_id=uuid.uuid4(),
        status=status,
        dispatched_at=datetime.now(UTC) if dispatched else None,
        heartbeat_at=heartbeat,
        # Non-None by default (has finalised node output → NOT nodeless); a
        # nodeless zombie has never finalised any node.
        node_token_usage=None if nodeless else {},
        outputs_json=None if nodeless else {},
        started_at=datetime.now(UTC) - timedelta(minutes=60) if nodeless else datetime.now(UTC) - timedelta(minutes=1),
    )


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_reenqueue_window": 600,
        "saq_job_heartbeat": 300,
        "saq_claimed_nodeless_minutes": 45,
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
    }
    base.update(overrides)
    return MagicMock(**base)


def _make_queue(redis_client: MagicMock, *, job_result: Any = None) -> MagicMock:
    q = MagicMock()
    q.name = "runs"
    q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
    q.job = AsyncMock(return_value=job_result)
    return q


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


async def _run_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    queue_job_result: Any = None,
    dispatch_result: tuple[str, str | None] = ("enqueued", "new-job-id"),
    capacity_free: bool = True,
    awaiting_committed: bool = True,
) -> dict[str, Any]:
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    q = _make_queue(redis_client, job_result=queue_job_result)
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client

    with (
        patch.object(ch, "_open_factory", return_value=factory),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis", redis_cls),
        patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=dispatch_result) as reenqueue,
        patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        patch.object(
            ch,
            "_awaiting_human_has_committed_decision",
            new_callable=AsyncMock,
            return_value=awaiting_committed,
        ) as awaiting_guard,
    ):
        if capacity_free is False:
            with patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=5):
                summary = await ch.dispatcher_reconcile()
        else:
            with patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0):
                summary = await ch.dispatcher_reconcile()

    return summary, reenqueue, ingest, redis_client, awaiting_guard


class TestReconcilePredicateMatrix:
    @pytest.mark.asyncio
    async def test_pending_undispatched_capacity_free_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, ingest, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)]
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_undispatched_capacity_full_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _ingest, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)], capacity_free=False
        )
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        assert summary["skipped"] == 1

    @pytest.mark.asyncio
    async def test_pending_dispatched_stale_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_DISPATCHED, "pending", dispatched=True, stale=True)]
        )
        assert summary["repaired"] == 1
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_running_stale_redispatch_execute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _, _ = await _run_reconcile(monkeypatch, [_run_row(RUN_RUNNING, "running", stale=True)])
        assert summary["repaired"] == 1
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_awaiting_human_committed_decision_stale_redispatched_as_resume_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F6a gated recovery WITH a committed gate decision: an awaiting_human
        run with a stale heartbeat, NO SAQ job in Redis (a half-resumed run
        whose resume_run job was lost), and a committed HITL decision IS
        re-dispatched as resume_run."""
        summary, reenqueue, ingest, _, awaiting_guard = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "awaiting_human", stale=True)], awaiting_committed=True
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        ingest.assert_not_awaited()
        awaiting_guard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_awaiting_human_stale_no_committed_decision_not_redispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F6a auto-approve guard: an awaiting_human run with a stale heartbeat
        and NO SAQ job but NO committed gate decision (a genuinely-waiting run
        whose finished job hash expired + heartbeat froze) must NOT be
        re-dispatched — resume_run with empty resume_data would inject
        {"_hitl_decision": {}} and auto-approve the gate."""
        summary, reenqueue, ingest, _, awaiting_guard = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "awaiting_human", stale=True)], awaiting_committed=False
        )
        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        awaiting_guard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claimed_stale_no_job_redispatched_as_resume_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F6a gated recovery: a claimed run with a stale heartbeat and NO SAQ
        job in Redis IS re-dispatched as resume_run (a claim was already made —
        the guard does not apply)."""
        summary, reenqueue, _, _, awaiting_guard = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "claimed", stale=True)]
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        awaiting_guard.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_capacity_deferred_redispatched_in_saq_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Capacity-deferred runs (pending, dispatched_at NULL, dispatcher NULL)
        must be reachable and re-dispatched when capacity frees (F3c)."""
        summary, reenqueue, ingest, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)],
            capacity_free=True,
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_job_still_exists_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, ingest, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_WITH_JOB, "running", stale=True)],
            queue_job_result=SimpleNamespace(id="saq:job:runs:run:x"),
        )
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_nodeless_fresh_heartbeat_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A claimed-but-nodeless zombie (running + FRESH heartbeat + zero node
        output after the nodeless window) is terminal-failed, never re-dispatched.
        The fresh heartbeat keeps it invisible to the stale branch — that is the
        primary hang mechanism this branch closes."""
        summary, reenqueue, ingest, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_RUNNING, "running", stale=False, nodeless=True)],
        )
        assert summary["nodeless_failed"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_with_node_output_not_nodeless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run that finalised node output is NOT nodeless — a stale-heartbeat
        one still takes the worker-lost re-dispatch repair, never the fail."""
        summary, reenqueue, _, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_RUNNING, "running", stale=True, nodeless=False)],
        )
        assert summary["repaired"] == 1
        assert summary["nodeless_failed"] == 0
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_nodeless_age_gate_requires_staleness(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Age-gate unit check: a nodeless-but-recently-started run is NOT
        matched (the predicate age gate protects a legitimate long first node)."""
        row = _run_row(RUN_RUNNING, "running", stale=False, nodeless=True)
        row.started_at = datetime.now(UTC) - timedelta(minutes=10)
        assert ch._is_nodeless_zombie_row(row, 45) is False

    @pytest.mark.asyncio
    async def test_nodeless_row_without_started_at_is_not_zombie(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A running row that never recorded a started_at cannot be age-gated —
        it must not be terminalized by the nodeless branch."""
        row = _run_row(RUN_RUNNING, "running", stale=False, nodeless=True)
        row.started_at = None
        assert ch._is_nodeless_zombie_row(row, 45) is False

    @pytest.mark.asyncio
    async def test_nodeless_with_recent_start_falls_through_to_job_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A nodeless run that started recently (age gate not elapsed) is not
        failed by the nodeless branch; with a fresh heartbeat and no other
        branch matching, it is skipped (job exists) rather than failed."""
        row = _run_row(RUN_RUNNING, "running", stale=False, nodeless=True)
        row.started_at = datetime.now(UTC) - timedelta(minutes=10)
        summary, reenqueue, _, _, _ = await _run_reconcile(
            monkeypatch,
            [row],
            queue_job_result=SimpleNamespace(id="saq:job:runs:run:x"),
        )
        assert summary["nodeless_failed"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()


class TestReconcileRedisFailSafe:
    @pytest.mark.asyncio
    async def test_redis_read_error_does_nothing_and_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        q.job = AsyncMock(side_effect=RuntimeError("redis read failed"))
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["redis_errors"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_awaited_once()
        # Fail-safe: NEVER act on an unreadable Redis — no DEL/ZREM/LREM issued.
        redis_client.delete.assert_not_called()
        redis_client.zrem.assert_not_called()
        redis_client.lrem.assert_not_called()


class TestPartialEviction:
    @pytest.mark.asyncio
    async def test_evicted_job_repaired_with_prefix_aware_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker stopped, job hash deleted -> reconcile DEL+ZREM+LREM+enqueue."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)  # queue.job returns None
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(
                ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job")
            ) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 1
        job_key = f"run:{RUN_EVICTED}"
        job_id = f"saq:job:runs:run:{RUN_EVICTED}"
        # All keys derived from the configured queue name (PREFIX-AWARE).
        redis_client.delete.assert_awaited_with(f"saq:abort:{job_key}")
        redis_client.zrem.assert_awaited_with("saq:runs:incomplete", job_id)
        redis_client.lrem.assert_any_await("saq:runs:queued", 0, job_id)
        redis_client.lrem.assert_any_await("saq:runs:active", 0, job_id)
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[0] == "runs"
        assert reenqueue.await_args.args[1] == str(RUN_EVICTED)
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_deduped_after_repair_alerts_no_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enqueue gate-on-return: a still-deduped result must not loop."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("deduped", None)) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 0
        assert summary["deduped"] == 1
        reenqueue.assert_awaited_once()  # gate-on-return: exactly one attempt
        ingest.assert_awaited_once()


class TestCapacityMarkerExclusion:
    """PR review: capacity-blocked runs must NEVER be re-dispatched.

    A run demoted to ``pending`` with ``error_code`` in
    (``org_capacity_limited``, ``pipeline_capacity``) has a LIVE in-process
    retry accelerator (``_retry_pending``). If ``dispatcher_reconcile``
    re-enqueues it, a second worker spawns a SECOND retry loop that can
    double-execute the run. ``_reconcile_capacity_marker_exclusion()`` is the
    WHERE-clause guard. These assertions fail if the exclusion is removed.
    """

    def _sql(self) -> str:
        return str(ch._reconcile_capacity_marker_exclusion().compile(compile_kwargs={"literal_binds": True}))

    def test_null_error_code_not_excluded(self) -> None:
        """error_code IS NULL (no failure) must be allowed through."""
        assert "IS NULL" in self._sql()

    def test_org_capacity_limited_marker_excluded(self) -> None:
        assert "org_capacity_limited" in self._sql()

    def test_pipeline_capacity_marker_excluded(self) -> None:
        assert "pipeline_capacity" in self._sql()

    def test_markers_rendered_in_not_in_clause(self) -> None:
        """Both markers live in a single NOT IN clause — a run carrying either
        marker fails the whole exclusion predicate and is never re-dispatched."""
        sql = self._sql()
        assert "NOT IN ('org_capacity_limited', 'pipeline_capacity')" in sql


class TestReconcilePrefixAware:
    @pytest.mark.asyncio
    async def test_staging_queue_names_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same reconcile against staging-runs must derive staging keys."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "staging-runs"
        q.job_id.side_effect = lambda key: f"saq:job:staging-runs:{key}"
        q.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings(saq_runs_queue="staging-runs")),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "job")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
        ):
            await ch.dispatcher_reconcile()

        redis_client.zrem.assert_awaited_with("saq:staging-runs:incomplete", f"saq:job:staging-runs:run:{RUN_RUNNING}")
        redis_client.lrem.assert_any_await("saq:staging-runs:queued", 0, f"saq:job:staging-runs:run:{RUN_RUNNING}")
        redis_client.lrem.assert_any_await("saq:staging-runs:active", 0, f"saq:job:staging-runs:run:{RUN_RUNNING}")


class TestReEnqueueRun:
    @pytest.mark.asyncio
    async def test_delegates_to_dispatch_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_re_enqueue_run is the single dispatch gating point — it must pass
        through to dispatch_run unchanged (queue + job_type flow through)."""
        _patch_env(monkeypatch)
        with patch(
            "modulo.core.dispatch.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-42"),
        ) as dispatch:
            result = await ch._re_enqueue_run("runs", str(RUN_EVICTED), str(ORG), "execute_run")

        assert result == ("enqueued", "job-42")
        dispatch.assert_awaited_once_with(str(RUN_EVICTED), str(ORG), queue="runs", job_type="execute_run")

    @pytest.mark.asyncio
    async def test_resume_run_job_type_flows_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        with patch(
            "modulo.core.dispatch.dispatch_run",
            new_callable=AsyncMock,
            return_value=("deduped", None),
        ) as dispatch:
            result = await ch._re_enqueue_run("runs", str(RUN_AWAITING), str(ORG), "resume_run")

        assert result == ("deduped", None)
        dispatch.assert_awaited_once_with(str(RUN_AWAITING), str(ORG), queue="runs", job_type="resume_run")


class TestReconcileEmptyOrg:
    @pytest.mark.asyncio
    async def test_no_orgs_records_stats_and_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty org table must still record a fresh last_run_at so
        /healthz/ready sees the cron ticking even in an empty-org environment."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([])])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["scanned"] == 0
        assert summary["repaired"] == 0
        redis_cls.from_url.assert_not_called()
        assert ch.get_dispatcher_reconcile_stats()["last_run_at"] is not None


class TestReconcileFailureBranches:
    @pytest.mark.asyncio
    async def test_read_failure_logs_and_continues_to_next_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A per-org row read failure must not abort the whole reconcile — the
        cron continues scanning the remaining orgs."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG])])
        original_execute = session.execute

        async def _execute_with_failing_read(stmt: Any, params: dict[str, Any] | None = None) -> Any:
            if "FROM runs" in str(stmt):
                raise RuntimeError("read boom")
            return await original_execute(stmt, params)

        session.execute = _execute_with_failing_read
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=MagicMock(name="runs"))),
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["scanned"] == 0
        assert summary["repaired"] == 0
        # The tick still completed (no re-raise) and recorded its stats.
        assert ch.get_dispatcher_reconcile_stats()["last_run_at"] is not None

    @pytest.mark.asyncio
    async def test_claim_cap_exhausted_run_terminalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An SAQ run whose claim_count reached its cap can never complete —
        it must be terminalized, never re-dispatched."""
        _patch_env(monkeypatch)
        row = _run_row(RUN_RUNNING, "running", stale=True)
        row.claim_count = 20  # == settings.saq_run_claim_cap default
        session = _MockSession([_org_result([ORG]), _rows_result([row])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=MagicMock(name="runs"))),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["claim_cap_terminalized"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        # The terminalizer UPDATE was issued.
        assert any("claim_cap_exhausted" in str(stmt) for stmt, _ in session.executed)

    @pytest.mark.asyncio
    async def test_partial_eviction_failure_counts_redis_error_and_alerts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Redis failure during the DEL/ZREM/LREM repair must count as a redis
        error, alert, and NOT re-enqueue (fail-safe on unreadable Redis)."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.delete = AsyncMock(side_effect=RuntimeError("redis down"))
        q = _make_queue(redis_client, job_result=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["redis_errors"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reenqueue_failure_counts_redis_error_and_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dispatch failure during re-enqueue must count as a redis error and
        alert — never silently drop the run."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, side_effect=RuntimeError("dispatch boom")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["redis_errors"] == 1
        assert summary["repaired"] == 0
        ingest.assert_awaited_once()
        assert ingest.await_args.kwargs["function"] == "dispatcher_reconcile"

    @pytest.mark.asyncio
    async def test_cancelled_redis_read_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cancelled Redis job lookup must propagate — never treated as a
        Redis error (which would skip the run on a shutdown signal)."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        q.job = AsyncMock(side_effect=asyncio.CancelledError())
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock),
            pytest.raises(asyncio.CancelledError),
        ):
            await ch.dispatcher_reconcile()
