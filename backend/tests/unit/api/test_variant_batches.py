"""Unit tests for variant batch API routes — pure function tests (no DB, no auth)."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.api.routes.variant_batches import (
    _compute_batch_status,
    _run_to_variant_run,
)
from tests.unit.api.mock_session import configure_mock_session


def make_session_mock() -> AsyncMock:
    """Create an AsyncSession mock that supports async with session.begin()."""
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=session)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    return session


def make_mock_principal(**kwargs: object) -> MagicMock:
    p = MagicMock()
    p.organisation_id = kwargs.get("org_id", uuid.uuid4())
    p.account_id = kwargs.get("user_id", uuid.uuid4())
    p.username = kwargs.get("username", "test_user")
    p.org_role = kwargs.get("org_role", "admin")
    return p


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    status: str = "complete",
    pipeline_id: uuid.UUID | None = None,
    variant_config_snapshot: dict[str, Any] | None = None,
    total_cost_usd: float | None = 0.01,
    total_tokens: int | None = 1000,
    created_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.status = status
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.variant_config_snapshot = variant_config_snapshot or {}
    run.total_cost_usd = total_cost_usd
    run.total_tokens = total_tokens
    run.created_at = created_at or datetime.now(UTC)
    run.completed_at = completed_at
    run._eval_results = []
    return run


class TestComputeBatchStatus:
    def test_empty_returns_pending(self) -> None:
        assert _compute_batch_status([]) == "pending"

    def test_all_complete(self) -> None:
        assert _compute_batch_status(["complete", "complete"]) == "complete"

    def test_partial_with_mix(self) -> None:
        assert _compute_batch_status(["complete", "running"]) == "partial"

    def test_all_pending(self) -> None:
        assert _compute_batch_status(["pending", "pending"]) == "pending"

    def test_running(self) -> None:
        assert _compute_batch_status(["running", "pending"]) == "running"

    def test_failed(self) -> None:
        assert _compute_batch_status(["complete", "failed"]) == "failed"

    def test_cancelled(self) -> None:
        assert _compute_batch_status(["cancelled", "cancelled"]) == "cancelled"

    def test_eval_failed_counts_as_failed(self) -> None:
        assert _compute_batch_status(["complete", "eval_failed"]) == "failed"


class TestRunToVariantRun:
    def test_maps_complete_run(self) -> None:
        run = _make_run(
            status="complete",
            variant_config_snapshot={
                "variant_name": "control",
                "snapshot_id": "snap-123",
                "run_context_overrides": {"temperature": 0.7},
            },
            total_cost_usd=0.05,
            total_tokens=5000,
        )
        result = _run_to_variant_run(
            run,
            eval_stats={run.id: (10, 8)},
            node_outputs={"agent1": {"text": "hello"}},
        )
        assert result["run_id"] == str(run.id)
        assert result["variant_name"] == "control"
        assert result["snapshot_label"] == "snap-123"
        assert result["run_status"] == "complete"
        assert result["pass_rate"] == pytest.approx(0.8)
        assert result["total_cost_usd"] == pytest.approx(0.05)
        assert result["total_tokens"] == 5000
        assert result["node_outputs"] == {"agent1": {"text": "hello"}}

    def test_maps_pending_run_no_evals(self) -> None:
        run = _make_run(
            status="pending",
            variant_config_snapshot={"variant_name": "treatment"},
        )
        result = _run_to_variant_run(
            run,
            eval_stats={},
            node_outputs=None,
        )
        assert result["run_status"] == "pending"
        assert result["pass_rate"] is None
        assert result["node_outputs"] is None

    def test_unknown_variant_name_defaults_to_unknown(self) -> None:
        run = _make_run(
            status="running",
            variant_config_snapshot={},
        )
        result = _run_to_variant_run(run, eval_stats={}, node_outputs=None)
        assert result["variant_name"] == "unknown"

    def test_input_label_from_overrides(self) -> None:
        run = _make_run(
            status="complete",
            variant_config_snapshot={
                "run_context_overrides": {"temperature": 0.9, "model": "gpt-4o"},
            },
        )
        result = _run_to_variant_run(run, eval_stats={}, node_outputs=None)
        assert result["input_label"] is not None
        assert "temperature" in result["input_label"]

    def test_input_label_none_when_no_overrides(self) -> None:
        run = _make_run(
            status="complete",
            variant_config_snapshot={},
        )
        result = _run_to_variant_run(run, eval_stats={}, node_outputs=None)
        assert result["input_label"] is None
