"""FAR-1141 / ADR-042: `execution_origin` on the remaining claim-ready run surfaces.

ADR-042's conformance criterion: **any claim-ready surface for a dispatched run
must be provenance-typed, so a dispatched run never reads indistinguishably from
an executed one.** The runs list + run detail + frontend badges already carried
it; this module pins the surfaces that did not:

* MCP ``list_runs`` (``_mcp_run_item``) and ``get_run_status``
  (``_run_status_base``) — the agent-facing read surfaces.
* the dashboard summary's recent-runs panel (``_load_recent_runs``) — including
  the fact that the column is really SELECTed, not merely echoed back.
* ``/viewmodel/current``'s ``recent_runs`` (``RunSummary``).
* the Slack app_mention 202 ack (covered in ``test_slack_trigger_endpoint``).

Every surface is asserted on BOTH sides of the conformance line — a dispatched
run reads ``"dispatched"``, a Modulo-executed / pre-column run reads ``None`` —
and every ``getattr``-based read is additionally proven MagicMock-safe: a
``MagicMock`` run stand-in whose unset attribute resolves to a mock must degrade
to ``None`` (origin not recorded), never a repr, because these payloads are
serialized verbatim.

No DB and no network: sessions and run rows are in-memory stand-ins.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from modulo.api.mcp_server import _mcp_run_item, _run_status_base
from modulo.api.routes.dashboard import _load_recent_runs
from modulo.api.routes.viewmodel import RunSummary

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _run_row(**overrides: Any) -> SimpleNamespace:
    """A fully-populated run stand-in for the MCP read surfaces."""
    row = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        pipeline_id=uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        status="complete",
        trigger_type="manual",
        run_number=7,
        created_at=_NOW,
        started_at=_NOW,
        completed_at=_NOW,
        error_code=None,
        error_detail=None,
        total_cost_usd=None,
        execution_origin=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


# ---------------------------------------------------------------------------
# MCP list_runs — _mcp_run_item
# ---------------------------------------------------------------------------


class TestMcpRunItemExecutionOrigin:
    def test_dispatched_run_reads_dispatched(self) -> None:
        item = _mcp_run_item(_run_row(execution_origin="dispatched"), {})
        assert item["execution_origin"] == "dispatched"

    def test_executed_run_reads_null(self) -> None:
        item = _mcp_run_item(_run_row(execution_origin=None), {})
        assert item["execution_origin"] is None

    def test_mock_run_stand_in_degrades_to_null_not_a_repr(self) -> None:
        """A MagicMock whose unset attribute resolves to a mock must never leak
        a repr into the JSON tool result (the runs REST item degrades the same
        way — this is the shared ``_optional_str`` contract)."""
        run = MagicMock()
        run.id = uuid.uuid4()
        run.pipeline_id = uuid.uuid4()
        run.status = "complete"
        run.trigger_type = "manual"
        run.run_number = 1
        run.created_at = _NOW
        run.started_at = None
        run.completed_at = None
        run.error_code = None
        run.error_detail = None
        run.total_cost_usd = None
        # execution_origin deliberately left unset -> a MagicMock attribute.
        item = _mcp_run_item(run, {})
        assert item["execution_origin"] is None

    def test_run_without_the_column_at_all_degrades_to_null(self) -> None:
        """A pre-column / partially-loaded stand-in (no attribute at all) reads
        NULL rather than raising — provenance must never break the read."""
        row = _run_row()
        del row.execution_origin  # type: ignore[attr-defined]
        item = _mcp_run_item(row, {})
        assert item["execution_origin"] is None


# ---------------------------------------------------------------------------
# MCP get_run_status — _run_status_base
# ---------------------------------------------------------------------------


class TestMcpRunStatusBaseExecutionOrigin:
    def test_dispatched_run_reads_dispatched(self) -> None:
        base = _run_status_base(_run_row(execution_origin="dispatched"))  # type: ignore[arg-type]
        assert base["execution_origin"] == "dispatched"

    def test_executed_run_reads_null(self) -> None:
        base = _run_status_base(_run_row(execution_origin=None))  # type: ignore[arg-type]
        assert base["execution_origin"] is None

    def test_run_without_the_column_degrades_to_null(self) -> None:
        row = _run_row()
        del row.execution_origin  # type: ignore[attr-defined]
        assert _run_status_base(row)["execution_origin"] is None  # type: ignore[arg-type]

    def test_the_field_sits_alongside_trigger_type(self) -> None:
        """The conformance shape: origin is a first-class sibling of the other
        run-identity keys, not buried in a nested object."""
        base = _run_status_base(_run_row(execution_origin="dispatched"))  # type: ignore[arg-type]
        assert {"run_id", "status", "trigger_type", "execution_origin"} <= set(base)


# ---------------------------------------------------------------------------
# Dashboard — _load_recent_runs
# ---------------------------------------------------------------------------


class _RecentRunsSession:
    """Session stand-in capturing the SELECT so the test can prove the column is
    really projected (not just mapped from an unrelated row attribute)."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.statements: list[Any] = []

    async def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statements.append(stmt)
        return SimpleNamespace(all=lambda: self._rows)


def _recent_run_row(execution_origin: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
        run_number=3,
        pipeline_name="PR Reviewer Agent",
        status="complete",
        created_at=_NOW,
        trigger_type="cron",
        execution_origin=execution_origin,
    )


class TestDashboardRecentRunsExecutionOrigin:
    async def test_dispatched_run_reads_dispatched(self) -> None:
        session = _RecentRunsSession([_recent_run_row("dispatched")])
        rows = await _load_recent_runs(session, _ORG)  # type: ignore[arg-type]
        assert rows[0]["execution_origin"] == "dispatched"

    async def test_executed_run_reads_null_and_the_key_is_never_omitted(self) -> None:
        session = _RecentRunsSession([_recent_run_row(None)])
        rows = await _load_recent_runs(session, _ORG)  # type: ignore[arg-type]
        assert rows[0]["execution_origin"] is None
        assert "execution_origin" in rows[0]

    async def test_the_column_is_actually_selected(self) -> None:
        """Mapping the attribute back is not enough — the projection must carry
        the column or every row would read NULL regardless of the DB value."""
        session = _RecentRunsSession([])
        await _load_recent_runs(session, _ORG)  # type: ignore[arg-type]
        assert len(session.statements) == 1
        assert "execution_origin" in str(session.statements[0])


# ---------------------------------------------------------------------------
# Viewmodel — RunSummary
# ---------------------------------------------------------------------------


class TestRunSummaryExecutionOrigin:
    def test_dispatched_run_reads_dispatched(self) -> None:
        summary = RunSummary.model_validate(_run_row(execution_origin="dispatched"))
        assert summary.execution_origin == "dispatched"

    def test_executed_run_reads_null(self) -> None:
        summary = RunSummary.model_validate(_run_row(execution_origin=None))
        assert summary.execution_origin is None

    def test_field_is_nullable_and_optional(self) -> None:
        """The column shipped in migration 0288 — a payload without the key must
        still validate (additive/nullable, matching the REST list item)."""
        summary = RunSummary.model_validate(
            {
                "id": uuid.uuid4(),
                "pipeline_id": uuid.uuid4(),
                "status": "complete",
                "trigger_type": "manual",
                "created_at": _NOW,
            }
        )
        assert summary.execution_origin is None

    def test_mock_run_stand_in_degrades_to_null(self) -> None:
        """``/viewmodel/current`` is exercised with MagicMock run stand-ins in
        ``test_viewmodel_endpoint`` — the ``before`` validator must coerce the
        mock attribute to ``None`` instead of failing response validation."""
        run = MagicMock()
        run.id = uuid.uuid4()
        run.pipeline_id = uuid.uuid4()
        run.status = "complete"
        run.trigger_type = "manual"
        run.created_at = _NOW
        summary = RunSummary.model_validate(run)
        assert summary.execution_origin is None

    def test_non_string_origin_is_rejected_to_null(self) -> None:
        """An int/enum/whatever from a half-typed stand-in degrades to NULL —
        the wire type stays ``str | None``."""
        summary = RunSummary.model_validate(_run_row(execution_origin=12345))  # type: ignore[arg-type]
        assert summary.execution_origin is None
