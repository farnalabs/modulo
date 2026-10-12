"""FAR-1715 — independent SAQ zero-progress terminalisation in the stale sweep.

``stale_run_recovery_sweep`` gains a zero-progress branch for
``dispatcher='saq'`` runs: the SAME predicate shape the dispatcher_reconcile
nodeless router selects with (``cron_helpers._ZERO_PROGRESS_SHAPE_SQL``), the
SAME Settings knob (``saq_zero_progress_max_age_minutes``), the SAME
``executor_stalled`` error_code, a ``status='running'`` guard (double-firing
with the reconcile path is a no-op) and a per-org row cap.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import MagicMock, patch

import modulo.core.pipeline_execution as pe
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY


class _CapturingConn:
    """AsyncConnection double recording statements/params; rows configurable.

    Faithful shape for the sweep helpers: ``dialect`` for the row-lock bound
    (a non-postgres double takes its documented no-op branch), async context
    manager + ``begin()`` returning self, and ``execute`` capturing the
    compiled statement string and bind params.
    """

    def __init__(
        self,
        *,
        org_rows: list[Any] | None = None,
        zero_progress_rows: list[Any] | None = None,
        dialect_name: str = "sqlite",
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements: list[str] = []
        self.params: list[dict[str, object]] = []
        self._org_rows = org_rows or []
        self._zero_progress_rows = zero_progress_rows or []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _CapturingConn:
        return self

    async def execute(self, stmt: object, bind: dict[str, object] | None = None) -> MagicMock:
        sql = str(stmt)
        self.statements.append(sql)
        self.params.append(bind or {})
        result = MagicMock()
        if "SELECT id FROM organisations" in sql:
            result.all.return_value = list(self._org_rows)
        elif "dispatcher='saq'" in sql:
            # The FAR-1715 zero-progress UPDATE — the only sweep statement
            # carrying the unspaced saq dispatcher guard.
            result.all.return_value = list(self._zero_progress_rows)
            result.rowcount = len(self._zero_progress_rows)
        else:
            result.all.return_value = []
            result.rowcount = 0
        return result


class _CapturingEngine:
    def __init__(self, conn: _CapturingConn) -> None:
        self._conn = conn

    def connect(self) -> _CapturingConn:
        return self._conn


class TestSweepOrgSaqZeroProgressRuns:
    """Row-level behaviour of the new branch's helper (in-memory double)."""

    async def test_assembled_sql_carries_the_shared_shape_and_guards(self) -> None:
        """The UPDATE reuses ``cron_helpers._ZERO_PROGRESS_SHAPE_SQL`` verbatim
        (single definition — no drift from the reconcile router) and keeps the
        ``status='running'`` + ``dispatcher='saq'`` guards + the row cap."""
        import modulo.core.cron_helpers as ch

        conn = _CapturingConn()
        count = await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=uuid.uuid4(),
            max_age_minutes=135,
            max_rows=pe._SAQ_ZERO_PROGRESS_SWEEP_MAX_ROWS,
            terminalised_run_ids=[],
        )

        assert count == 0
        update_statements = [s for s in conn.statements if s.startswith("UPDATE runs")]
        assert len(update_statements) == 1
        sql = update_statements[0]
        # The shared shape is assembled in — not a re-spelling of it.
        assert ch._ZERO_PROGRESS_SHAPE_SQL in sql
        assert "__ZERO_PROGRESS_SHAPE__" not in sql
        assert "status='running'" in sql
        assert "dispatcher='saq'" in sql
        assert "LIMIT :max_rows" in sql
        assert ":max_age_minutes" in sql

    async def test_binds_the_shared_constants_and_knob(self) -> None:
        """final_key / first_node_phase bind the SHARED constants; the age
        bound and row cap travel as binds; the error_code is the age-bound
        path's ``executor_stalled``."""
        conn = _CapturingConn()
        org_id = uuid.uuid4()
        count = await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=org_id,
            max_age_minutes=200,
            max_rows=7,
            terminalised_run_ids=[],
        )

        assert count == 0
        update_params = [p for p in conn.params if "code" in p]
        assert len(update_params) == 1
        bound = update_params[0]
        assert bound["oid"] == str(org_id)
        assert bound["code"] == pe.EXECUTOR_STALLED_ERROR_CODE
        assert bound["code"] == "executor_stalled"
        assert "stale_run_recovery zero-progress sweep" in str(bound["detail"])
        assert "200m age bound" in str(bound["detail"])
        assert bound["final_key"] == FINAL_ATTEMPT_KEY
        assert bound["first_node_phase"] == PHASE_FIRST_NODE_DISPATCHED
        assert bound["max_age_minutes"] == 200
        assert bound["max_rows"] == 7

    async def test_scopes_the_org_and_bounds_the_row_lock(self) -> None:
        """The helper is self-contained in the caller's transaction: on
        postgres it issues the hot-``runs`` row-lock bound FIRST, then the
        org ``set_config``."""
        conn = _CapturingConn(dialect_name="postgresql")
        org_id = uuid.uuid4()
        await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=org_id,
            max_age_minutes=135,
            max_rows=25,
            terminalised_run_ids=[],
        )

        assert len(conn.statements) >= 2
        assert "lock_timeout" in conn.statements[0]
        # The org set_config binds the org id (the lock bound binds the
        # timeout value into its own set_config — both use a "val" param).
        assert any(p.get("val") == str(org_id) for p in conn.params)

    async def test_counts_and_collects_terminalised_rows(self) -> None:
        """Rows returned by the UPDATE are counted and collected into the
        caller's terminalised list (journeys advance + compensating daily
        facts ride on that list, like the other sweep branches)."""
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        conn = _CapturingConn(zero_progress_rows=[(run_id,)])
        terminalised: list[tuple[uuid.UUID, uuid.UUID]] = []

        count = await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=org_id,
            max_age_minutes=135,
            max_rows=25,
            terminalised_run_ids=terminalised,
        )

        assert count == 1
        assert terminalised == [(run_id, org_id)]


class TestStaleRunRecoverySweepWiring:
    """The sweep resolves the knob once, runs the branch per org, reports it."""

    async def test_sweep_runs_the_branch_with_the_knob_and_row_cap(self) -> None:
        """The per-org loop calls the zero-progress branch after the legacy
        UPDATEs, binding the resolved knob and the module row cap, and reports
        ``saq_zero_progress_swept`` in the result."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = 200
        with patch.object(pe, "get_settings", return_value=settings):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert result["saq_zero_progress_swept"] == 0
        zp_statements = [s for s in conn.statements if "dispatcher='saq'" in s]
        assert len(zp_statements) == 1
        zp_params = [p for p in conn.params if "max_age_minutes" in p and "final_key" in p]
        assert len(zp_params) == 1
        assert zp_params[0]["max_age_minutes"] == 200
        assert zp_params[0]["max_rows"] == pe._SAQ_ZERO_PROGRESS_SWEEP_MAX_ROWS

    async def test_sweep_falls_back_to_the_historical_bound_when_knob_is_unset(self) -> None:
        """An unset (None) knob — the shipped default — keeps the pre-FAR-1715
        ~135m derivation: behaviour-neutral at the default."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = None
        with patch.object(pe, "get_settings", return_value=settings):
            await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        zp_params = [p for p in conn.params if "max_age_minutes" in p and "final_key" in p]
        assert len(zp_params) == 1
        assert zp_params[0]["max_age_minutes"] == 135

    async def test_sweep_reports_terminalised_zero_progress_rows(self) -> None:
        """A row selected by the branch lands in the result count."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        run_id = uuid.uuid4()
        conn = _CapturingConn(org_rows=[(org_id,)], zero_progress_rows=[(run_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        with (
            patch.object(pe, "get_settings", return_value=settings),
            # The post-commit advance machinery is exercised elsewhere; keep
            # this test on the sweep's own counting/selection behaviour.
            patch.object(pe, "_advance_terminalised_run", new=AsyncMockNone()),
        ):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert result["saq_zero_progress_swept"] == 1

    async def test_no_orgs_early_return_carries_the_new_key(self) -> None:
        """The no-orgs early return stays key-complete."""
        conn = _CapturingConn(org_rows=[])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        with patch.object(pe, "get_settings", return_value=settings):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert result["saq_zero_progress_swept"] == 0
        assert result["never_dispatched_swept"] == 0


class AsyncMockNone:
    """Async no-op stand-in for the post-commit terminal advance."""

    async def __call__(self, *args: object, **kwargs: object) -> None:
        return None
