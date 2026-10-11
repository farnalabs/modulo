"""FAR-1649 qa iteration 1: a node-STARTED run is never re-dispatched.

The durable ``first_node_dispatched`` phase (FAR-1422) proves a node STARTED
even though no super-step ever completed — so re-dispatching such a row (the
``execute_run`` job re-executes the started node) can duplicate side effects.
Before this fix, once the nodeless selection legs excluded the class it fell
through to the generic stale-heartbeat re-dispatch branch and was re-dispatched
uncapped (the old nodeless branch capped the flood at
``NODELESS_REDISPATCH_MAX_PER_TICK``; the general branch has no cap). These
tests drive a ``first_node_dispatched`` row with a stale heartbeat / no SAQ job
through the REAL reconcile tick (``dispatcher_reconcile``) and assert it is
NOT re-dispatched — plus the control (NULL phase) that proves the harness
discriminates, and the mass-wedge bound (60 such rows enqueue ZERO jobs).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED

ORG = uuid.uuid4()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Minimal session double for one ``dispatcher_reconcile`` tick.

    Mirrors the shapes ``test_dispatcher_reconcile`` handles: the RLS
    ``set_config`` probe, the raw-SQL terminalizer statements (zero rows by
    default), the capacity pipeline lookup, then the org result / row result.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.terminalizer_rows: dict[str, list[uuid.UUID]] = {}
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

    async def flush(self) -> None:
        return

    async def get(self, model: Any, pk: Any) -> SimpleNamespace:
        return SimpleNamespace(
            max_concurrent_runs=5,
            status="running",
            pipeline_id=uuid.uuid4(),
            trigger_id=uuid.uuid4(),
            claim_count=1,
            started_at=datetime.now(UTC) - timedelta(minutes=60),
            dispatched_at=datetime.now(UTC) - timedelta(minutes=65),
        )

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        s = str(stmt)
        if "set_config" in s:
            return MagicMock()
        if s.startswith("SELECT id FROM runs"):
            ids = self.terminalizer_rows.get("aged_nodeless", [])
            r = MagicMock()
            r.all.return_value = [(uid,) for uid in ids]
            r.rowcount = len(ids)
            return r
        if "UPDATE runs SET" in s:
            ids = self.terminalizer_rows.get("dispatch_failed", [])
            if "node_attempt_count" in s:
                ids = self.terminalizer_rows.get("no_progress", [])
            elif "claim_cap_exhausted" in s:
                ids = self.terminalizer_rows.get("claim_cap_exhausted", [])
            elif "created_at < now()" in s:
                ids = self.terminalizer_rows.get("hitl_review_missing", [])
            elif "hitl_claims" in s:
                ids = self.terminalizer_rows.get("hitl_review_expired", [])
            r = MagicMock()
            r.all.return_value = [(uid,) for uid in ids]
            r.rowcount = len(ids)
            return r
        if s.startswith("SELECT pipelines."):
            r = MagicMock()
            r.scalar_one_or_none.return_value = SimpleNamespace(max_concurrent_runs=5, status="running")
            return r
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
    *,
    status: str = "running",
    stale: bool = True,
    dispatch_phase: str | None = None,
    claim_count: int = 1,
) -> SimpleNamespace:
    """A reconcile-scan row (the REAL scan selects ``dispatch_phase``).

    ``node_token_usage`` is non-NULL and ``outputs_absent`` False by default:
    the row has finalised node output, so the nodeless repair never claims it
    and the ONLY branch that can act is the stale-heartbeat re-dispatch path —
    exactly the leg under test.
    """
    heartbeat = datetime.now(UTC) - timedelta(minutes=30) if stale else datetime.now(UTC)
    return SimpleNamespace(
        id=run_id,
        pipeline_id=uuid.uuid4(),
        trigger_id=None,
        status=status,
        dispatched_at=datetime.now(UTC) - timedelta(minutes=35),
        heartbeat_at=heartbeat,
        node_token_usage={},
        outputs_absent=False,
        checkpoints_absent=False,
        started_at=datetime.now(UTC) - timedelta(minutes=60),
        error_code=None,
        dispatcher="saq",
        enqueue_failed_at=None,
        claim_count=claim_count,
        retry_policy=None,
        dispatch_phase=dispatch_phase,
    )


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_reenqueue_window": 600,
        "saq_job_heartbeat": 300,
        "saq_claimed_nodeless_minutes": 35,
        "saq_nodeless_redispatch_budget": 4,
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "saq_run_claim_cap": 20,
        "hitl_review_cancel_grace_seconds": 3600,
        "modulo_telemetry_enabled": False,
        "dispatcher_reconcile_budget_seconds": 95,
        "dispatcher_reconcile_terminalize_max_per_tick": 25,
        "dispatcher_reconcile_facts_max_per_tick": 25,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


async def _run_reconcile(
    monkeypatch: Any,
    rows: list[Any],
    *,
    dispatch_result: tuple[str, str | None] = ("enqueued", "new-job-id"),
) -> tuple[dict[str, Any], AsyncMock, _MockSession]:
    """Drive one real ``dispatcher_reconcile`` tick against a mocked env."""
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    q = MagicMock()
    q.name = "runs"
    q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
    q.job = AsyncMock(return_value=None)
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
        stack.enter_context(patch.object(ch, "get_settings", return_value=_settings()))
        stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
        stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
        reenqueue = stack.enter_context(
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=dispatch_result)
        )
        stack.enter_context(patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock))
        stack.enter_context(
            patch.object(
                ch,
                "_awaiting_human_has_committed_decision",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        stack.enter_context(patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock))
        stack.enter_context(
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0)
        )
        summary = await ch.dispatcher_reconcile()
    return summary, reenqueue, session


class TestNodeStartedRowIsNeverRedispatched:
    """The reconcile tick must hold back a node-STARTED row (qa finding 1)."""

    async def test_node_started_stale_row_is_not_redispatched(self, monkeypatch: Any) -> None:
        """A running + stale-heartbeat + no-SAQ-job row whose phase proves a
        node STARTED is never re-dispatched (execute_run would re-execute the
        started node). It is counted ``node_started_deferred`` and left for
        the watchdogs / age gate — no enqueue, no terminal-fail, no zero-node
        mislabel."""
        run_id = uuid.uuid4()
        row = _run_row(run_id, dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["nodeless_redispatched"] == 0
        assert summary["nodeless_failed"] == 0
        assert summary["claimed_but_never_dispatched"] == 0
        assert summary["node_started_deferred"] == 1

    async def test_control_null_phase_stale_row_is_redispatched(self, monkeypatch: Any) -> None:
        """The control: the SAME row with no durable phase (no node-start
        evidence) still takes the generic stale-heartbeat re-dispatch — the
        guard discriminates on the phase, not on the row shape."""
        row = _run_row(uuid.uuid4(), dispatch_phase=None)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        assert summary["repaired"] == 1
        assert summary["node_started_deferred"] == 0

    async def test_mass_node_started_burst_enqueues_zero(self, monkeypatch: Any) -> None:
        """Finding 2 (lost fleet cap): a mass worker wedge leaves a burst of
        node-STARTED stale rows eligible in the SAME tick. The class is now
        out of EVERY re-dispatch leg, so the burst enqueues ZERO jobs — the
        flood bound the old nodeless cap (50/tick) provided holds a fortiori
        (0 <= 50), and every held-back row is counted."""
        burst = 60  # above the former NODELESS_REDISPATCH_MAX_PER_TICK=50
        rows = [_run_row(uuid.uuid4(), dispatch_phase=PHASE_FIRST_NODE_DISPATCHED) for _ in range(burst)]
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, rows)

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["node_started_deferred"] == burst

    def test_stale_heartbeat_predicate_excludes_node_started_phase(self) -> None:
        """The SQL leg itself (the selection half of finding 1): the generic
        stale-heartbeat branch binds the shared phase constant, so a
        node-STARTED row is never even selected (and the D8 marker sweep, which
        composes this same predicate, judges it unrecoverable in lockstep)."""
        import sqlalchemy as sa

        from modulo.db.models.run import Run

        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        compiled = sa.select(Run.id).where(predicate).compile()
        sql = str(compiled)
        assert "dispatch_phase" in sql
        assert PHASE_FIRST_NODE_DISPATCHED in dict(compiled.params).values()


class TestNodelessChokepointTruthfulForNodeStartedRow:
    """Finding 4: the chokepoint's three observability legs describe the row."""

    async def test_counter_log_and_event_are_truthful(self, caplog: Any) -> None:
        """A node-started row that (by race) reaches the chokepoint must never
        bump ``claimed_but_never_dispatched`` or emit the "no node was ever
        dispatched" log/event — it gets its own counter and message."""
        import logging

        from modulo.core.cron_helpers import _fail_nodeless_run

        run = SimpleNamespace(
            status="running",
            pipeline_id=uuid.uuid4(),
            trigger_id=uuid.uuid4(),
            claim_count=3,
            started_at=datetime.now(UTC) - timedelta(minutes=40),
            dispatched_at=datetime.now(UTC) - timedelta(minutes=45),
            dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
            dispatch_phase_entered_at=datetime.now(UTC) - timedelta(minutes=30),
        )

        class _SessionWithRun:
            async def get(self, model: Any, pk: Any) -> Any:
                return run

        summary = ch._dispatcher_summary()
        ingest = AsyncMock()
        with caplog.at_level(logging.ERROR, logger=ch.__name__), patch.object(ch, "_ingest_saq_error", ingest):
            await _fail_nodeless_run(_SessionWithRun(), uuid.uuid4(), uuid.uuid4(), summary)

        assert summary["node_started_terminalized"] == 1
        assert summary["claimed_but_never_dispatched"] == 0
        messages = [record.getMessage() for record in caplog.records]
        assert any("node_started_terminalized" in message for message in messages)
        assert any("first node STARTED" in message for message in messages)
        assert not any("no node was ever dispatched" in message for message in messages)
        event_message = ingest.await_args.kwargs["message"]
        assert "node-started" in event_message
        assert "no node was ever dispatched" not in event_message
