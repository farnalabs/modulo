"""Unit tests for variant batch API routes — pure function tests (no DB, no auth)."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.variant_batches import (
    _compute_batch_status,
    _run_to_variant_run,
)

# Re-export for type-checked mocks in list_batches tests
from modulo.db.crud.variant_group import (  # noqa: F401
    get_all_state_batch_ids,
    list_batch_states,
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
            eval_results=[{"eval_id": "e1", "passed": True, "score": 0.8}],
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
        assert len(result["eval_results"]) == 1
        assert result["eval_results"][0]["eval_id"] == "e1"

    def test_maps_pending_run_no_evals(self) -> None:
        run = _make_run(
            status="pending",
            variant_config_snapshot={"variant_name": "treatment"},
        )
        result = _run_to_variant_run(
            run,
            eval_stats={},
            eval_results=[],
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
        result = _run_to_variant_run(run, eval_stats={}, eval_results=[], node_outputs=None)
        assert result["variant_name"] == "unknown"

    def test_input_label_from_overrides(self) -> None:
        run = _make_run(
            status="complete",
            variant_config_snapshot={
                "run_context_overrides": {"temperature": 0.9, "model": "gpt-4o"},
            },
        )
        result = _run_to_variant_run(run, eval_stats={}, eval_results=[], node_outputs=None)
        assert result["input_label"] is not None
        assert "temperature" in result["input_label"]

    def test_input_label_none_when_no_overrides(self) -> None:
        run = _make_run(
            status="complete",
            variant_config_snapshot={},
        )
        result = _run_to_variant_run(run, eval_stats={}, eval_results=[], node_outputs=None)
        assert result["input_label"] is None


@pytest.mark.asyncio
class TestCrossTenantIsolation:
    """M10: Cross-tenant IDOR isolation for batch detail/re-fire/delete."""

    async def test_get_batch_returns_404_for_cross_org_batch(self) -> None:
        """Another org's batch_id returns 404, not the other org's data."""
        from fastapi import HTTPException

        from modulo.api.routes.variant_batches import get_batch

        principal = make_mock_principal()
        mock_session = make_session_mock()

        # Mock: no state row found + no runs found = 404.
        with (
            patch(
                "modulo.api.routes.variant_batches.get_batch_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "modulo.api.routes.variant_batches.get_batch_runs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_batch(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404

    async def test_delete_batch_returns_404_for_cross_org_batch(self) -> None:
        """Soft-delete another org's batch_id returns 404."""
        from fastapi import HTTPException

        from modulo.api.routes.variant_batches import delete_batch

        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variant_batches.soft_delete_batch_state",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with pytest.raises(HTTPException) as exc:
                await delete_batch(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404

    async def test_refire_batch_returns_404_for_cross_org_batch(self) -> None:
        """Re-fire another org's batch_id returns 404."""
        from fastapi import HTTPException

        from modulo.api.routes.variant_batches import re_fire_batch

        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variant_batches.get_batch_state",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc:
                await re_fire_batch(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestListBatchesTotalCount:
    """FAR-775: total must not double-count state batches on pages 2+."""

    async def test_legacy_total_excludes_all_state_ids(self) -> None:
        """states_total=50, page shows 20 state IDs, 10 legacy batches.

        Without the fix: legacy scan excluded only the page's 20 IDs,
        so 30 state-row batches leaked into the legacy count → total 90.
        With the fix: legacy scan excludes all 50 state IDs → total 60.
        """
        from modulo.api.routes.variant_batches import list_batches

        org_id = uuid.uuid4()
        principal = make_mock_principal(org_id=org_id)
        mock_session = make_session_mock()

        # Build 50 state batch_ids (only 20 will appear on the page).
        all_state_batch_ids = [uuid.uuid4() for _ in range(50)]
        page_batch_ids = all_state_batch_ids[:20]

        # State row mock objects (only the page's 20).
        state_items = []
        for bid in page_batch_ids:
            st = MagicMock()
            st.batch_id = bid
            st.name = f"batch-{bid}"
            st.pipeline_id = uuid.uuid4()
            st.created_at = datetime(2026, 1, 1, tzinfo=UTC)
            state_items.append(st)

        # 10 real legacy batches.
        legacy_batch_ids = [uuid.uuid4() for _ in range(10)]

        # --- mock session.execute dispatch ---
        # The execute calls in order are:
        #   1. legacy count query → scalar_one()
        #   2. legacy batch listing query → [(bid, count), ...]
        legacy_count_result = MagicMock()
        legacy_count_result.scalar_one.return_value = 10

        legacy_list_result = MagicMock()
        legacy_list_result.all.return_value = [(bid, 3) for bid in legacy_batch_ids]

        execute_calls = [legacy_count_result, legacy_list_result]
        call_idx = 0

        async def dispatch_execute(stmt: Any) -> MagicMock:
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            return execute_calls[idx]

        mock_session.execute = AsyncMock(side_effect=dispatch_execute)

        with (
            patch(
                "modulo.api.routes.variant_batches.set_rls_org",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.api.routes.variant_batches.set_rls_user_context",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.api.routes.variant_batches.list_batch_states",
                new_callable=AsyncMock,
                return_value=(state_items, 50),
            ),
            patch(
                "modulo.api.routes.variant_batches.get_all_state_batch_ids",
                new_callable=AsyncMock,
                return_value=set(all_state_batch_ids),
            ),
            patch(
                "modulo.api.routes.variant_batches.list_batch_runs_for_batch_ids",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            result = await list_batches(page=1, page_size=20, _session=mock_session, _principal=principal)

        # total = states_total(50) + legacy_total_count(10) = 60
        assert result["total"] == 60
        # Items are the 20 state rows + up to 10 legacy = 30 items max.
        assert len(result["items"]) <= 30
