"""FAR-1886 B1 — terminal-path triage evidence and the triage envelope data.

``deadline_triage`` used to exist only for ``node.deadline_exceeded``. The
age-gate ``run.no_progress``, the stranded-pending backstop, the FAR-1999
node-deadline reaper and ``harness.worker_lost`` terminalisations returned
``[]``, so "executor died" and "silent stall" were indistinguishable on the
run page. These tests pin the three parts of the fix:

* the projection carries ``run_attempt_count`` — DATA from the run row, never
  a cause — and no derived age metric;
* ``persist_terminal_triage_marker`` writes the synthetic entry with a reason
  PASSED IN by the caller, inside one savepoint (reads included), under the
  same run-row ``FOR UPDATE`` serialisation the raw-output marker store uses,
  bounded by a client-side timeout;
* ``fail_run_terminal`` persists that entry for ``harness.worker_lost`` only.

The age-gate wiring is covered in
``tests/unit/cron_helpers/test_dispatcher_reconcile.py``. No DB, no network.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes import runs as runs_module
from modulo.core import cron_helpers as ch
from modulo.core import pipeline_execution as pe
from modulo.core.worker_handoff import WORKER_LOST_ERROR_CODE

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN = uuid.UUID("00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Fake sessions / results
# ---------------------------------------------------------------------------


class _Nested:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        self._session.depth += 1
        return self

    async def __aexit__(self, *args: object) -> bool:
        self._session.depth -= 1
        return False


class _Result:
    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None, one: Any = None) -> None:
        self._scalar = scalar
        self._rows = rows or []
        self._one = one

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._scalar

    def one(self) -> Any:
        return self._one

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    """Models the one behaviour under test: an error outside a savepoint
    aborts the caller's transaction (every later statement fails); an error
    inside one leaves the transaction usable."""

    def __init__(self, run: Any, *, one: Any = None, rows: list[Any] | None = None) -> None:
        self.run = run
        self.one = one
        self.rows = rows
        self.depth = 0
        self.aborted = False
        self.statements: list[Any] = []
        self.execute_depths: list[int] = []

    @property
    def in_savepoint(self) -> bool:
        return self.depth > 0

    async def _guard(self) -> None:
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored until end")

    async def get(self, model: Any, pk: Any, **kwargs: Any) -> Any:
        await self._guard()
        return self.run

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        await self._guard()
        self.statements.append(stmt)
        self.execute_depths.append(self.depth)
        return _Result(scalar=self.run, rows=self.rows, one=self.one)

    async def flush(self) -> None:
        await self._guard()

    def begin_nested(self) -> _Nested:
        return _Nested(self)


def _writer_session(run: Any) -> _Session:
    return _Session(run)


# ---------------------------------------------------------------------------
# Projection: the envelope's data field (no derived metric)
# ---------------------------------------------------------------------------


def test_projection_carries_run_attempt_count_as_data() -> None:
    markers = {
        f"terminal-triage:{_RUN}": {
            "deadline_triage": True,
            "node_id": None,
            "no_output_reason": "executor lost (worker handoff)",
            "raw_output": "",
            "sandbox_log_tail": "",
            "stdout_length": 0,
            "stderr_length": 0,
        }
    }
    entries = runs_module._deadline_triage_from_markers(markers, run_attempt_count=2)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["no_output_reason"] == "executor lost (worker handoff)"
    assert entry["run_attempt_count"] == 2
    # The derived age metric was removed: it measured how the run was
    # terminalised, not whether the attempt was productive.
    assert "last_output_age_seconds" not in entry
    # Same envelope keys as a per-attempt deadline marker.
    assert entry["attempt_key"] == f"terminal-triage:{_RUN}"
    assert not entry["raw_output_tail"]
    assert entry["stdout_length"] == 0


def test_projection_defaults_run_attempt_count_to_none() -> None:
    markers = {"k": {"deadline_triage": True, "node_id": "n1"}}
    entry = runs_module._deadline_triage_from_markers(markers)[0]
    assert entry["run_attempt_count"] is None
    assert "last_output_age_seconds" not in entry


def test_run_attempt_count_normalises_a_non_int_value_to_none() -> None:
    assert runs_module._run_attempt_count(SimpleNamespace(node_attempt_count=3)) == 3
    assert runs_module._run_attempt_count(SimpleNamespace(node_attempt_count=None)) is None
    assert runs_module._run_attempt_count(SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# Synthetic marker writer — reason, savepoint, serialisation, bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "error_code"),
    [
        (ch._TERMINAL_TRIAGE_REASON_AGE_BOUND, "no_progress"),
        (ch._TERMINAL_TRIAGE_REASON_STRANDED_PENDING, "no_progress"),
        (ch._TERMINAL_TRIAGE_REASON_NODE_DEADLINE, "node_deadline_exceeded"),
        (ch._TERMINAL_TRIAGE_REASON_WORKER_LOST, "harness.worker_lost"),
    ],
)
async def test_writer_states_the_callers_reason_verbatim(reason: str, error_code: str) -> None:
    session = _writer_session(SimpleNamespace(node_attempt_count=1))
    write = AsyncMock()
    with (
        patch("modulo.db.crud.run_node_outputs.read_run_markers", AsyncMock(return_value=None)),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", write),
    ):
        assert await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code=error_code,
            reason=reason,
        )

    entry = write.await_args.kwargs["markers"][f"terminal-triage:{_RUN}"]
    assert entry["deadline_triage"] is True
    assert entry["no_output_reason"] == reason
    assert entry["attempt_key"] == f"terminal-triage:{_RUN}"
    assert entry["node_id"] is None


async def test_writer_takes_the_run_row_for_update_inside_the_savepoint() -> None:
    """The read-merge-write must serialise like the raw-output marker store:
    the run row is locked FOR UPDATE, and the lock is taken inside the
    savepoint (so a lock/read failure cannot abort the caller)."""
    session = _writer_session(SimpleNamespace(node_attempt_count=1))
    read_depths: list[int] = []

    async def _read(sess: Any, *, run_id: Any, organisation_id: Any = None) -> Any:
        read_depths.append(sess.depth)
        return {}

    with (
        patch("modulo.db.crud.run_node_outputs.read_run_markers", _read),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", AsyncMock()),
    ):
        assert await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )

    assert read_depths == [1]
    assert session.execute_depths == [1]
    lock_stmt = session.statements[0]
    assert lock_stmt._for_update_arg is not None
    assert session.depth == 0


async def test_read_failure_leaves_the_callers_transaction_usable() -> None:
    """A DB-level read failure inside the savepoint must not poison the
    caller's transaction (the reconcile org pass keeps committing the
    terminalisations it already wrote)."""
    session = _writer_session(SimpleNamespace(node_attempt_count=0))

    async def _failing_read(sess: Any, *, run_id: Any, organisation_id: Any = None) -> Any:
        if sess.depth == 0:
            # Mirrors Postgres: an error outside a savepoint aborts the
            # enclosing transaction.
            sess.aborted = True
        raise RuntimeError("read aborted the transaction")

    with patch("modulo.db.crud.run_node_outputs.read_run_markers", _failing_read):
        assert not await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )

    assert session.depth == 0
    assert session.aborted is False
    # The proof the transaction is still usable: the next statement succeeds.
    await session.execute("select 1")


async def test_writer_is_best_effort_on_a_write_failure() -> None:
    session = _writer_session(SimpleNamespace(node_attempt_count=2))
    with (
        patch("modulo.db.crud.run_node_outputs.read_run_markers", AsyncMock(return_value={})),
        patch(
            "modulo.db.crud.run_node_outputs.write_run_markers",
            AsyncMock(side_effect=RuntimeError("write failed")),
        ),
    ):
        assert not await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )

    assert session.depth == 0
    assert session.aborted is False


async def test_writer_skips_when_the_run_row_is_not_visible() -> None:
    session = _writer_session(None)
    write = AsyncMock()
    with (
        patch("modulo.db.crud.run_node_outputs.read_run_markers", AsyncMock(return_value={})),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", write),
    ):
        assert not await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )

    write.assert_not_awaited()
    assert session.depth == 0


async def test_writer_is_bounded_by_a_client_side_timeout() -> None:
    """A contended run row must not stall the caller's terminalisation
    transaction: the whole read-merge-write is bounded."""

    async def _hanging_read(sess: Any, *, run_id: Any, organisation_id: Any = None) -> Any:
        await asyncio.sleep(30)
        return {}

    session = _writer_session(SimpleNamespace(node_attempt_count=1))
    with (
        patch.object(ch, "_TERMINAL_TRIAGE_PERSIST_TIMEOUT", 0.05),
        patch("modulo.db.crud.run_node_outputs.read_run_markers", _hanging_read),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", AsyncMock()),
    ):
        assert not await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )

    # The savepoint unwound cleanly, so the caller can keep using the session.
    assert session.depth == 0
    assert session.aborted is False


async def test_writer_propagates_cancellation() -> None:
    """Cancellation is not a triage failure: it must propagate, never be
    swallowed as a best-effort miss."""
    session = _writer_session(SimpleNamespace(node_attempt_count=1))

    async def _cancelled_read(sess: Any, *, run_id: Any, organisation_id: Any = None) -> Any:
        raise asyncio.CancelledError

    with (
        patch("modulo.db.crud.run_node_outputs.read_run_markers", _cancelled_read),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )


async def test_writer_preserves_concurrently_written_markers() -> None:
    """The read and the write share one savepoint under the run-row lock, so
    the merge sees (and re-sends) another writer's key rather than deleting it
    via ``write_run_markers``' delete-absent half."""
    session = _writer_session(SimpleNamespace(node_attempt_count=1))
    write = AsyncMock()
    with (
        patch(
            "modulo.db.crud.run_node_outputs.read_run_markers",
            AsyncMock(return_value={"run:r:node:n1:1": {"deadline_triage": True}}),
        ),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", write),
    ):
        assert await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
            reason=ch._TERMINAL_TRIAGE_REASON_AGE_BOUND,
        )

    markers = write.await_args.kwargs["markers"]
    assert "run:r:node:n1:1" in markers
    assert f"terminal-triage:{_RUN}" in markers


# ---------------------------------------------------------------------------
# Terminaliser wiring — each call site passes its own observation
# ---------------------------------------------------------------------------


async def test_stranded_pending_backstop_writes_its_reason() -> None:
    run_id = uuid.uuid4()
    session = _Session(None, rows=[(run_id,)])
    writer = AsyncMock(return_value=True)
    with patch.object(ch, "persist_terminal_triage_marker", writer):
        terminalised = await ch._terminalize_stranded_pending_runs(session, _ORG, max_age_minutes=135)  # type: ignore[arg-type]

    assert terminalised == [run_id]
    writer.assert_awaited_once()
    kwargs = writer.await_args.kwargs
    assert kwargs["run_id"] == run_id
    assert kwargs["organisation_id"] == _ORG
    assert kwargs["error_code"] == "no_progress"
    assert kwargs["reason"] == "stranded pending run past the age bound"


async def test_node_deadline_reaper_writes_its_reason() -> None:
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    run = SimpleNamespace(
        id=run_id,
        status="running",
        dispatch_phase=ch.PHASE_FIRST_NODE_DISPATCHED,
        node_token_usage=None,
        node_deadline_watchdog_fired_count=0,
        started_at=now - timedelta(hours=2),
        dispatch_phase_entered_at=now - timedelta(hours=2),
        pipeline_id=uuid.uuid4(),
        trigger_id=uuid.uuid4(),
        claim_count=1,
        error_code=None,
        error_detail=None,
        completed_at=None,
    )
    session = _Session(run, one=SimpleNamespace(outputs_absent=True, checkpoints_absent=True))
    writer = AsyncMock(return_value=True)
    summary = {"node_deadline_reaped": 0}
    with (
        patch.object(ch, "_is_node_started_executor_lost_row", return_value=True),
        patch.object(ch, "_node_started_deadline", return_value=now - timedelta(minutes=1)),
        patch.object(ch, "_ingest_saq_error", AsyncMock()),
        patch.object(ch, "persist_terminal_triage_marker", writer),
    ):
        reaped = await ch._fail_node_deadline_run(
            session,  # type: ignore[arg-type]
            run_id,
            _ORG,
            summary,
            stale_window=60,
            node_budget_seconds=600,
        )

    assert reaped is True
    assert summary["node_deadline_reaped"] == 1
    writer.assert_awaited_once()
    kwargs = writer.await_args.kwargs
    assert kwargs["error_code"] == "node_deadline_exceeded"
    assert kwargs["reason"] == "node started, no terminal envelope before its deadline"


# ---------------------------------------------------------------------------
# fail_run_terminal wiring (harness.worker_lost only)
# ---------------------------------------------------------------------------


class _FakeConn:
    dialect = SimpleNamespace(name="sqlite")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _FakeConn:
        return self

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        result = MagicMock()
        result.fetchone.return_value = ("id",)
        return result


class _FakeEngine:
    def connect(self) -> _FakeConn:
        return _FakeConn()


async def test_fail_run_terminal_persists_triage_for_worker_lost() -> None:
    engine = _FakeEngine()
    persist = AsyncMock()
    with (
        patch.object(pe, "_advance_journeys_from_stored_refs", AsyncMock()),
        patch.object(pe, "_record_fact_for_terminal_failed_run", AsyncMock()),
        patch.object(pe, "_persist_worker_lost_triage", persist),
    ):
        ok = await pe.fail_run_terminal(
            engine,  # type: ignore[arg-type]
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
            error_code="harness.worker_lost",
            error_detail="worker shut down",
        )

    assert ok is True
    persist.assert_awaited_once()


async def test_fail_run_terminal_skips_triage_for_other_codes() -> None:
    engine = _FakeEngine()
    persist = AsyncMock()
    with (
        patch.object(pe, "_advance_journeys_from_stored_refs", AsyncMock()),
        patch.object(pe, "_record_fact_for_terminal_failed_run", AsyncMock()),
        patch.object(pe, "_persist_worker_lost_triage", persist),
    ):
        ok = await pe.fail_run_terminal(
            engine,  # type: ignore[arg-type]
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
            error_code="executor_stalled",
            error_detail="boom",
        )

    assert ok is True
    persist.assert_not_awaited()


def test_worker_lost_code_is_the_registry_constant() -> None:
    """The literal is a circular-import workaround, so pin it to the
    authoritative registry value; the reason is the shared terminal-path
    vocabulary (one definition, no drift)."""
    assert pe._WORKER_LOST_TRIAGE_ERROR_CODE == WORKER_LOST_ERROR_CODE
    assert ch._TERMINAL_TRIAGE_REASON_WORKER_LOST == "executor lost (worker handoff)"


def _worker_lost_factory() -> tuple[MagicMock, AsyncMock]:
    """The ``async_sessionmaker`` double the worker-lost helper opens."""
    session = AsyncMock()

    class _Begin:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> bool:
            return False

    session.begin = MagicMock(return_value=_Begin())
    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=factory_cm), session


async def test_worker_lost_triage_helper_sets_rls_and_delegates_the_reason() -> None:
    """The helper opens its own RLS-scoped session and delegates with the
    exact ``harness.worker_lost`` registry spelling and the handoff reason."""
    factory, _session = _worker_lost_factory()
    writer = AsyncMock(return_value=True)
    rls = AsyncMock()

    with (
        patch.object(pe, "async_sessionmaker", MagicMock(return_value=factory)),
        patch.object(pe, "set_rls_org", rls),
        patch.object(ch, "persist_terminal_triage_marker", writer),
    ):
        await pe._persist_worker_lost_triage(
            MagicMock(),
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
        )

    rls.assert_awaited_once()
    writer.assert_awaited_once()
    kwargs = writer.await_args.kwargs
    assert kwargs["error_code"] == "harness.worker_lost"
    assert kwargs["reason"] == "executor lost (worker handoff)"
    assert kwargs["run_id"] == _RUN
    assert kwargs["organisation_id"] == _ORG


async def test_worker_lost_triage_helper_swallows_a_generic_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Triage is best-effort: a DB failure while writing the marker is logged
    and swallowed — it must never fail the terminal write that already
    committed."""
    factory, session = _worker_lost_factory()
    writer = AsyncMock(return_value=True)
    caplog.set_level(logging.WARNING, logger="modulo.core.pipeline_execution")

    with (
        patch.object(pe, "async_sessionmaker", MagicMock(return_value=factory)),
        patch.object(pe, "set_rls_org", AsyncMock(side_effect=RuntimeError("db down"))),
        patch.object(ch, "persist_terminal_triage_marker", writer),
    ):
        result = await pe._persist_worker_lost_triage(
            MagicMock(),
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
        )

    assert result is None
    writer.assert_not_awaited()
    # The failure is observable, not silently dropped.
    assert "pipeline_execution.worker_lost_triage_failed" in caplog.text
    assert session.begin.called


async def test_worker_lost_triage_helper_propagates_cancellation() -> None:
    """Cancellation is not a triage failure: it must propagate, never be
    swallowed as a best-effort miss."""
    factory, _session = _worker_lost_factory()

    with (
        patch.object(pe, "async_sessionmaker", MagicMock(return_value=factory)),
        patch.object(pe, "set_rls_org", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await pe._persist_worker_lost_triage(
            MagicMock(),
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
        )
