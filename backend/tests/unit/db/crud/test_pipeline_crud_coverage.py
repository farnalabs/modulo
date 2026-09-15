"""Unit tests for pipeline CRUD — coverage for pure functions and CRUD branches.

Covers the uncovered paths identified by coverage: validate_max_concurrent_runs,
create_pipeline folder_id, get_pipeline filters, list_pipelines branches,
update_pipeline ownership transfer, soft_delete/restore/archive, graph helpers,
_edge_to_plain_dict, _snapshot_to_dict, _preserve_omitted_gate_config, and
_resolve_read_session_factory.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.pipeline import (
    PipelineHasActiveRunsError,
    _edge_to_plain_dict,
    _preserve_omitted_gate_config,
    _resolve_read_session_factory,
    _snapshot_pin_to_dict,
    _snapshot_to_dict,
    archive_pipeline,
    check_pipeline_name_available,
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    get_pipeline_graph,
    list_pipelines,
    restore_pipeline,
    soft_delete_pipeline,
    unarchive_pipeline,
    update_pipeline,
    validate_max_concurrent_runs,
)

# ---------------------------------------------------------------------------
# validate_max_concurrent_runs
# ---------------------------------------------------------------------------


class TestValidateMaxConcurrentRuns:
    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="max_concurrent_runs must be >= 1"):
            validate_max_concurrent_runs(0)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="max_concurrent_runs must be >= 1"):
            validate_max_concurrent_runs(-3)

    def test_accepts_valid_values(self) -> None:
        assert validate_max_concurrent_runs(1) == 1
        assert validate_max_concurrent_runs(10) == 10
        assert validate_max_concurrent_runs(999) == 999


# ---------------------------------------------------------------------------
# PipelineHasActiveRunsError
# ---------------------------------------------------------------------------


class TestPipelineHasActiveRunsError:
    def test_message_includes_count(self) -> None:
        err = PipelineHasActiveRunsError(5)
        assert "5" in str(err)
        assert err.active_run_count == 5


# ---------------------------------------------------------------------------
# get_pipeline
# ---------------------------------------------------------------------------


def _mock_session(result: MagicMock | None = None) -> AsyncMock:
    session = AsyncMock()
    res = result or MagicMock()
    session.execute = AsyncMock(return_value=res)
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.mark.asyncio
class TestGetPipeline:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        result = await get_pipeline(session, uuid.uuid4())
        assert result is None

    async def test_with_include_deleted(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value="pipeline")))
        result = await get_pipeline(session, uuid.uuid4(), include_deleted=True)
        assert result is not None

    async def test_with_organisation_id_filter(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value="pipeline")))
        result = await get_pipeline(session, uuid.uuid4(), organisation_id=uuid.uuid4())
        assert result is not None


# ---------------------------------------------------------------------------
# check_pipeline_name_available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCheckPipelineNameAvailable:
    async def test_returns_true_when_available(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        assert await check_pipeline_name_available(session, uuid.uuid4(), "name") is True

    async def test_returns_false_when_taken(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value="existing")))
        assert await check_pipeline_name_available(session, uuid.uuid4(), "name") is False


# ---------------------------------------------------------------------------
# create_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCreatePipeline:
    async def test_creates_without_folder(self) -> None:
        session = _mock_session()
        pipeline = await create_pipeline(session, org_id=uuid.uuid4(), name="test", account_id=uuid.uuid4())
        assert pipeline is not None
        session.add.assert_called_once()
        session.flush.assert_awaited_once()

    async def test_raises_on_missing_folder(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with pytest.raises(ValueError, match="Folder not found"):
            await create_pipeline(
                session,
                org_id=uuid.uuid4(),
                name="test",
                account_id=uuid.uuid4(),
                folder_id=uuid.uuid4(),
            )

    async def test_validates_max_concurrent_runs(self) -> None:
        session = _mock_session()
        with pytest.raises(ValueError, match="max_concurrent_runs must be >= 1"):
            await create_pipeline(
                session,
                org_id=uuid.uuid4(),
                name="test",
                account_id=uuid.uuid4(),
                max_concurrent_runs=0,
            )


# ---------------------------------------------------------------------------
# list_pipelines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestListPipelines:
    async def test_programming_error_returns_empty(self) -> None:
        from sqlalchemy.exc import ProgrammingError

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=ProgrammingError("table missing", {}, None))
        result = await list_pipelines(session, page=1, page_size=20)
        assert not result.items
        assert result.total == 0

    async def test_cursor_path(self) -> None:
        session = _mock_session(MagicMock(items=[], total=0, next_cursor=None, has_more=False))
        with patch("modulo.db.crud.pipeline.CursorPaginator") as mock_paginator_cls:
            mock_pag = MagicMock()
            mock_paginator_cls.return_value = mock_pag
            mock_pag.paginate = AsyncMock(return_value=MagicMock(items=[], total=0, next_cursor=None, has_more=False))
            result = await list_pipelines(session, cursor="abc", page=1, page_size=20)
            assert not result.items


# ---------------------------------------------------------------------------
# update_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUpdatePipeline:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        result = await update_pipeline(session, uuid.uuid4(), {"name": "new"})
        assert result is None

    async def test_validates_max_concurrent_runs_in_update(self) -> None:
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        pipeline.archived_at = None
        pipeline.deleted_at = None
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=pipeline)))
        with pytest.raises(ValueError, match="max_concurrent_runs must be >= 1"):
            await update_pipeline(session, uuid.uuid4(), {"max_concurrent_runs": 0})

    async def test_flushes_on_valid_update(self) -> None:
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        pipeline.archived_at = None
        pipeline.deleted_at = None
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=pipeline)))
        result = await update_pipeline(session, uuid.uuid4(), {"name": "new"})
        assert result is pipeline
        session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# soft_delete / restore / delete / archive / unarchive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSoftDeletePipeline:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        assert await soft_delete_pipeline(session, uuid.uuid4()) is None

    async def test_deletes_when_found(self) -> None:
        mock_pipeline = MagicMock()
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=mock_pipeline)))
        result = await soft_delete_pipeline(session, uuid.uuid4(), deleted_by=uuid.uuid4())
        assert result is mock_pipeline


@pytest.mark.asyncio
class TestRestorePipeline:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        assert await restore_pipeline(session, uuid.uuid4()) is None


@pytest.mark.asyncio
class TestDeletePipeline:
    async def test_returns_false_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        assert await delete_pipeline(session, uuid.uuid4()) is False

    async def test_deletes_when_found(self) -> None:
        pipeline = MagicMock()
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=pipeline)))
        assert await delete_pipeline(session, uuid.uuid4()) is True


@pytest.mark.asyncio
class TestArchivePipeline:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        assert await archive_pipeline(session, uuid.uuid4()) is None

    async def test_archives_when_found(self) -> None:
        pipeline = MagicMock()
        pipeline.archived_at = None
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=pipeline)))
        result = await archive_pipeline(session, uuid.uuid4())
        assert result is pipeline
        assert pipeline.archived_at is not None


@pytest.mark.asyncio
class TestUnarchivePipeline:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        assert await unarchive_pipeline(session, uuid.uuid4()) is None

    async def test_unarchives_when_found(self) -> None:
        pipeline = MagicMock()
        pipeline.archived_at = "some-date"
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=pipeline)))
        result = await unarchive_pipeline(session, uuid.uuid4())
        assert result is pipeline
        assert pipeline.archived_at is None


# ---------------------------------------------------------------------------
# get_pipeline_graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetPipelineGraph:
    async def test_returns_none_when_not_found(self) -> None:
        session = _mock_session(MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        result = await get_pipeline_graph(session, uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# _edge_to_plain_dict
# ---------------------------------------------------------------------------


class TestEdgeToPlainDict:
    def test_converts_edge_to_dict(self) -> None:
        edge = MagicMock()
        edge.source_node_id = uuid.uuid4()
        edge.target_node_id = uuid.uuid4()
        edge.edge_type = "normal"
        edge.source_port = "out"
        edge.target_port = "in"
        edge.hitl_gate_config = {"some": "config"}
        edge.condition_expression = "x > 0"
        d = _edge_to_plain_dict(edge)
        assert d["source_node_id"] == edge.source_node_id
        assert d["target_node_id"] == edge.target_node_id
        assert d["edge_type"] == "normal"
        assert d["source_port"] == "out"
        assert d["target_port"] == "in"
        assert d["hitl_gate_config"] == {"some": "config"}
        assert d["condition_expression"] == "x > 0"

    def test_defaults_ports_when_none(self) -> None:
        edge = MagicMock()
        edge.source_port = None
        edge.target_port = None
        edge.hitl_gate_config = None
        d = _edge_to_plain_dict(edge)
        assert d["source_port"] == "out"
        assert d["target_port"] == "in"


# ---------------------------------------------------------------------------
# _snapshot_pin_to_dict / _snapshot_to_dict
# ---------------------------------------------------------------------------


class TestSnapshotPinToDict:
    def test_converts_pin(self) -> None:
        pin = MagicMock()
        pin.node_id = "node1"
        pin.direction = "input"
        pin.schema_id = uuid.uuid4()
        pin.schema_version = 1
        d = _snapshot_pin_to_dict(pin)
        assert d["node_id"] == "node1"
        assert d["schema_version"] == 1


class TestSnapshotToDict:
    def test_converts_snapshot(self) -> None:
        snap = MagicMock()
        snap.snapshot_version = 1
        snap.account_id = uuid.uuid4()
        snap.environment_profile_id = None
        snap.graph_json = {"nodes": []}
        snap.connector_bindings_json = {}
        snap.schema_pins_json = []
        snap.prompt_pins_json = []
        snap.model_backend_pins_json = {}
        snap.composite_bindings_json = {}
        snap.parameter_bindings_json = {}
        snap.tag = "v1"
        snap.notes = "test"
        snap.default_autonomy_level = "manual"
        snap.config_json = {}
        snap.run_context_defaults = {}
        d = _snapshot_to_dict(snap, [{"node_id": "n1"}])
        assert d["snapshot_version"] == 1
        assert d["pins"] == [{"node_id": "n1"}]
        assert d["graph_json"] == {"nodes": []}


# ---------------------------------------------------------------------------
# _preserve_omitted_gate_config
# ---------------------------------------------------------------------------


class TestPreserveOmittedGateConfig:
    def test_present_returns_config_value(self) -> None:
        edge = {"hitl_gate_config": {"gated": True}}
        result = _preserve_omitted_gate_config(edge, {})
        assert result == {"gated": True}

    def test_omitted_preserves_existing(self) -> None:
        edge = {"source_node_id": "a", "target_node_id": "b", "edge_type": "normal"}
        old = {("a", "b", "normal"): {"old_config": True}}
        result = _preserve_omitted_gate_config(edge, old)
        assert result == {"old_config": True}

    def test_omitted_returns_none_when_no_existing(self) -> None:
        edge = {"source_node_id": "a", "target_node_id": "b", "edge_type": "normal"}
        result = _preserve_omitted_gate_config(edge, {})
        assert result is None

    def test_explicit_false_returns_none(self) -> None:
        edge = {
            "hitl_gate_config_present": False,
            "source_node_id": "a",
            "target_node_id": "b",
            "edge_type": "normal",
        }
        result = _preserve_omitted_gate_config(edge, {"key": "val"})
        assert result is None


# ---------------------------------------------------------------------------
# _resolve_read_session_factory
# ---------------------------------------------------------------------------


class TestResolveReadSessionFactory:
    def test_returns_supplied_factory(self) -> None:
        def factory():
            return None

        result_factory, engine = _resolve_read_session_factory(MagicMock(), factory)
        assert result_factory is factory
        assert engine is None

    def test_raises_when_invalid_request(self) -> None:
        from sqlalchemy.exc import InvalidRequestError

        session = MagicMock()
        session.bind = None
        session.get_bind.side_effect = InvalidRequestError("no bind", {}, None)
        with pytest.raises(RuntimeError, match="cannot derive"):
            _resolve_read_session_factory(session, None)
