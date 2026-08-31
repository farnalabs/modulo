"""Unit tests for RunResponse timestamp serialization.

Run Detail UI feedback: the run detail page showed ``Created: —`` /
``Started: —`` / ``Completed: —`` because ``RunResponse`` did not carry
``created_at`` / ``started_at`` / ``completed_at`` even though the ``Run`` ORM
entity has those columns. These tests prove ``_build_run_response`` now
populates the three timestamps when present and returns ``None`` when absent.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from modulo.api.routes.runs import (
    _build_list_item,
    _build_run_response,
    _ListPageContext,
)

_RUN_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()


def _make_run(**overrides: object) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.status = "complete"
    run.pipeline_id = _PIPELINE_ID
    run.pipeline = None
    run.run_number = 1
    run.langgraph_thread_id = "thread-1"
    run.snapshot_id = _SNAPSHOT_ID
    run.error_detail = None
    run.error_code = None
    run.total_cost_usd = None
    run.total_tokens = None
    run.node_token_usage = None
    run.cost_breakdown = None
    run.trigger_type = None
    run.trigger_id = None
    run.account_id = None
    run.heartbeat_at = None
    run.work_item_refs = None
    run.created_at = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    run.started_at = datetime(2026, 8, 1, 10, 1, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 8, 1, 10, 5, 30, tzinfo=UTC)
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def test_run_response_populates_timestamps_when_present() -> None:
    resp = _build_run_response(_make_run())

    assert resp.created_at == datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    assert resp.started_at == datetime(2026, 8, 1, 10, 1, 0, tzinfo=UTC)
    assert resp.completed_at == datetime(2026, 8, 1, 10, 5, 30, tzinfo=UTC)


def test_run_response_timestamps_none_when_absent() -> None:
    resp = _build_run_response(_make_run(created_at=None, started_at=None, completed_at=None))

    assert resp.created_at is None
    assert resp.started_at is None
    assert resp.completed_at is None


def test_run_response_carries_snapshot_id_when_present() -> None:
    resp = _build_run_response(_make_run())

    assert resp.snapshot_id == _SNAPSHOT_ID


def test_run_response_snapshot_id_none_when_absent() -> None:
    resp = _build_run_response(_make_run(snapshot_id=None))

    assert resp.snapshot_id is None


def test_list_item_carries_snapshot_id() -> None:
    ctx = _ListPageContext(
        child_rollup={},
        account_labels={},
        trigger_labels={},
        active_count=0,
        concurrency_limit=None,
    )
    item = _build_list_item(_make_run(input_payload=None), ctx)

    assert item["snapshot_id"] == str(_SNAPSHOT_ID)


def test_list_item_snapshot_id_none_when_absent() -> None:
    ctx = _ListPageContext(
        child_rollup={},
        account_labels={},
        trigger_labels={},
        active_count=0,
        concurrency_limit=None,
    )
    item = _build_list_item(_make_run(snapshot_id=None, input_payload=None), ctx)

    assert item["snapshot_id"] is None
