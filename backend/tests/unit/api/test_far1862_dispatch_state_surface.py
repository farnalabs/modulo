"""FAR-1862 (Deliverable A): the dispatch-state columns are exposed on the run read surfaces.

The reconciler's recovery decisions are computed from ``dispatcher``,
``dispatched_at``, ``dispatch_phase``, ``claim_count`` and ``enqueue_failed_at``.
The 2026-10-16 incident could not be diagnosed from the product because NONE of
these was exposed by any read surface. These tests pin the additive exposure on:

* the REST runs-list item builder (``_build_list_item``),
* the REST run-detail response model (``RunResponse`` / ``_build_run_response``),
* the MCP ``get_run_status`` base payload (``_run_status_base``).

They are not credentials — the values are passed through unmasked — and a
stand-in carrying a non-column value degrades to ``None`` rather than failing
response validation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from modulo.api.mcp_server import _run_status_base
from modulo.api.routes.runs import (
    RunResponse,
    _build_list_item,
    _build_run_response,
    _ListPageContext,
)
from modulo.db.models.run import Run

_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _run_row(**overrides: object) -> Run:
    """A real (transient) ``Run`` row, no DB."""
    row = Run(
        id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        pipeline_id=uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        status="pending",
        trigger_type="manual",
        run_number=7,
        langgraph_thread_id="thread-far1862",
        created_at=_NOW,
        dispatcher="saq",
        dispatched_at=_NOW,
        dispatch_phase="claimed",
        claim_count=3,
        enqueue_failed_at=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _ctx() -> _ListPageContext:
    return _ListPageContext(
        child_rollup={},
        account_labels={},
        trigger_labels={},
        active_count=0,
        concurrency_limit=None,
    )


class TestRestListSurface:
    def test_list_item_carries_dispatch_state(self) -> None:
        item = _build_list_item(_run_row(), _ctx())
        assert item["dispatcher"] == "saq"
        assert item["dispatched_at"] == _NOW.isoformat()
        assert item["dispatch_phase"] == "claimed"
        assert item["claim_count"] == 3
        assert item["enqueue_failed_at"] is None

    def test_list_item_nulls_when_unset(self) -> None:
        item = _build_list_item(
            _run_row(dispatcher=None, dispatched_at=None, dispatch_phase=None, claim_count=0, enqueue_failed_at=None),
            _ctx(),
        )
        assert item["dispatcher"] is None
        assert item["dispatched_at"] is None
        assert item["dispatch_phase"] is None
        assert item["claim_count"] == 0
        assert item["enqueue_failed_at"] is None

    def test_list_item_stand_in_without_the_columns_degrades_to_none(self) -> None:
        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "pending"
        run.pipeline_id = uuid.uuid4()
        run.pipeline = None
        run.run_number = 1
        item = _build_list_item(run, _ctx())
        assert item["dispatcher"] is None
        assert item["dispatched_at"] is None
        assert item["dispatch_phase"] is None
        assert item["claim_count"] is None
        assert item["enqueue_failed_at"] is None


class TestRestDetailSurface:
    def test_response_model_populates_dispatch_state(self) -> None:
        resp = _build_run_response(_run_row())
        assert isinstance(resp, RunResponse)
        assert resp.dispatcher == "saq"
        assert resp.dispatched_at == _NOW
        assert resp.dispatch_phase == "claimed"
        assert resp.claim_count == 3
        assert resp.enqueue_failed_at is None

    def test_response_model_nulls_when_unset(self) -> None:
        resp = _build_run_response(_run_row(dispatcher=None, dispatched_at=None, dispatch_phase=None, claim_count=0))
        assert resp.dispatcher is None
        assert resp.dispatched_at is None
        assert resp.dispatch_phase is None
        assert resp.claim_count == 0


class TestMcpBaseSurface:
    def test_base_payload_carries_dispatch_state(self) -> None:
        base = _run_status_base(_run_row())
        assert base["dispatcher"] == "saq"
        assert base["dispatched_at"] == _NOW.isoformat()
        assert base["dispatch_phase"] == "claimed"
        assert base["claim_count"] == 3
        assert base["enqueue_failed_at"] is None

    def test_base_payload_nulls_when_unset(self) -> None:
        base = _run_status_base(
            _run_row(dispatcher=None, dispatched_at=None, dispatch_phase=None, claim_count=0, enqueue_failed_at=None)
        )
        assert base["dispatcher"] is None
        assert base["dispatched_at"] is None
        assert base["dispatch_phase"] is None
        assert base["claim_count"] == 0
        assert base["enqueue_failed_at"] is None

    def test_base_payload_is_present_without_detail(self) -> None:
        """The fields ride the BASE response, so a plain ``get_run_status`` can
        diagnose a stuck run without requesting detail."""
        base = _run_status_base(_run_row())
        assert {"dispatcher", "dispatched_at", "dispatch_phase", "claim_count", "enqueue_failed_at"} <= set(base)
