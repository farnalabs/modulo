"""FAR-1088 (W3): the claimed-but-nodeless terminal record names the phase.

``_fail_nodeless_run`` reads ``runs.dispatch_phase`` and
``runs.dispatch_phase_entered_at`` off the already-loaded run row and renders
them into the terminal ``error_detail``, the alert-grade ERROR log, and the
ingested error context — so a reader can see WHERE the claim stalled without
forensics. A NULL phase must surface as ``dispatch_phase=unknown``, never be
silently omitted.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core import cron_helpers as ch

_PHASE_ELAPSED_RE = re.compile(r"dispatch_phase_elapsed=([0-9]+\.[0-9])s")


def _make_run(**overrides: Any) -> SimpleNamespace:
    """A running, claimed-but-nodeless run row (the terminaliser's input)."""
    fields: dict[str, Any] = {
        "status": "running",
        "pipeline_id": uuid.uuid4(),
        "trigger_id": uuid.uuid4(),
        "claim_count": 3,
        "started_at": datetime.now(UTC) - timedelta(minutes=40),
        "dispatched_at": datetime.now(UTC) - timedelta(minutes=45),
        "dispatch_phase": None,
        "dispatch_phase_entered_at": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _SessionWithRun:
    """Minimal async session: ``session.get(Run, pk)`` returns the fixture row."""

    def __init__(self, run: SimpleNamespace) -> None:
        self._run = run

    async def get(self, model: Any, pk: Any) -> Any:
        return self._run


async def _terminal_fail(run: SimpleNamespace) -> tuple[dict[str, Any], AsyncMock]:
    """Run the chokepoint and return (tick summary, patched error ingest)."""
    summary = ch._dispatcher_summary()
    ingest = AsyncMock()
    with patch.object(ch, "_ingest_saq_error", ingest):
        await ch._fail_nodeless_run(_SessionWithRun(run), uuid.uuid4(), uuid.uuid4(), summary)
    return summary, ingest


def _elapsed_from(detail: str) -> float:
    match = _PHASE_ELAPSED_RE.search(detail)
    assert match is not None, f"no dispatch_phase_elapsed in detail: {detail!r}"
    return float(match.group(1))


async def test_phased_run_appends_phase_and_elapsed_to_terminal_detail() -> None:
    """A run that recorded a phase names it, with the seconds spent in it."""
    entered_at = datetime.now(UTC) - timedelta(seconds=30)
    run = _make_run(dispatch_phase="loading_setup", dispatch_phase_entered_at=entered_at)
    summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=loading_setup dispatch_phase_elapsed=" in detail
    assert summary["claimed_but_never_dispatched"] == 1
    assert run.status == "failed"


async def test_null_phase_renders_as_unknown_not_omitted() -> None:
    """No recorded phase is explicit: ``dispatch_phase=unknown``, and no
    elapsed token is invented for a NULL ``dispatch_phase_entered_at``."""
    run = _make_run()
    summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=unknown" in detail
    assert "dispatch_phase_elapsed" not in detail
    assert summary["claimed_but_never_dispatched"] == 1


async def test_elapsed_is_seconds_since_phase_entered() -> None:
    """The elapsed value is measured against the terminal ``completed_at``
    (here: 120s after phase entry), not a constant."""
    entered_at = datetime.now(UTC) - timedelta(seconds=120)
    run = _make_run(dispatch_phase="streaming", dispatch_phase_entered_at=entered_at)
    _summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=streaming dispatch_phase_elapsed=" in detail
    elapsed = _elapsed_from(detail)
    assert 120.0 <= elapsed <= 123.0
    # The rendered value is derived from the row's own completed_at.
    recomputed = (run.completed_at - entered_at).total_seconds()
    assert abs(recomputed - elapsed) < 0.2


async def test_naive_phase_timestamp_is_treated_as_utc() -> None:
    """A naive ``dispatch_phase_entered_at`` must not raise; it is assumed UTC."""
    naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    run = _make_run(dispatch_phase="loading_setup", dispatch_phase_entered_at=naive)
    summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=loading_setup dispatch_phase_elapsed=" in detail
    assert summary["claimed_but_never_dispatched"] == 1


async def test_phase_carried_in_error_log_and_ingested_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The phase rides alongside pipeline/trigger in the ERROR log line and
    the Error Dashboard context, not just the error_detail string."""
    entered_at = datetime.now(UTC) - timedelta(seconds=12)
    run = _make_run(dispatch_phase="streaming", dispatch_phase_entered_at=entered_at)
    with caplog.at_level(logging.ERROR):
        _summary, ingest = await _terminal_fail(run)

    context = ingest.await_args.kwargs["context"]
    assert context["dispatch_phase"] == "streaming"
    assert 12.0 <= context["dispatch_phase_elapsed"] <= 15.0

    messages = [record.getMessage() for record in caplog.records]
    assert any("dispatch_phase=streaming" in message for message in messages)
