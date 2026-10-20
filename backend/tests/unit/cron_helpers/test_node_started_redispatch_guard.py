"""FAR-1649 qa iterations 1+2+3: a node-STARTED run is never re-EXECUTED
UNLESS the graph is already vetted safe to re-run — and is never stranded.

The durable ``first_node_dispatched`` phase (FAR-1422) proves a node STARTED
even though no super-step ever completed. Iteration 1 excluded the class from
the nodeless branches and the generic stale-heartbeat re-dispatch branch (the
2026-10-10 production storm). Iteration 2 ALSO excluded it from the
``pending`` re-enqueue leg and widened the row belt to ``pending`` — and
REGRESSED: the fenced pending-reset row (``dispatcher``/``dispatched_at``/
phase all kept) then became invisible to every recovery path and was
permanently stranded ``pending`` when its SAQ retry job was lost. Iteration 3
(the finding under this change) RESTORES the pending leg for that row shape,
because its only producer is idempotency-gated at all three reset call sites.

This file covers:

* the RUNNING legs still exclude the class (nodeless branches, generic
  stale-heartbeat, row belt) — the storm does not come back;
* the ``pending`` re-dispatch leg DELIBERATELY ADMITS the fenced-reset row
  (idempotency-gated producer, durable backstop — never stranded) and the row
  belt is RUNNING-only + fail-closed;
* the F6a gated-recovery leg, which deliberately ADMITS the class but only as
  ``resume_run`` (checkpoint resume, never re-execution);
* ``_fenced_pending_reset`` being reachable only through an idempotency gate,
  so a NON-idempotent node-started run can never become a reset row;
* the executor write site binding the shared ``PHASE_FIRST_NODE_DISPATCHED``
  constant instead of a bare literal;
* the slot-sweep UPDATE carrying the node-started exclusion on the RETRY arm
  only (the over-budget terminal-fail arm still releases the slot).

These tests drive a ``first_node_dispatched`` row through the REAL reconcile
tick (``dispatcher_reconcile``) and assert the correct job-type outcome — plus
the controls (NULL phase) that prove the harness discriminates.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
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

    async def get(self, model: Any, pk: Any, with_for_update: bool = False) -> SimpleNamespace:
        # ``with_for_update`` is accepted for the FAR-1914 router's locked re-read.
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
            if "stranded_age_minutes" in s:
                # FAR-1862 stranded-pending backstop: empty by default.
                ids = self.terminalizer_rows.get("stranded_pending", [])
            elif "node_attempt_count" in s:
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
        started node). Since FAR-1803 it is counted
        ``node_started_executor_lost`` (the observed dead-executor class —
        stale heartbeat IS the executor-lost evidence) and left for the age
        gate — no enqueue, no terminal-fail, no zero-node mislabel. Before
        FAR-1803 it landed on the belt's ``node_started_deferred`` (a counter
        documented as "production should see 0")."""
        run_id = uuid.uuid4()
        row = _run_row(run_id, dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["nodeless_redispatched"] == 0
        assert summary["nodeless_failed"] == 0
        assert summary["claimed_but_never_dispatched"] == 0
        assert summary["node_started_executor_lost"] == 1
        assert summary["node_started_deferred"] == 0

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
        node-STARTED stale rows eligible in the SAME tick. The class is out of
        every ``execute_run`` re-dispatch leg (the F6a leg matches only
        awaiting_human/claimed/hitl_parked, never ``running``), so the burst
        enqueues ZERO jobs — the flood bound the old nodeless cap (50/tick)
        provided holds a fortiori (0 <= 50), and every held-back row is
        counted ``node_started_executor_lost`` (FAR-1803; pre-fix it was the
        belt's ``node_started_deferred``)."""
        burst = 60  # above the former NODELESS_REDISPATCH_MAX_PER_TICK=50
        rows = [_run_row(uuid.uuid4(), dispatch_phase=PHASE_FIRST_NODE_DISPATCHED) for _ in range(burst)]
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, rows)

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["node_started_executor_lost"] == burst
        assert summary["node_started_deferred"] == 0

    def test_stale_heartbeat_predicate_excludes_node_started_phase(self) -> None:
        """The SQL leg itself (the selection half of finding 1): the generic
        stale-heartbeat branch binds the shared phase constant, so a
        node-STARTED row is never re-dispatched through THAT leg. FAR-1803
        added a separate leg that admits the same shape for COUNTING only
        (``node_started_executor_lost`` — held back, never re-dispatched), so
        the D8 marker sweep now judges such rows recoverable — their marker is
        kept until the age gate terminalises them at the bound, which is
        strictly earlier than D8's 25h staleness sweep anyway."""
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
            async def get(self, model: Any, pk: Any, with_for_update: bool = False) -> Any:
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


class TestPendingLegRedispatchesTheFencedResetRow:
    """FAR-1649 qa iteration 3 (REGRESSION of iteration 2's finding 1).

    ``executor._fenced_pending_reset`` resets a running row to ``pending``
    WITHOUT clearing ``dispatcher``/``dispatched_at``/``dispatch_phase``.
    Iteration 2 excluded that shape from the ``pending`` re-enqueue leg and
    widened the row belt to ``pending`` — which made the row invisible to
    EVERY recovery path (the stale sweep's never-dispatched arm requires
    ``dispatcher != 'saq'`` + ``claim_count = 0`` + ``dispatched_at IS NULL``;
    its capacity arms need a capacity ``error_code`` the reset NULLs; its age
    gates require ``status='running'``; ``resume_run``'s claim SQL accepts
    only awaiting_human/claimed/hitl_parked). If the SAQ retry job was lost
    between the reset and the re-claim, the run was permanently stranded
    ``pending``: never re-executed (good) but never resumed or terminalised
    either (bad).

    Iteration 3 RESTORES the pending leg and narrows the belt back to
    ``running``. Safe because the row's ONLY producer is idempotency-gated at
    all three reset call sites (see ``TestFencedResetIsIdempotencyGated``),
    so re-dispatching it re-executes a graph already vetted safe to re-run
    (FAR-295) — the FAR-1649 guarantee is "never re-EXECUTE a node-started
    NON-idempotent run", and a non-idempotent run can never reach this shape.
    """

    def test_pending_leg_carries_no_phase_exclusion(self) -> None:
        """The SQL pending leg has NO phase leg (selection half of the
        no-strand fix): exactly ONE phase-exclusion leg remains in the whole
        predicate — the RUNNING stale-heartbeat leg, whose re-dispatch is NOT
        idempotency-gated and is the shape behind the 2026-10-10 storm."""
        import sqlalchemy as sa

        from modulo.db.models.run import Run

        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        compiled = sa.select(Run.id).where(predicate).compile()
        sql = str(compiled)
        assert sql.count("dispatch_phase IS NULL") == 1
        phase_params = [v for v in dict(compiled.params).values() if v == PHASE_FIRST_NODE_DISPATCHED]
        # FAR-1728 bound the phase constant TWICE — once by the single
        # EXCLUSION leg above (the running stale-heartbeat leg, pinned at
        # exactly one "IS NULL") and once by the guardrail-override resume
        # leg's POSITIVE match (``dispatch_phase = ...`` + the flip's
        # dedicated ``guardrail_override_resume_pending`` marker column —
        # NOT ``is_replay``, which webhook/manual replay also set).
        # FAR-1803 adds the THIRD binding: its own POSITIVE match (the
        # node-started / executor-lost leg — counted, never re-dispatched).
        # A fourth binding would mean yet another leg started binding the
        # phase — inspect before relaxing this.
        assert len(phase_params) == 3

    async def test_pending_node_started_reset_row_is_redispatched(self, monkeypatch: Any) -> None:
        """The REGRESSION test: a pending + dispatcher='saq' + old-
        dispatched_at row whose phase proves a node STARTED — the fenced
        pending-reset signature — IS re-dispatched as ``execute_run`` when no
        SAQ job exists. Without this leg the row is PERMANENTLY stranded
        (no other recovery path reaches a pending+saq row); with it, the lost
        SAQ retry job is recovered on the next reconcile tick."""
        run_id = uuid.uuid4()
        row = _run_row(run_id, status="pending", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        assert summary["repaired"] == 1
        # The belt is RUNNING-only now — it must NOT hold a pending row back.
        assert summary["node_started_deferred"] == 0

    async def test_control_null_phase_pending_row_is_redispatched(self, monkeypatch: Any) -> None:
        """The control: the SAME pending row with no durable phase still takes
        the reenqueue-window re-dispatch as ``execute_run`` — recovery of the
        pending class does not discriminate on the phase."""
        row = _run_row(uuid.uuid4(), status="pending", dispatch_phase=None)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        assert summary["repaired"] == 1
        assert summary["node_started_deferred"] == 0

    async def test_belt_fails_closed_on_missing_phase_attribute(self, monkeypatch: Any) -> None:
        """FAR-1649 qa iteration 3: the row belt fails CLOSED on a row source
        that does not expose ``dispatch_phase`` at all — exactly like
        ``_is_nodeless_zombie_row`` does on the same input. Missing evidence
        is never "safe to re-execute" evidence, so a RUNNING row without the
        column is held back (counted deferred, never enqueued), not
        fail-opened through to an ``execute_run`` re-dispatch."""
        row = _run_row(uuid.uuid4(), status="running", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        delattr(row, "dispatch_phase")
        assert not hasattr(row, "dispatch_phase")

        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["node_started_deferred"] == 1

    async def test_belt_holds_back_running_node_started_row(self, monkeypatch: Any) -> None:
        """The belt's surviving scope (RUNNING only): a running node-started
        row that reaches the re-dispatch path is still held back — the storm
        does not come back. Since FAR-1803 a STALE-heartbeat row of this shape
        is classified earlier (``node_started_executor_lost``), so the belt's
        own scope is the FRESH-heartbeat row: a live attempt that reached the
        loop by a selection race (never re-dispatched, never counted as
        executor-lost)."""
        row = _run_row(uuid.uuid4(), status="running", stale=False, dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["node_started_deferred"] == 1
        assert summary["node_started_executor_lost"] == 0


class TestFencedResetIsIdempotencyGated:
    """The safety premise the restored pending leg rests on.

    A ``pending`` node-started row can only be produced by
    ``executor._fenced_pending_reset``, and every production call site of
    that method is gated on ``_graph_is_idempotent`` — so the row the
    restored pending leg re-dispatches always carries a graph already vetted
    safe to re-run (FAR-295). If a future call site drops its gate, a
    NON-idempotent node-started run becomes re-dispatchable and these tests
    must fail first.
    """

    def test_every_fenced_reset_call_site_is_behind_an_idempotency_gate(self) -> None:
        """Pin the three production call sites AND their gates.

        ``_fenced_pending_reset`` is awaited exactly three times: twice in
        the executor (the in-execute transient requeue behind
        ``_can_fenced_requeue``; the retry_policy re-dispatch behind
        ``_retry_policy_applies``) and once in ``watchdog_retry`` behind its
        ``not graph_idempotent`` stand-down. A fourth, ungated call site — or
        a removed gate — breaks the count / snippet assertions, and the
        functional watchdog test below breaks when its own gate stops
        working.
        """
        from modulo.core.pipeline_engine import executor, watchdog_retry

        exec_src = Path(executor.__file__).read_text(encoding="utf-8")
        wr_src = Path(watchdog_retry.__file__).read_text(encoding="utf-8")

        assert exec_src.count("await self._fenced_pending_reset(") == 2
        assert wr_src.count("await executor._fenced_pending_reset(") == 1
        # Gate 1 — in-execute transient requeue (the call sits inside this if).
        assert "if _can_fenced_requeue(" in exec_src
        # Gate 2 — the retry_policy re-dispatch path.
        assert "if not _retry_policy_applies(" in exec_src
        # Gate 3 — the watchdog hook's stand-down.
        assert "if is_correction_run or not graph_idempotent:" in wr_src


class TestF6aGatedRecoveryResumesNodeStartedNeverReexecutes:
    """FAR-1649 qa iteration 2 (finding 2): the F6a leg may ADMIT a
    node-STARTED row, but only as ``resume_run``.

    The phase never advances past ``first_node_dispatched``, so a run parked
    at a HITL gate routinely carries that phase — a phase exclusion here would
    strand every gated run. What makes it safe is the job type: F6a statuses
    re-dispatch as ``resume_run`` (checkpoint resume, no re-execution) and
    only after the committed-decision guard. These tests pin that contract:
    a first_node_dispatched F6a row is enqueued as ``resume_run``, never
    ``execute_run``.
    """

    async def test_node_started_awaiting_human_row_is_resumed_not_reexecuted(self, monkeypatch: Any) -> None:
        """A first_node_dispatched awaiting_human row with a committed gate
        decision is re-dispatched as ``resume_run`` — resumed from the
        checkpoint, never re-executed as ``execute_run``."""
        run_id = uuid.uuid4()
        row = _run_row(run_id, status="awaiting_human", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        assert summary["repaired"] == 1
        # The belt is a re-EXECUTION guard: it must not hold back a resume.
        assert summary["node_started_deferred"] == 0

    async def test_node_started_hitl_parked_row_is_resumed_not_reexecuted(self, monkeypatch: Any) -> None:
        """Same contract for ``hitl_parked`` (qa F1 park recovery)."""
        run_id = uuid.uuid4()
        row = _run_row(run_id, status="hitl_parked", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        assert summary["node_started_deferred"] == 0

    def test_f6a_leg_carries_no_phase_exclusion(self) -> None:
        """The F6a leg deliberately has NO phase exclusion — pin the design so
        a future \"consistency\" edit that adds one (and strands every gated
        run) fails here first."""
        import sqlalchemy as sa

        from modulo.db.models.run import Run

        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        compiled = sa.select(Run.id).where(predicate).compile()
        sql = str(compiled)
        # Exactly ONE phase-exclusion leg (the running stale-heartbeat leg):
        # the F6a leg must not add a second one (it would strand every gated
        # run), and the pending leg deliberately has none either (it would
        # strand every fenced-reset row — iteration 2's regression).
        assert sql.count("dispatch_phase IS NULL") == 1
        # The F6a statuses are bound as one list param (awaiting_human among
        # them), proving the leg is present while carrying no second phase
        # exclusion.
        bound_groups = [v for v in dict(compiled.params).values() if isinstance(v, list)]
        assert any("awaiting_human" in group for group in bound_groups)


class TestExecutorWriteSiteSharesPhaseConstant:
    """FAR-1649 qa iteration 2 (finding 3): one definition, no bare literal."""

    def test_executor_imports_and_uses_the_shared_constant(self) -> None:
        """The executor write site binds ``PHASE_FIRST_NODE_DISPATCHED`` from
        the dependency-free ``dispatch_phases`` leaf — the same object the
        predicates bind — and no bare ``"first_node_dispatched"`` literal
        remains in the executor module."""
        from modulo.core.pipeline_engine import executor

        assert executor.PHASE_FIRST_NODE_DISPATCHED is PHASE_FIRST_NODE_DISPATCHED
        source = Path(executor.__file__).read_text(encoding="utf-8")
        assert 'enter_phase("first_node_dispatched")' not in source
        assert "enter_phase(PHASE_FIRST_NODE_DISPATCHED)" in source


class TestSlotSweepExcludesNodeStartedFromRetryOnly:
    """FAR-1649 qa iteration 2 (finding 5): the slot sweep's UPDATE excludes
    a node-STARTED row from the RETRY-RESET arm only. The over-budget
    terminal-fail arm still collects it (terminal-fail re-executes nothing)
    so the pipeline slot is released promptly instead of ~105 min later via
    the age gate.
    """

    async def test_slot_sweep_sql_excludes_node_started_from_retry_arm_only(self) -> None:
        """The emitted UPDATE carries the claim_count OR-arm: over-budget
        rows bypass the phase exclusion entirely."""
        import modulo.core.run_admission as ra

        statements: list[str] = []

        class _Result:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def all(self) -> list[Any]:
                return self._rows

        class _Conn:
            def get_bind(self) -> Any:
                return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
                statements.append(str(stmt))
                if "SELECT id FROM organisations" in str(stmt):
                    return _Result([(ORG,)])
                return _Result([])

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        with patch.object(
            ra,
            "get_settings",
            return_value=SimpleNamespace(slot_reconcile_stale_seconds=1800, heartbeat_stale_retry_budget=3),
        ):
            result = await ra.reconcile_pipeline_slots(_Engine())  # type: ignore[arg-type]

        assert result["released"] == 0
        assert result["retried"] == 0
        release_stmts = [s for s in statements if "status = 'running'" in s and ":stale_seconds" in s]
        assert len(release_stmts) == 1
        sql = release_stmts[0]
        # The exclusion is OR'd with the over-budget arm, not AND'd onto the
        # whole WHERE: over-budget rows are terminal-failed regardless of phase.
        assert "claim_count > :retry_budget" in sql
        assert "dispatch_phase <> :first_node_phase" in sql


class TestNodeStartedTerminalizedTickAlert:
    """FAR-1649 qa finding 4: a tick that terminal-fails a node-STARTED row
    through the zero-node chokepoint is alert-grade. ``_dispatcher_reconcile_body``
    must emit its own ERROR (a distinct log event) rather than folding the
    count into the zero-node class, so a selection-leg race that reaches the
    chokepoint is visible on its own terms instead of passing silently.
    """

    async def test_body_emits_node_started_alert_when_counter_nonzero(self, caplog: Any) -> None:
        import logging

        summary = ch._dispatcher_summary()
        summary["node_started_terminalized"] = 2
        org_id = uuid.uuid4()

        async def fake_reconcile_org(*, org_id: uuid.UUID, **_kwargs: Any) -> int:
            return 0

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=[org_id]))
            stack.enter_context(patch.object(ch, "_reconcile_org", side_effect=fake_reconcile_org))
            stack.enter_context(patch.object(ch, "reconciler_recovery_predicate"))
            stack.enter_context(patch.object(ch, "_open_system_factory"))
            stack.enter_context(patch.object(ch, "_run_reconcile_sweeps", new_callable=AsyncMock))
            stack.enter_context(patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock))
            stack.enter_context(patch("modulo.core.cron_helpers.AsyncRedis"))
            with caplog.at_level(logging.ERROR, logger=ch.__name__):
                await ch._dispatcher_reconcile_body(
                    _settings=_settings(),
                    factory=MagicMock(),
                    queue_name="runs",
                    reenqueue_window=600,
                    tuning=ch.ReconcileTuning(
                        nodeless_window=15,
                        max_age_minutes=135,
                        claim_cap=3,
                        stale_window=90,
                        capacity_redispatch_seconds=60,
                        hitl_review_cancel_grace_seconds=3600,
                    ),
                    terminalize_max=25,
                    facts_max=25,
                    max_rows=500,
                    redis_client=MagicMock(),
                    summary=summary,
                    terminalized_run_ids=[],
                )

        messages = [record.getMessage() for record in caplog.records]
        assert any("node_started_terminalized_summary" in message for message in messages)
