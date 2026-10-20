"""FAR-1886 B1 — terminal-path triage evidence and the triage envelope stats.

``deadline_triage`` used to exist only for ``node.deadline_exceeded``. The
age-gate ``run.no_progress`` and ``harness.worker_lost`` terminalisations
returned ``[]``, so "executor died" and "silent stall" were indistinguishable
on the run page. These tests pin the three parts of the fix:

* the projection carries ``last_output_age_seconds`` + ``attempt_count`` on
  every entry (including synthetic run-level entries);
* ``persist_terminal_triage_marker`` writes the synthetic entry with a
  row-derived reason (``no attempts`` / ``executor lost``) and is best-effort;
* ``fail_run_terminal`` persists that entry for ``harness.worker_lost`` only.

The age gate's wiring is covered in
``tests/unit/cron_helpers/test_dispatcher_reconcile.py``. No DB, no network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.routes import runs as runs_module
from modulo.core import cron_helpers as ch
from modulo.core import pipeline_execution as pe

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN = uuid.UUID("00000000-0000-0000-0000-000000000002")
_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake sessions / results
# ---------------------------------------------------------------------------


class _Nested:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _Session:
    """Minimal session double for the synthetic-marker writer."""

    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, model: Any, pk: Any) -> Any:
        return self._run

    def begin_nested(self) -> _Nested:
        return _Nested()


class _Result:
    def __init__(self, *, scalar: Any = None, row: Any = None) -> None:
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._row


class _StatsSession:
    """Session double returning the two stats reads in call order."""

    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)

    async def execute(self, stmt: Any) -> _Result:
        return self._results.pop(0)


# ---------------------------------------------------------------------------
# Projection: the new envelope fields
# ---------------------------------------------------------------------------


def test_projection_adds_last_output_age_and_attempt_count() -> None:
    markers = {
        f"terminal-triage:{_RUN}": {
            "deadline_triage": True,
            "node_id": None,
            "no_output_reason": "executor lost",
            "raw_output": "",
            "sandbox_log_tail": "",
            "stdout_length": 0,
            "stderr_length": 0,
        }
    }
    entries = runs_module._deadline_triage_from_markers(
        markers,
        last_output_age_seconds=120.0,
        attempt_count=2,
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry["no_output_reason"] == "executor lost"
    assert entry["last_output_age_seconds"] == 120.0
    assert entry["attempt_count"] == 2
    # Same envelope keys as a per-attempt deadline marker.
    assert entry["attempt_key"] == f"terminal-triage:{_RUN}"
    assert not entry["raw_output_tail"]
    assert entry["stdout_length"] == 0


def test_projection_defaults_stats_to_none_for_the_mcp_path() -> None:
    """The MCP surface projects the same helper without DB stats — the keys
    still exist so the two surfaces keep one shape."""
    markers = {"k": {"deadline_triage": True, "node_id": "n1"}}
    entry = runs_module._deadline_triage_from_markers(markers)[0]
    assert entry["last_output_age_seconds"] is None
    assert entry["attempt_count"] is None


# ---------------------------------------------------------------------------
# Read-time stats derivation
# ---------------------------------------------------------------------------


async def test_output_stats_age_is_measured_from_the_last_output_row() -> None:
    session = _StatsSession(
        [
            _Result(scalar=_T0),
            _Result(row=SimpleNamespace(started_at=_T0, completed_at=_T1, node_attempt_count=3)),
        ]
    )
    age, attempts = await runs_module._deadline_triage_output_stats(session, _RUN)  # type: ignore[arg-type]
    assert age == 300.0
    assert attempts == 3


async def test_output_stats_falls_back_to_started_at_when_no_output_exists() -> None:
    session = _StatsSession(
        [
            _Result(scalar=None),
            _Result(row=SimpleNamespace(started_at=_T0, completed_at=_T1, node_attempt_count=0)),
        ]
    )
    age, attempts = await runs_module._deadline_triage_output_stats(session, _RUN)  # type: ignore[arg-type]
    assert age == 300.0
    assert attempts == 0


async def test_output_stats_degrades_to_none_on_a_read_error() -> None:
    class _Boom:
        async def execute(self, stmt: Any) -> Any:
            raise RuntimeError("db down")

    age, attempts = await runs_module._deadline_triage_output_stats(_Boom(), _RUN)  # type: ignore[arg-type]
    assert age is None
    assert attempts is None


# ---------------------------------------------------------------------------
# Synthetic marker writer
# ---------------------------------------------------------------------------


async def test_writer_marks_executor_lost_when_attempts_exist() -> None:
    session = _Session(SimpleNamespace(node_attempt_count=1))
    write = AsyncMock()
    with (
        patch(
            "modulo.db.crud.run_node_outputs.read_run_markers",
            AsyncMock(return_value={"prior": {"deadline_triage": True}}),
        ),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", write),
    ):
        assert await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="no_progress",
        )

    assert write.await_count == 1
    markers = write.await_args.kwargs["markers"]
    key = f"terminal-triage:{_RUN}"
    # Existing evidence is preserved; the synthetic entry is additive.
    assert "prior" in markers
    entry = markers[key]
    assert entry["deadline_triage"] is True
    assert entry["no_output_reason"] == "executor lost"
    assert entry["attempt_key"] == key
    assert entry["node_id"] is None


async def test_writer_states_no_attempts_when_the_row_has_none() -> None:
    session = _Session(SimpleNamespace(node_attempt_count=0))
    write = AsyncMock()
    with (
        patch("modulo.db.crud.run_node_outputs.read_run_markers", AsyncMock(return_value=None)),
        patch("modulo.db.crud.run_node_outputs.write_run_markers", write),
    ):
        assert await ch.persist_terminal_triage_marker(
            session,  # type: ignore[arg-type]
            run_id=_RUN,
            organisation_id=_ORG,
            error_code="harness.worker_lost",
        )

    entry = write.await_args.kwargs["markers"][f"terminal-triage:{_RUN}"]
    assert entry["no_output_reason"] == "no attempts"


async def test_writer_is_best_effort_and_never_raises() -> None:
    session = _Session(SimpleNamespace(node_attempt_count=2))
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
        )


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


async def test_worker_lost_triage_helper_uses_the_truthful_code() -> None:
    """The helper opens its own RLS-scoped session and delegates with the
    exact ``harness.worker_lost`` registry spelling."""
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
    factory = MagicMock(return_value=factory_cm)
    writer = AsyncMock(return_value=True)

    with (
        patch.object(pe, "async_sessionmaker", MagicMock(return_value=factory)),
        patch.object(pe, "set_rls_org", AsyncMock()),
        patch.object(ch, "persist_terminal_triage_marker", writer),
    ):
        await pe._persist_worker_lost_triage(
            MagicMock(),
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
        )

    writer.assert_awaited_once()
    kwargs = writer.await_args.kwargs
    assert kwargs["error_code"] == "harness.worker_lost"
    assert kwargs["run_id"] == _RUN
    assert kwargs["organisation_id"] == _ORG
