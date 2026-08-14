"""Unit tests for the ``node_telemetry_json`` CRUD plumbing (FAR-125 P1a).

Verifies the Agent Return Contract split at the persistence layer: the
run-status and run-outputs writers persist the split-out per-node telemetry
column atomically alongside ``outputs_json`` — a single flush on the same ORM
object, never a torn half-state — and that ``node_telemetry_json`` defaults to
None (untouched) when a caller does not pass it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.run import update_run_outputs, update_run_status


def _run(**attrs: object) -> SimpleNamespace:
    """A minimal stand-in for a SQLAlchemy ``Run`` row.

    ``update_run_status`` / ``update_run_outputs`` only mutate attributes on
    the object returned by ``scalar_one_or_none()`` and flush the session, so a
    plain mutable namespace exercises the real write path. The FAR-189
    classification hook fires on terminal status writes and reads the run's
    classification inputs (``error_code``, ``raw_output_markers``,
    ``work_intact``) plus ``id``, so the stand-in carries them too.
    """
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "error_code": None,
        "outputs_json": None,
        "node_telemetry_json": None,
        "raw_output_markers": None,
        "work_intact": None,
    }
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


def _session_returning(run: object) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = run
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# update_run_status — atomic write of outputs_json + node_telemetry_json
# ---------------------------------------------------------------------------


async def test_update_run_status_writes_both_columns_atomically() -> None:
    run = _run()
    session = _session_returning(run)
    telemetry = {"status": "completed", "wall_clock_time_ms": 1200, "exit_code": 0}

    result = await update_run_status(
        session,
        uuid.uuid4(),
        "complete",
        outputs_json={"n1": {"answer": 42}},
        node_telemetry_json={"n1": telemetry},
    )

    assert result is run
    assert run.outputs_json == {"n1": {"answer": 42}}
    assert run.node_telemetry_json == {"n1": telemetry}
    # One flush on the same ORM object — both columns persist in a single
    # atomic write, never a torn half-state.
    session.flush.assert_awaited_once()


async def test_update_run_status_telemetry_defaults_to_none_when_not_passed() -> None:
    run = _run()
    session = _session_returning(run)

    result = await update_run_status(
        session,
        uuid.uuid4(),
        "complete",
        outputs_json={"n1": {"answer": 42}},
    )

    assert result is run
    assert run.outputs_json == {"n1": {"answer": 42}}
    assert run.node_telemetry_json is None
    session.flush.assert_awaited_once()


async def test_update_run_status_persists_telemetry_without_outputs() -> None:
    """Telemetry alone (no outputs_json) still lands on the ORM object."""
    run = _run()
    session = _session_returning(run)

    result = await update_run_status(
        session,
        uuid.uuid4(),
        "complete",
        node_telemetry_json={"n1": {"status": "completed"}},
    )

    assert result is run
    assert run.outputs_json is None
    assert run.node_telemetry_json == {"n1": {"status": "completed"}}
    session.flush.assert_awaited_once()


async def test_update_run_status_missing_run_returns_none() -> None:
    session = _session_returning(None)

    result = await update_run_status(
        session,
        uuid.uuid4(),
        "complete",
        outputs_json={"n1": {"answer": 42}},
        node_telemetry_json={"n1": {"status": "completed"}},
    )

    assert result is None
    session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_run_outputs — split-correct for future callers
# ---------------------------------------------------------------------------


async def test_update_run_outputs_writes_telemetry_with_outputs() -> None:
    run = _run()
    session = _session_returning(run)
    telemetry = {"n1": {"status": "completed", "wall_clock_time_ms": 900}}

    result = await update_run_outputs(
        session,
        uuid.uuid4(),
        {"n1": {"answer": 7}},
        node_telemetry_json=telemetry,
    )

    assert result is run
    assert run.outputs_json == {"n1": {"answer": 7}}
    assert run.node_telemetry_json == telemetry
    session.flush.assert_awaited_once()


async def test_update_run_outputs_telemetry_defaults_to_none_when_not_passed() -> None:
    run = _run()
    session = _session_returning(run)

    result = await update_run_outputs(session, uuid.uuid4(), {"n1": {"answer": 7}})

    assert result is run
    assert run.outputs_json == {"n1": {"answer": 7}}
    assert run.node_telemetry_json is None
    session.flush.assert_awaited_once()


async def test_update_run_outputs_missing_run_returns_none() -> None:
    session = _session_returning(None)

    result = await update_run_outputs(
        session,
        uuid.uuid4(),
        {"n1": {"answer": 7}},
        node_telemetry_json={"n1": {"status": "completed"}},
    )

    assert result is None
    session.flush.assert_not_awaited()
