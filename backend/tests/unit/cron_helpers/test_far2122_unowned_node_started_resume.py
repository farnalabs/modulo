"""FAR-2122: a dispatcherless node-started run recovers by CHECKPOINT RESUME.

A ``running`` + ``first_node_dispatched`` run whose ``dispatcher`` is NULL (or a
foreign value) was previously only terminalised by the stale-run sweep's
``_SWEEP_ORPHANED_RUNS_SQL`` arm.  FAR-2122 adds a ``dispatcher_reconcile`` scan
leg that recovers it via ``resume_run`` — checkpoint resume, never
``execute_run`` re-execution — when its thread carries a resumable checkpoint.
The sweep keeps a one-tick precedence grace so the resume arm acts first; a row
with no checkpoint is refused here and left to the sweep.

This file pins the FOUR pieces the review asked for:

* the scan leg (``_build_re_dispatch_predicate``) selects the class and only it;
* the row recheck ``_is_unowned_node_started_resume_row`` (including tz-naive
  heartbeat normalisation via ``_as_utc``);
* the per-row handler through a REAL ``dispatcher_reconcile`` tick — a
  checkpoint-bearing row is re-enqueued as ``resume_run``; a checkpointless row
  is skipped (left to the sweep);
* the ``NOT EXISTS run_node_outputs`` recency clause in ``_MID_GRAPH_WEDGE_SQL``
  — executed for real against SQLite via the shared fragment, so a recent node
  output shields the run and a stale one does not. The window is a BOUND param
  from the tuning registry, never a hard-coded SQL literal.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine import recovery as recovery_module
from modulo.db.models.run import Run
from tests.unit.api.mock_session import make_redis_client

ORG = uuid.uuid4()
STALE_WINDOW = 600

# A minimal SELECT that exercises the production fragment in isolation.
_RECENT_OUTPUT_SHIELD_SELECT = "SELECT id FROM runs WHERE " + ch._NODE_OUTPUT_PROGRESS_ABSENT_SQL + " ORDER BY id"  # noqa: S608  # nosec B608 - module constant only


# ---------------------------------------------------------------------------
# The scan leg
# ---------------------------------------------------------------------------


def _compile_predicate(*, stale_window: int = STALE_WINDOW) -> str:
    predicate = ch._build_re_dispatch_predicate(
        reenqueue_window=600,
        stale_window=stale_window,
        capacity_redispatch_seconds=120,
    )
    return str(sa.select(Run.id).where(predicate).compile(compile_kwargs={"literal_binds": True}))


class TestScanPredicateCarriesTheUnownedResumeLeg:
    def test_leg_matches_running_node_started_without_an_saq_dispatcher(self) -> None:
        """The leg is ``running + (dispatcher IS NULL OR dispatcher != 'saq')
        + first_node_dispatched + stale/NULL heartbeat``. The compiled SQL
        contains that exact shape — and it is distinct from the FAR-1803 saq
        leg, which requires ``dispatcher = 'saq'``."""
        sql = _compile_predicate()

        assert "runs.dispatcher IS NULL OR runs.dispatcher != 'saq'" in sql
        assert "runs.status = 'running' AND (runs.dispatcher IS NULL OR runs.dispatcher != 'saq')" in sql
        assert (
            "runs.dispatch_phase = 'first_node_dispatched' AND "
            "(runs.heartbeat_at IS NULL OR runs.heartbeat_at < now() - 600 * interval '1 second')"
        ) in sql
        # The FAR-1803 saq-owned leg is untouched.
        assert "runs.dispatcher = 'saq' AND runs.dispatch_phase = 'first_node_dispatched'" in sql

    def test_leg_uses_the_configured_stale_window(self) -> None:
        sql = _compile_predicate(stale_window=42)
        assert "now() - 42 * interval '1 second'" in sql


class TestUnownedNodeStartedResumeRowRecheck:
    """The row-side belt, mirroring the sibling ``_is_*_row`` rechecks."""

    def _row(self, **overrides: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "status": "running",
            "dispatcher": None,
            "dispatch_phase": PHASE_FIRST_NODE_DISPATCHED,
            "heartbeat_at": datetime.now(UTC) - timedelta(seconds=STALE_WINDOW + 30),
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_stale_unowned_node_started_row_is_selected(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(), STALE_WINDOW) is True

    def test_foreign_dispatcher_is_also_selected(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(dispatcher="legacy"), STALE_WINDOW) is True

    def test_saq_owned_row_is_not_selected(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(dispatcher="saq"), STALE_WINDOW) is False

    def test_fresh_heartbeat_is_a_live_attempt(self) -> None:
        assert (
            ch._is_unowned_node_started_resume_row(
                self._row(heartbeat_at=datetime.now(UTC) - timedelta(seconds=5)), STALE_WINDOW
            )
            is False
        )

    def test_null_heartbeat_is_stale_evidence(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(heartbeat_at=None), STALE_WINDOW) is True

    def test_wrong_phase_is_rejected(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(dispatch_phase="claimed"), STALE_WINDOW) is False

    def test_not_running_is_rejected(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(status="pending"), STALE_WINDOW) is False

    def test_non_datetime_heartbeat_fails_closed(self) -> None:
        assert ch._is_unowned_node_started_resume_row(self._row(heartbeat_at="nope"), STALE_WINDOW) is False

    def test_naive_heartbeat_is_normalised_to_utc(self) -> None:
        """A tz-naive heartbeat (legacy row / driver dropping tzinfo) must not
        raise on subtraction — ``_as_utc`` treats it as UTC."""
        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=STALE_WINDOW + 30)
        assert ch._is_unowned_node_started_resume_row(self._row(heartbeat_at=naive), STALE_WINDOW) is True


# ---------------------------------------------------------------------------
# The per-row handler, driven through a real dispatcher_reconcile tick
# ---------------------------------------------------------------------------


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Minimal session double for one ``dispatcher_reconcile`` tick.

    Mirrors ``test_far1728_override_resume_selfheal._MockSession``: the RLS
    probe, the terminalizer statements (zero rows), the capacity pipeline
    lookup, then the org/row results.
    """

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
            r = MagicMock()
            r.all.return_value = []
            r.rowcount = 0
            return r
        if "UPDATE runs SET" in s:
            r = MagicMock()
            r.all.return_value = []
            r.rowcount = 0
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


def _unowned_row(run_id: uuid.UUID) -> SimpleNamespace:
    """The scan row the FAR-2122 leg selects (no SAQ dispatcher identity)."""
    return SimpleNamespace(
        id=run_id,
        pipeline_id=uuid.uuid4(),
        trigger_id=None,
        status="running",
        dispatched_at=datetime.now(UTC) - timedelta(minutes=40),
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=30),
        node_token_usage={},
        outputs_absent=False,
        checkpoints_absent=False,
        started_at=datetime.now(UTC) - timedelta(minutes=60),
        error_code=None,
        dispatcher=None,
        enqueue_failed_at=None,
        claim_count=1,
        retry_policy=None,
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
        guardrail_override_resume_pending=False,
        langgraph_thread_id=f"thread-{run_id}",
        sandbox_dispatch_state=None,
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
        "dispatcher_reconcile_node_output_progress_seconds": 900,
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
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    factory = MagicMock(return_value=session)
    redis_client = make_redis_client()
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
            patch.object(ch, "_awaiting_human_has_committed_decision", new_callable=AsyncMock, return_value=True)
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


class TestHandlerRecoversByCheckpointResume:
    async def test_unowned_node_started_row_is_reenqueued_as_resume_run(self, monkeypatch: Any) -> None:
        """THE regression: without the leg the row is only terminalised by the
        sweep; with it, a checkpoint-bearing row resumes from its checkpoint —
        never ``execute_run`` (FAR-1649)."""
        run_id = uuid.uuid4()
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [_unowned_row(run_id)])

        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        assert reenqueue.await_args.kwargs.get("resume_data") is None
        assert summary["repaired"] == 1
        assert summary["node_started_executor_lost"] == 0

    async def test_no_checkpoint_is_left_to_the_orphan_sweep(self, monkeypatch: Any) -> None:
        """A checkpointless row would be RE-EXECUTED from the graph start by a
        resume, so the handler refuses it (counted skipped) and the sweep's
        terminaliser owns it."""
        summary, reenqueue, _session = await _run_reconcile(
            monkeypatch, [_unowned_row(uuid.uuid4())], checkpoint_present=False
        )

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["skipped"] >= 1

    async def test_existing_saq_job_suppresses_the_resume(self, monkeypatch: Any) -> None:
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [_unowned_row(uuid.uuid4())], job_exists=True)

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0


# ---------------------------------------------------------------------------
# The mid-graph age gate's recent-node-output clause
# ---------------------------------------------------------------------------


class _CaptureSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.calls.append((str(stmt), dict(params or {})))
        result = MagicMock()
        result.all.return_value = []
        return result

    async def flush(self) -> None:
        return


async def test_mid_graph_wedge_binds_the_recent_node_output_window() -> None:
    """The 15-minute literal is gone: the window is threaded from the tuning
    registry and bound as a cutoff datetime."""
    session = _CaptureSession()
    await ch._terminalize_mid_graph_wedges(session, ORG, max_age_minutes=135, node_output_progress_seconds=1234)

    stmt, params = session.calls[-1]
    assert "interval '15 minutes'" not in stmt
    assert "NOT EXISTS (SELECT 1 FROM run_node_outputs rno_progress" in stmt
    assert "rno_progress.updated_at >= :node_output_progress_cutoff" in stmt
    cutoff = params["node_output_progress_cutoff"]
    # The bound cutoff is now - window (small execution tolerance).
    delta = datetime.now(UTC) - cutoff
    assert timedelta(seconds=1220) <= delta <= timedelta(seconds=1250)


async def test_mid_graph_wedge_defaults_to_the_tuned_setting(
    monkeypatch: Any,
) -> None:
    session = _CaptureSession()
    monkeypatch.setattr(
        ch,
        "get_settings",
        lambda: MagicMock(
            saq_job_heartbeat=300,
            dispatcher_reconcile_node_output_progress_seconds=1800,
        ),
    )
    await ch._terminalize_mid_graph_wedges(session, ORG, max_age_minutes=135)

    _stmt, params = session.calls[-1]
    delta = datetime.now(UTC) - params["node_output_progress_cutoff"]
    assert timedelta(seconds=1780) <= delta <= timedelta(seconds=1820)


async def test_recent_node_output_shields_the_run_and_a_stale_one_does_not() -> None:
    """Behavioural proof of the clause against SQLite: a run with a node output
    inside the window is EXCLUDED; one whose last output is old is selected.

    The production statement is Postgres-only (``ctid``/``now()``/casts), so this
    executes the shared fragment the statement is assembled from. The fragment
    is the SAME constant ``_terminalize_mid_graph_wedges`` composes in — it
    cannot drift from the production predicate.
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    now = datetime.now(UTC).replace(tzinfo=None)
    # Pass ISO strings (not datetimes): Python 3.12 deprecates the default
    # sqlite3 datetime adapter, and the suite turns DeprecationWarning into an
    # error. Lexicographic order on this fixed format is chronological order.
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE runs (id VARCHAR PRIMARY KEY)")
            await conn.exec_driver_sql("CREATE TABLE run_node_outputs (run_id VARCHAR, updated_at DATETIME)")
            await conn.execute(text("INSERT INTO runs (id) VALUES ('recent'), ('stale'), ('none')"))
            await conn.execute(
                text(
                    "INSERT INTO run_node_outputs (run_id, updated_at) VALUES "
                    "(:recent_id, :recent_at), (:stale_id, :stale_at)"
                ),
                {
                    "recent_id": "recent",
                    "recent_at": (now - timedelta(minutes=1)).strftime(fmt),
                    "stale_id": "stale",
                    "stale_at": (now - timedelta(minutes=120)).strftime(fmt),
                },
            )

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(_RECENT_OUTPUT_SHIELD_SELECT),
                    {"node_output_progress_cutoff": (now - timedelta(seconds=900)).strftime(fmt)},
                )
            ).all()
        # 'none' has no output at all; 'stale' last wrote 2 hours ago. Only the
        # recent write shields its run.
        assert [row[0] for row in rows] == ["none", "stale"]
    finally:
        await engine.dispose()
