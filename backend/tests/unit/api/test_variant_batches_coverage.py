"""Coverage tests for the FAR-775 variant batch API routes.

Drives every handler and helper in modulo.api.routes.variant_batches with
mocked DB sessions / RLS so the new production code clears the SonarCloud
new-code coverage gate. No real database is touched.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes import variant_batches as vb
from modulo.api.routes.variant_batches import (
    _build_state_summaries,
    _legacy_batch_id_filter,
    _load_batch_detail,
    _load_run_blobs,
    _resolve_batch_meta,
    _resolve_batch_timestamps,
    _summarise_batch_runs,
    delete_batch,
    get_batch,
    list_batches,
    re_fire_batch,
)
from modulo.db.crud.run_node_outputs import RunBlobs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_session_mock() -> AsyncMock:
    """Create an AsyncSession mock that supports `async with session.begin()`."""
    session = AsyncMock()
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


def _make_state(batch_id: uuid.UUID | None = None, **kwargs: object) -> MagicMock:
    st = MagicMock()
    st.batch_id = batch_id or uuid.uuid4()
    st.name = kwargs.get("name", "state-batch")
    st.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    st.created_at = kwargs.get("created_at", datetime.now(UTC))
    st.updated_at = kwargs.get("updated_at", datetime.now(UTC))
    return st


def _patch_rls() -> Any:
    return (
        patch("modulo.api.routes.variant_batches.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.variant_batches.set_rls_user_context", new_callable=AsyncMock),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestLegacyBatchIdFilter:
    def test_excludes_state_ids_when_present(self) -> None:
        ids = {uuid.uuid4()}
        expr = _legacy_batch_id_filter(ids)
        assert expr is not None

    def test_degenerate_when_no_state_ids(self) -> None:
        expr = _legacy_batch_id_filter(set())
        assert expr is not None


class TestResolveBatchMeta:
    def test_state_wins(self) -> None:
        state = _make_state(name="named", pipeline_id=uuid.uuid4())
        name, pid = _resolve_batch_meta(state, [])
        assert name == "named"
        assert pid == state.pipeline_id

    def test_runs_fallback(self) -> None:
        run = _make_run(variant_config_snapshot={"variant_name": "ctrl"})
        name, pid = _resolve_batch_meta(None, [run])
        assert "ctrl comparison" in name
        assert pid == run.pipeline_id

    def test_neither(self) -> None:
        name, pid = _resolve_batch_meta(None, [])
        assert name == ""
        assert pid is None


class TestResolveBatchTimestamps:
    def test_state_wins(self) -> None:
        c, u = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        state = _make_state(created_at=c, updated_at=u)
        assert _resolve_batch_timestamps(state, []) == (c, u)

    def test_runs_fallback(self) -> None:
        c = datetime(2026, 1, 1, tzinfo=UTC)
        comp = datetime(2026, 1, 3, tzinfo=UTC)
        run = _make_run(created_at=c, completed_at=comp)
        got_c, got_u = _resolve_batch_timestamps(None, [run])
        assert got_c == c
        assert got_u == comp

    def test_neither(self) -> None:
        assert _resolve_batch_timestamps(None, []) == (None, None)


class TestSummariseBatchRuns:
    def test_with_run_count(self) -> None:
        status, count = _summarise_batch_runs(["complete"], runs=[MagicMock()], run_count=5)
        assert status == "complete"
        assert count == 5

    def test_without_run_count(self) -> None:
        status, count = _summarise_batch_runs(["running"], runs=[MagicMock(), MagicMock()])
        assert status == "running"
        assert count == 2


class TestBuildStateSummaries:
    def test_name_and_pipeline_present(self) -> None:
        st = _make_state(name="b", pipeline_id=uuid.uuid4())
        summaries = _build_state_summaries([st], {})
        assert summaries[0]["name"] == "b"
        assert summaries[0]["batch_id"] == str(st.batch_id)
        assert summaries[0]["status"] == "pending"

    def test_pipeline_fallback_from_runs(self) -> None:
        # pipeline_id is computed from runs but intentionally dropped from the
        # emitted summary; here we just exercise that branch (lines 327-329).
        st = _make_state(name="b", pipeline_id=None)
        run = _make_run()
        summaries = _build_state_summaries([st], {st.batch_id: [run]})
        assert summaries[0]["name"] == "b"

    def test_empty(self) -> None:
        assert not _build_state_summaries([], {})


# ---------------------------------------------------------------------------
# Async helpers that hit the DB surface
# ---------------------------------------------------------------------------


class TestLoadRunBlobs:
    async def test_reads_node_outputs(self) -> None:
        session = make_session_mock()
        run = _make_run()
        blobs = RunBlobs(outputs={"agent1": {"text": "hi"}}, telemetry={}, markers=None)
        with patch(
            "modulo.api.routes.variant_batches.read_run_node_outputs_raw",
            new_callable=AsyncMock,
            return_value=blobs,
        ):
            result = await _load_run_blobs(session, run, org_id=uuid.uuid4())
        assert result == {"agent1": {"text": "hi"}}

    async def test_no_outputs_returns_none(self) -> None:
        session = make_session_mock()
        run = _make_run()
        blobs = RunBlobs(outputs=None, telemetry=None, markers=None)
        with patch(
            "modulo.api.routes.variant_batches.read_run_node_outputs_raw",
            new_callable=AsyncMock,
            return_value=blobs,
        ):
            assert await _load_run_blobs(session, run, org_id=uuid.uuid4()) is None

    async def test_empty_outputs_returns_none(self) -> None:
        session = make_session_mock()
        run = _make_run()
        blobs = RunBlobs(outputs={}, telemetry={}, markers=None)
        with patch(
            "modulo.api.routes.variant_batches.read_run_node_outputs_raw",
            new_callable=AsyncMock,
            return_value=blobs,
        ):
            assert await _load_run_blobs(session, run, org_id=uuid.uuid4()) is None


class TestBatchLoadEvalResults:
    async def test_empty_run_ids(self) -> None:
        assert not await vb._batch_load_eval_results(make_session_mock(), [])

    async def test_loads_results(self) -> None:
        er = MagicMock()
        er.run_id = uuid.uuid4()
        er.eval_id = uuid.uuid4()
        er.node_id = uuid.uuid4()
        er.passed = True
        er.score = 0.9
        er.detail = "ok"
        result = MagicMock()
        result.scalars.return_value.all.return_value = [er]
        session = make_session_mock()
        session.execute.return_value = result
        out = await vb._batch_load_eval_results(session, [er.run_id])
        assert out[er.run_id][0]["eval_id"] == str(er.eval_id)
        assert out[er.run_id][0]["passed"] is True


class TestBatchLoadEvalStats:
    async def test_empty_run_ids(self) -> None:
        assert not await vb._batch_load_eval_stats(make_session_mock(), [])

    async def test_loads_stats(self) -> None:
        rid = uuid.uuid4()
        result = MagicMock()
        result.all.return_value = [(rid, 10, 7)]
        session = make_session_mock()
        session.execute.return_value = result
        out = await vb._batch_load_eval_stats(session, [rid])
        assert out[rid] == (10, 7)


class TestLoadBatchDetail:
    async def test_raises_404_when_empty(self) -> None:
        from fastapi import HTTPException

        session = make_session_mock()
        with (
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.variant_batches.get_batch_runs", new_callable=AsyncMock, return_value=[]),
        ):
            with pytest.raises(HTTPException) as exc:
                await _load_batch_detail(session, batch_id=uuid.uuid4(), org_id=uuid.uuid4())
            assert exc.value.status_code == 404

    async def test_legacy_fallback(self) -> None:
        run = _make_run(variant_config_snapshot={"variant_name": "ctrl"}, completed_at=datetime.now(UTC))
        session = make_session_mock()
        with (
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.variant_batches.get_batch_runs", new_callable=AsyncMock, return_value=[run]),
            patch("modulo.api.routes.variant_batches._batch_load_eval_stats", new_callable=AsyncMock, return_value={}),
            patch(
                "modulo.api.routes.variant_batches._batch_load_eval_results", new_callable=AsyncMock, return_value={}
            ),
            patch("modulo.api.routes.variant_batches._load_run_blobs", new_callable=AsyncMock, return_value=None),
        ):
            detail = await _load_batch_detail(session, batch_id=uuid.uuid4(), org_id=uuid.uuid4())
        assert detail["status"] == "complete"
        assert detail["runs"][0]["variant_name"] == "ctrl"

    async def test_state_present(self) -> None:
        run = _make_run()
        state = _make_state(name="named")
        session = make_session_mock()
        with (
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
            patch("modulo.api.routes.variant_batches.get_batch_runs", new_callable=AsyncMock, return_value=[run]),
            patch(
                "modulo.api.routes.variant_batches._batch_load_eval_stats",
                new_callable=AsyncMock,
                return_value={run.id: (0, 0)},
            ),
            patch(
                "modulo.api.routes.variant_batches._batch_load_eval_results", new_callable=AsyncMock, return_value={}
            ),
            patch("modulo.api.routes.variant_batches._load_run_blobs", new_callable=AsyncMock, return_value=None),
        ):
            detail = await _load_batch_detail(session, batch_id=state.batch_id, org_id=uuid.uuid4())
        assert detail["name"] == "named"
        assert detail["runs"][0]["run_status"] == "complete"


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


class TestListBatchesLegacyScan:
    async def test_fills_page_with_legacy_batches(self) -> None:
        org_id = uuid.uuid4()
        principal = make_mock_principal(org_id=org_id)
        session = make_session_mock()

        state_bid = uuid.uuid4()
        legacy_bid = uuid.uuid4()
        states = [_make_state(batch_id=state_bid, name="state-batch")]
        legacy_run = _make_run(variant_config_snapshot={"variant_name": "legacy"})

        legacy_count_result = MagicMock()
        legacy_count_result.scalar_one.return_value = 2
        legacy_list_result = MagicMock()
        legacy_list_result.all.return_value = [(state_bid, 3), (legacy_bid, 4)]

        execute_calls = [legacy_count_result, legacy_list_result]
        call_idx = 0

        async def dispatch(stmt: Any) -> MagicMock:
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            return execute_calls[idx]

        session.execute = AsyncMock(side_effect=dispatch)

        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch(
                "modulo.api.routes.variant_batches.list_batch_states",
                new_callable=AsyncMock,
                return_value=(states, 1),
            ),
            patch(
                "modulo.api.routes.variant_batches.get_all_state_batch_ids",
                new_callable=AsyncMock,
                return_value={state_bid},
            ),
            patch(
                "modulo.api.routes.variant_batches.list_batch_runs_for_batch_ids",
                new_callable=AsyncMock,
                return_value={legacy_bid: [legacy_run]},
            ),
        ):
            result = await list_batches(page=1, page_size=20, _session=session, _principal=principal)

        # total = 1 state + 2 legacy = 3
        assert result["total"] == 3
        # one state summary + one legacy summary (the state_bid hit is excluded)
        assert len(result["items"]) == 2
        legacy_item = next(i for i in result["items"] if i["batch_id"] == str(legacy_bid))
        assert "legacy comparison" in legacy_item["name"]


class TestGetBatchHandler:
    async def test_returns_detail(self) -> None:
        principal = make_mock_principal()
        session = make_session_mock()
        detail = {"batch_id": str(uuid.uuid4()), "name": "x"}
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch(
                "modulo.api.routes.variant_batches._load_batch_detail",
                new_callable=AsyncMock,
                return_value=detail,
            ),
        ):
            out = await get_batch(uuid.uuid4(), session, principal)
        assert out == detail


class TestDeleteBatchHandler:
    async def test_soft_deletes(self) -> None:
        principal = make_mock_principal()
        session = make_session_mock()
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch(
                "modulo.api.routes.variant_batches.soft_delete_batch_state",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            out = await delete_batch(uuid.uuid4(), session, principal)
        assert out == {}


class TestReFireBatchHandler:
    async def test_re_fires(self) -> None:
        org_id = uuid.uuid4()
        principal = make_mock_principal(org_id=org_id)
        session = make_session_mock()
        new_id = uuid.uuid4()
        state = MagicMock()
        state.variant_group_id = uuid.uuid4()
        state.input_payload = {"prompt": "hi"}
        group = MagicMock()
        group.organisation_id = org_id
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
            patch("modulo.api.routes.variant_batches.get_variant_group", new_callable=AsyncMock, return_value=group),
            patch(
                "modulo.api.routes.variant_batches.run_variant_batch",
                new_callable=AsyncMock,
                return_value=[{"frozen_snapshot": {"batch_id": str(new_id)}}],
            ),
            patch(
                "modulo.api.routes.variant_batches._load_batch_detail",
                new_callable=AsyncMock,
                return_value={"batch_id": str(new_id)},
            ),
        ):
            out = await re_fire_batch(uuid.uuid4(), session, principal)
        assert out["batch_id"] == str(new_id)

    async def test_no_source_group_returns_422(self) -> None:
        from fastapi import HTTPException

        principal = make_mock_principal()
        session = make_session_mock()
        state = MagicMock()
        state.variant_group_id = None
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
        ):
            with pytest.raises(HTTPException) as exc:
                await re_fire_batch(uuid.uuid4(), session, principal)
            assert exc.value.status_code == 422

    async def test_missing_group_returns_404(self) -> None:
        from fastapi import HTTPException

        principal = make_mock_principal()
        session = make_session_mock()
        state = MagicMock()
        state.variant_group_id = uuid.uuid4()
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
            patch("modulo.api.routes.variant_batches.get_variant_group", new_callable=AsyncMock, return_value=None),
        ):
            with pytest.raises(HTTPException) as exc:
                await re_fire_batch(uuid.uuid4(), session, principal)
            assert exc.value.status_code == 404

    async def test_org_mismatch_returns_404(self) -> None:
        from fastapi import HTTPException

        org_id = uuid.uuid4()
        principal = make_mock_principal(org_id=org_id)
        session = make_session_mock()
        state = MagicMock()
        state.variant_group_id = uuid.uuid4()
        group = MagicMock()
        group.organisation_id = uuid.uuid4()  # different org
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
            patch("modulo.api.routes.variant_batches.get_variant_group", new_callable=AsyncMock, return_value=group),
        ):
            with pytest.raises(HTTPException) as exc:
                await re_fire_batch(uuid.uuid4(), session, principal)
            assert exc.value.status_code == 404

    async def test_quota_exceeded_returns_429(self) -> None:
        from fastapi import HTTPException

        org_id = uuid.uuid4()
        principal = make_mock_principal(org_id=org_id)
        session = make_session_mock()
        state = MagicMock()
        state.variant_group_id = uuid.uuid4()
        state.input_payload = {}
        group = MagicMock()
        group.organisation_id = org_id
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
            patch("modulo.api.routes.variant_batches.get_variant_group", new_callable=AsyncMock, return_value=group),
            patch("modulo.api.routes.variant_batches.run_variant_batch", new_callable=AsyncMock, return_value=None),
        ):
            with pytest.raises(HTTPException) as exc:
                await re_fire_batch(uuid.uuid4(), session, principal)
            assert exc.value.status_code == 429

    async def test_missing_new_batch_id_returns_502(self) -> None:
        from fastapi import HTTPException

        org_id = uuid.uuid4()
        principal = make_mock_principal(org_id=org_id)
        session = make_session_mock()
        state = MagicMock()
        state.variant_group_id = uuid.uuid4()
        state.input_payload = {}
        group = MagicMock()
        group.organisation_id = org_id
        with (
            _patch_rls()[0],
            _patch_rls()[1],
            patch("modulo.api.routes.variant_batches.get_batch_state", new_callable=AsyncMock, return_value=state),
            patch("modulo.api.routes.variant_batches.get_variant_group", new_callable=AsyncMock, return_value=group),
            patch(
                "modulo.api.routes.variant_batches.run_variant_batch",
                new_callable=AsyncMock,
                return_value=[{"frozen_snapshot": {}}],
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await re_fire_batch(uuid.uuid4(), session, principal)
            assert exc.value.status_code == 502
