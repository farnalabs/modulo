"""FAR-1728: the guardrail-override resume flip gets a reconcile self-heal.

FAR-1711 flips a node-started ``eval_blocked`` run to ``running`` (keeping the
durable ``dispatch_phase='first_node_dispatched'``, setting the DEDICATED
``guardrail_override_resume_pending`` marker column (migration 0305) and
stamping ``heartbeat_at`` one claim-stale window into the past) and
re-dispatches it as ``resume_run``. If that job is LOST — a worker crash before
claim, or ``dispatch_run`` returning ``deferred``/``enqueue_failed`` (the
override route's 500 leaves the committed flip behind) — no reconcile
re-dispatch leg matched the row:

* the generic stale-heartbeat leg excludes ``first_node_dispatched`` (FAR-1649);
* the pending legs require ``status='pending'``;
* the F6a leg requires ``awaiting_human``/``claimed``/``hitl_parked`` plus a
  committed HITL decision — which this path has none of;
* the nodeless-zombie predicate excludes ``first_node_dispatched``.

The only collector was the mid-graph age gate (``no_progress`` at ~135 min) —
fail-closed and truthful, but never retried.

The class discriminator is the DEDICATED marker column
(``runs.guardrail_override_resume_pending``), not ``is_replay``: ``is_replay``
is also written by webhook replay (``trigger_engine._create_webhook_run``) and
by a manual replay, so ``first_node_dispatched`` + ``is_replay`` is NOT the
flip's shape (the original QA finding on this branch — selecting on ``is_replay``
lifted the FAR-1649 belt for every replay-shaped row). The pre-0305 design used
a sentinel string in ``error_detail``; the dedicated boolean column replaces it
— an observation column must not carry a control-flow marker.

This file pins the self-heal leg (mirror of the F6a leg's shape):

* a ``running`` + ``first_node_dispatched`` + marker row with NO SAQ job is
  re-enqueued as ``resume_run`` (checkpoint resume, never ``execute_run``
  re-execution) — THE regression test; without the leg the row is held back by
  the node-started belt and only the age gate ever sees it;
* a webhook-replay-shaped row (``is_replay=True``, marker FALSE) is NOT
  selected/re-enqueued — the over-match regression (it fails on the pre-fix
  tree, where the leg selected on ``is_replay``);
* the belt still holds the same row shape WITHOUT the marker (the carve-out
  is class-scoped — the 2026-10-10 storm does not come back);
* a row whose thread carries no root checkpoint is deferred (FAR-1711 M1 —
  resuming it would silently re-execute it; the age gate keeps collecting it);
* an EXISTING SAQ job suppresses the re-enqueue (the job-absent gate);
* the SQL predicate itself selects the class (marker + phase + stale
  heartbeat, and never ``is_replay``), so the shared
  ``reconciler_recovery_predicate`` (D8 marker sweep) judges the row
  recoverable in lockstep.
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
from modulo.core.pipeline_engine import recovery as recovery_module

ORG = uuid.uuid4()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Minimal session double for one ``dispatcher_reconcile`` tick.

    Mirrors the shapes ``test_node_started_redispatch_guard`` handles: the RLS
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


def _override_row(
    run_id: uuid.UUID,
    *,
    status: str = "running",
    stale: bool = True,
    dispatch_phase: str | None = PHASE_FIRST_NODE_DISPATCHED,
    is_replay: bool | None = True,
    override_marker: bool = True,
    claim_count: int = 1,
) -> SimpleNamespace:
    """A reconcile-scan row shaped like a guardrail-override resume flip.

    Mirrors ``test_node_started_redispatch_guard._run_row`` plus the columns
    FAR-1728's leg needs: ``guardrail_override_resume_pending`` (the flip's
    single-writer dedicated marker column — True only when *override_marker*;
    a webhook/manual-replay row carries ``is_replay=True`` but the marker
    FALSE) and ``langgraph_thread_id`` (the resumable-checkpoint guard's probe
    key).
    """
    heartbeat = datetime.now(UTC) - timedelta(minutes=30) if stale else datetime.now(UTC)
    row = SimpleNamespace(
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
        guardrail_override_resume_pending=override_marker,
        langgraph_thread_id=f"thread-{run_id}",
        sandbox_dispatch_state=None,
    )
    if is_replay is not None:
        row.is_replay = is_replay
    return row


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
    job_exists: bool = False,
    checkpoint_present: bool = True,
) -> tuple[dict[str, Any], AsyncMock, _MockSession]:
    """Drive one real ``dispatcher_reconcile`` tick against a mocked env.

    ``job_exists`` feeds the shared ``q.job()`` read (the job-absent gate);
    ``checkpoint_present`` feeds the FAR-1711 M1 resumable-checkpoint guard.
    """
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    q = MagicMock()
    q.name = "runs"
    q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
    q.job = AsyncMock(return_value={"id": "existing-job"} if job_exists else None)
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
        stack.enter_context(
            patch.object(
                recovery_module,
                "_run_has_resumable_checkpoint",
                new_callable=AsyncMock,
                return_value=checkpoint_present,
            )
        )
        stack.enter_context(patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock))
        stack.enter_context(
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0)
        )
        summary = await ch.dispatcher_reconcile()
    return summary, reenqueue, session


class TestLostOverrideResumeJobIsReEnqueued:
    """The FAR-1728 regression: a lost override ``resume_run`` self-heals."""

    async def test_lost_override_resume_job_is_reenqueued_as_resume_run(self, monkeypatch: Any) -> None:
        """THE regression test (fails without the FAR-1728 leg).

        A ``running`` + ``first_node_dispatched`` + marker row with a stale
        heartbeat and NO SAQ job — exactly the shape the guardrail-
        override flip leaves behind when its ``resume_run`` job is lost — is
        re-enqueued as ``resume_run`` (checkpoint resume, never
        ``execute_run`` re-execution) and does NOT hit the node-started belt.
        Without the leg the row is held back (``node_started_deferred``) and
        only the mid-graph age gate ever collects it, ``no_progress`` ~135 min
        later — fail-closed but never retried.
        """
        run_id = uuid.uuid4()
        row = _override_row(run_id)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_awaited_once()
        # Job type is resume_run — checkpoint resume, NEVER execute_run
        # (FAR-1649: never re-execute a node-started run).
        assert reenqueue.await_args.args[3] == "resume_run"
        # No HITL decision exists on this path, so resume_data stays unset —
        # exactly what the override route's own dispatch passes.
        assert reenqueue.await_args.kwargs.get("resume_data") is None
        assert summary["repaired"] == 1
        # The class bypasses the belt (the belt is a re-EXECUTION guard; a
        # resume re-executes nothing).
        assert summary["node_started_deferred"] == 0

    async def test_control_node_started_row_without_the_override_marker_is_still_held_back(
        self, monkeypatch: Any
    ) -> None:
        """The belt's scope is UNCHANGED for every other node-started row: the
        same shape WITHOUT the flip's marker (the 2026-10-10 storm class —
        no replay flag either) is still held back — no enqueue, counted
        deferred — so the carve-out is strictly class-scoped and the storm
        does not come back."""
        row = _override_row(uuid.uuid4(), override_marker=False, is_replay=None)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["node_started_deferred"] == 1

    async def test_fresh_heartbeat_override_row_not_touched(self, monkeypatch: Any) -> None:
        """A FRESH-heartbeat override row (its resume job just claimed, or is
        mid-flight) is never touched — the SQL leg's staleness gate keeps the
        self-heal off a live attempt."""
        row = _override_row(uuid.uuid4(), stale=False)
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0


class TestSelfHealGuards:
    """The self-heal's two per-row guards (F6a-shape: gate, then precondition)."""

    async def test_existing_saq_job_suppresses_the_reenqueue(self, monkeypatch: Any) -> None:
        """Job-absent gate: while the original ``resume_run`` job hash still
        exists in Redis the reconcile NEVER re-enqueues (no double-dispatch
        while a live job is queued)."""
        row = _override_row(uuid.uuid4())
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row], job_exists=True)

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["skipped"] >= 1
        assert summary["node_started_deferred"] == 0

    async def test_no_root_checkpoint_defers_to_the_age_gate(self, monkeypatch: Any) -> None:
        """FAR-1711 M1 precondition, enforced per-row here too: a row whose
        thread carries no root checkpoint would be RE-EXECUTED by a resume
        (LangGraph seeds a fresh checkpoint and streams every node again), so
        the leg refuses and leaves the row to the age gate — fail-closed, no
        enqueue, counted ``skipped``."""
        row = _override_row(uuid.uuid4())
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row], checkpoint_present=False)

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["skipped"] >= 1
        assert summary["node_started_deferred"] == 0


class TestWebhookReplayIsNotOverMatched:
    """The QA finding on this branch: ``is_replay`` is NOT the flip's shape.

    ``is_replay=True`` is also written by webhook replay
    (``trigger_engine._create_webhook_run(..., is_replay=True)`` ->
    ``create_run``) and by a manual replay, so the ORIGINAL leg — which
    selected on ``is_replay`` — matched a wedged WEBHOOK-REPLAY row and
    re-enqueued it as ``resume_run``: a checkpoint resume that re-executes an
    in-progress, possibly non-idempotent super-step (the exact hazard the
    FAR-1649 node-started belt exists to prevent). The discriminator is now
    the flip's single-writer dedicated marker column.
    """

    async def test_webhook_replay_row_is_not_selected_or_reenqueued(self, monkeypatch: Any) -> None:
        """OVER-MATCH regression — this test FAILS on the pre-fix tree.

        A webhook-replay-shaped row (``status='running'``,
        ``dispatch_phase='first_node_dispatched'``, ``is_replay=True``, stale
        heartbeat, no SAQ job, marker FALSE — replay rows never carry the
        flip's marker) must NOT be selected/re-enqueued by the leg: it
        is held back by the node-started belt and left to the watchdogs / age
        gate, exactly like every other non-override node-started row.

        Pre-fix, the leg's ``Run.is_replay IS true`` condition matched this
        row and re-enqueued it ``resume_run`` (``repaired == 1``,
        ``node_started_deferred == 0``) — both assertions below fail. Post-fix
        the marker condition cannot match it.
        """
        run_id = uuid.uuid4()
        row = _override_row(run_id, override_marker=False, is_replay=True)
        # The row is genuinely a REPLAY row, not a marker-less storm row.
        assert row.is_replay is True
        assert row.guardrail_override_resume_pending is False

        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        # Falls through to the belt — held back, never re-executed.
        assert summary["node_started_deferred"] == 1


class TestPredicateSelectsTheOverrideResumeClass:
    """The SQL half: the shared predicate admits the class (D8 parity)."""

    def test_predicate_carries_the_override_resume_leg(self) -> None:
        import sqlalchemy as sa

        from modulo.db.models.run import Run

        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        compiled = sa.select(Run.id).where(predicate).compile()
        sql = str(compiled)
        # The leg's discriminators: running + the durable node-start phase
        # (bound, never a bare literal) + the DEDICATED marker column
        # (migration 0305) + the SAQ dispatcher. ``is_replay`` must NOT appear
        # anywhere in the predicate — webhook/manual replay share it, so it
        # cannot identify the flip (the over-match finding). ``error_detail``
        # must NOT appear either — the pre-0305 sentinel-in-error_detail
        # design is retired.
        assert "guardrail_override_resume_pending" in sql
        assert "error_detail" not in sql
        assert "is_replay" not in sql
        assert sql.count("dispatch_phase IS NULL") == 1  # still exactly ONE exclusion leg
        assert PHASE_FIRST_NODE_DISPATCHED in dict(compiled.params).values()
        assert "status" in sql

    def test_predicate_still_excludes_generic_running_leg(self) -> None:
        """The generic stale-heartbeat leg's node-started EXCLUSION is
        untouched — only the new positive leg binds the phase constant a
        second time."""
        import sqlalchemy as sa

        from modulo.db.models.run import Run

        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        compiled = sa.select(Run.id).where(predicate).compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)
        assert "runs.dispatch_phase IS NULL OR runs.dispatch_phase != " in sql
        assert "runs.is_replay" not in sql
        # The new leg's positive match (never an exclusion): the dedicated
        # marker column's IS-true, beside the phase constant's inlined literal.
        assert "runs.dispatch_phase = 'first_node_dispatched' AND runs.guardrail_override_resume_pending IS true" in sql
