"""FAR-1693 / ADR-042: the unified governed-run provenance taxonomy.

The governed-run contract used to describe *how we know a fact* with two
vocabularies at once — the dispatch arm's implicit ``witnessed_via`` marker and
the agent-facts arm's ``observed`` / ``reported``. FAR-1693 unifies them into ONE
closed four-value taxonomy (``core.provenance``): ``observed`` / ``derived`` /
``reported`` / ``attested``, where ``witnessed_via`` is the CHANNEL STAMP of a
``reported`` datum rather than a class of its own.

This module is the conformance test the ticket asks for. It asserts two things
across the dispatch-origin surfaces:

1. **The taxonomy itself** — the classes are a closed set, ``witnessed_via`` is
   not a class, and dispatch data is ``reported``, never ``attested`` (the
   binding foreclosure: ``attested`` never implies the substrate's data is true).
2. **The render/persist criterion** — every claim-ready dispatch-origin surface
   carries the engine-derived class, and NO fact self-asserts its provenance: a
   hostile / prompt-injected substrate result cannot set its own class or
   channel, and a run payload derives its class from ``execution_origin`` rather
   than accepting one from the wire.

The surfaces pinned here are the ones ADR-042 names for dispatch-origin runs:
the run list / run detail payload builder, the MCP status read, the dashboard
recent-runs panel, and the run-keyed audit payloads (all composed from the ONE
shared serializer, ``core.run_provenance``).

No DB and no network: rows are in-memory ORM instances / stand-ins.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.mcp_server import _mcp_run_item, _run_status_base
from modulo.api.routes.dashboard import _load_recent_runs
from modulo.core.node_output_split import DISPATCH_PROVENANCE_FIELDS
from modulo.core.pipeline_engine.node_runner import _stamp_dispatch_provenance
from modulo.core.provenance import (
    DISPATCH_DATUM_PROVENANCE,
    PROVENANCE_ATTESTED,
    PROVENANCE_DERIVED,
    PROVENANCE_KEY,
    PROVENANCE_OBSERVED,
    PROVENANCE_REPORTED,
    PROVENANCE_VALUES,
    WITNESSED_VIA_KEY,
    execution_origin_provenance_class,
)
from modulo.core.run_provenance import run_provenance_fields
from modulo.db.models.run import EXECUTION_ORIGIN_DISPATCHED, Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. The taxonomy is one closed vocabulary
# ---------------------------------------------------------------------------


def test_the_taxonomy_is_the_closed_four_value_set() -> None:
    assert {
        PROVENANCE_OBSERVED,
        PROVENANCE_DERIVED,
        PROVENANCE_REPORTED,
        PROVENANCE_ATTESTED,
    } == PROVENANCE_VALUES


def test_witnessed_via_is_a_channel_stamp_not_a_class() -> None:
    """The unification's whole point: ``witnessed_via`` names HOW a ``reported``
    datum arrived, not a fifth class."""
    assert WITNESSED_VIA_KEY not in PROVENANCE_VALUES


def test_dispatch_data_is_reported_never_attested() -> None:
    """The binding foreclosure — substrate data is the substrate's REPORT, never
    an attestation that it is true."""
    assert DISPATCH_DATUM_PROVENANCE == PROVENANCE_REPORTED
    assert DISPATCH_DATUM_PROVENANCE != PROVENANCE_ATTESTED


def test_the_dispatch_field_list_carries_the_class_key() -> None:
    """The failed-artifact splitter must lift the class out of the stamped
    output, or a failed dispatch would reach failure inspection untyped."""
    assert PROVENANCE_KEY in DISPATCH_PROVENANCE_FIELDS


# ---------------------------------------------------------------------------
# 2. The run-level class is engine-derived from execution_origin
# ---------------------------------------------------------------------------


class TestExecutionOriginProvenanceClass:
    def test_dispatched_maps_to_reported(self) -> None:
        assert execution_origin_provenance_class(EXECUTION_ORIGIN_DISPATCHED) == PROVENANCE_REPORTED

    def test_executed_or_legacy_maps_to_untyped(self) -> None:
        """NULL is executed-by-Modulo OR a row recorded before provenance
        shipped — the honest class for it is None, never a guessed ``observed``
        (that would over-claim a legacy row)."""
        assert execution_origin_provenance_class(None) is None

    def test_an_unknown_origin_never_derives_a_class(self) -> None:
        assert execution_origin_provenance_class("something-else") is None

    def test_the_derived_class_is_always_in_the_closed_set(self) -> None:
        for origin in (EXECUTION_ORIGIN_DISPATCHED, None):
            derived = execution_origin_provenance_class(origin)
            if derived is not None:
                assert derived in PROVENANCE_VALUES


# ---------------------------------------------------------------------------
# 3. The dispatch stamper is engine-assigned — no fact self-asserts provenance
# ---------------------------------------------------------------------------


class TestDispatchStamperConformance:
    def test_a_clean_result_is_stamped_reported_with_the_channel(self) -> None:
        stamped = _stamp_dispatch_provenance({"status": "success", "id": "job-1"}, "inst-1")
        assert stamped[PROVENANCE_KEY] == PROVENANCE_REPORTED
        assert stamped[WITNESSED_VIA_KEY] == "inst-1"
        assert stamped["execution_identity"] == "customer_substrate"

    def test_a_hostile_result_cannot_assert_its_own_provenance(self) -> None:
        """A prompt-injected substrate result claiming ``attested`` and a
        different channel is OVERWRITTEN, never merged (ADR-034's rule)."""
        hostile = {
            "status": "success",
            PROVENANCE_KEY: PROVENANCE_ATTESTED,
            WITNESSED_VIA_KEY: "attacker",
            "execution_identity": "modulo",
        }
        stamped = _stamp_dispatch_provenance(hostile, "inst-1")
        assert stamped[PROVENANCE_KEY] == PROVENANCE_REPORTED
        assert stamped[WITNESSED_VIA_KEY] == "inst-1"
        assert stamped["execution_identity"] == "customer_substrate"

    def test_a_list_result_is_wrapped_and_typed(self) -> None:
        """``list_runs`` returns a bare list — it must NOT pass through unstamped
        and read as an array Modulo produced."""
        stamped = _stamp_dispatch_provenance([{"id": "job-1"}], "inst-1")
        assert stamped[PROVENANCE_KEY] == PROVENANCE_REPORTED
        assert stamped[WITNESSED_VIA_KEY] == "inst-1"
        assert stamped["result"] == [{"id": "job-1"}]

    def test_a_stamped_dispatch_datum_is_never_attested(self) -> None:
        stamped = _stamp_dispatch_provenance({"status": "failure"}, "inst-1")
        assert stamped[PROVENANCE_KEY] != PROVENANCE_ATTESTED


# ---------------------------------------------------------------------------
# 4. The dispatch-origin run surfaces carry the engine-derived class
# ---------------------------------------------------------------------------


def _run_row(**overrides: Any) -> Run:
    row = Run(
        id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        pipeline_id=uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        status="complete",
        trigger_type="manual",
        run_number=7,
        created_at=_NOW,
        started_at=_NOW,
        completed_at=_NOW,
        execution_origin=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _RecentRunsSession(AsyncSession):
    """A real ``AsyncSession`` subclass capturing the SELECT (no engine, no DB)."""

    def __init__(self, rows: list[Any]) -> None:
        super().__init__()
        self._rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statements.append(statement)
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


class TestRunListSurface:
    def test_dispatched_run_payload_is_typed_reported(self) -> None:
        assert run_provenance_fields(_run_row(execution_origin="dispatched"))[PROVENANCE_KEY] == PROVENANCE_REPORTED

    def test_executed_run_payload_is_untyped(self) -> None:
        assert run_provenance_fields(_run_row(execution_origin=None))[PROVENANCE_KEY] is None

    def test_the_class_rides_alongside_the_origin(self) -> None:
        fields = run_provenance_fields(_run_row(execution_origin="dispatched"))
        assert fields["execution_origin"] == "dispatched"
        assert fields["provenance"] == PROVENANCE_REPORTED


class TestMcpStatusSurface:
    def test_dispatched_run_status_is_typed_reported(self) -> None:
        assert _run_status_base(_run_row(execution_origin="dispatched"))[PROVENANCE_KEY] == PROVENANCE_REPORTED

    def test_executed_run_status_is_untyped(self) -> None:
        assert _run_status_base(_run_row(execution_origin=None))[PROVENANCE_KEY] is None

    def test_dispatched_run_list_item_is_typed_reported(self) -> None:
        item = _mcp_run_item(_run_row(execution_origin="dispatched"), {})
        assert item[PROVENANCE_KEY] == PROVENANCE_REPORTED


class TestDashboardRecentRunsSurface:
    async def test_dispatched_row_is_typed_reported(self) -> None:
        session = _RecentRunsSession([_recent_run_row("dispatched")])
        rows = await _load_recent_runs(session, _ORG)
        assert rows[0][PROVENANCE_KEY] == PROVENANCE_REPORTED

    async def test_executed_row_is_untyped_and_the_key_is_never_omitted(self) -> None:
        session = _RecentRunsSession([_recent_run_row(None)])
        rows = await _load_recent_runs(session, _ORG)
        assert rows[0][PROVENANCE_KEY] is None
        assert PROVENANCE_KEY in rows[0]
