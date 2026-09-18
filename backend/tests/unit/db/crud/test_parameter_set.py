"""Unit tests for ParameterSet CRUD (mocked session)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.crud.parameter_set import (
    create_set,
    delete_set,
    get_set,
    get_set_references,
    list_sets,
    restore_set,
    soft_delete_set,
    update_set,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SET_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _mock_set(**overrides: object) -> MagicMock:
    ps = MagicMock()
    ps.id = overrides.get("id", _SET_ID)
    ps.name = overrides.get("name", "test-set")
    ps.description = overrides.get("description", "desc")
    ps.values = overrides.get("values", {"key": "val"})
    ps.version = overrides.get("version", 1)
    ps.organisation_id = overrides.get("org_id", _ORG_ID)
    ps.deleted_at = overrides.get("deleted_at")
    return ps


def _exec_scalar_one_or_none(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _exec_scalars(items: list[object]) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars)
    return result


def _exec_returning(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


# ── create_set ──────────────────────────────────────────────────────


class TestCreateSet:
    async def test_creates_and_flushes(self, mock_session: AsyncMock) -> None:
        await create_set(
            mock_session,
            parameter_schema_id=_SCHEMA_ID,
            org_id=_ORG_ID,
            name="my-set",
            description="a set",
            values={"a": 1},
            account_id=_ACCOUNT_ID,
            schema_version=2,
        )

        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        added = mock_session.add.call_args[0][0]
        assert added.name == "my-set"
        assert added.description == "a set"
        assert added.values == {"a": 1}
        assert added.schema_version == 2

    async def test_default_schema_version_is_1(self, mock_session: AsyncMock) -> None:
        await create_set(
            mock_session,
            parameter_schema_id=_SCHEMA_ID,
            org_id=_ORG_ID,
            name="s",
            description=None,
            values={},
            account_id=_ACCOUNT_ID,
        )

        added = mock_session.add.call_args[0][0]
        assert added.schema_version == 1


# ── get_set ─────────────────────────────────────────────────────────


class TestGetSet:
    async def test_returns_set(self, mock_session: AsyncMock) -> None:
        ps = _mock_set()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(ps))

        result = await get_set(mock_session, _SET_ID)

        assert result is ps

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await get_set(mock_session, uuid.uuid4())

        assert result is None


# ── list_sets ───────────────────────────────────────────────────────


class TestListSets:
    async def test_returns_filtered_sets(self, mock_session: AsyncMock) -> None:
        ps = _mock_set()
        mock_session.execute = AsyncMock(return_value=_exec_scalars([ps]))

        result = await list_sets(mock_session, parameter_schema_id=_SCHEMA_ID, org_id=_ORG_ID)

        assert len(result) == 1
        assert result[0] is ps

    async def test_returns_empty_when_none(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        result = await list_sets(mock_session, parameter_schema_id=_SCHEMA_ID, org_id=_ORG_ID)

        assert result == []


# ── update_set ──────────────────────────────────────────────────────


class TestUpdateSet:
    async def test_updates_name_and_bumps_version(self, mock_session: AsyncMock) -> None:
        ps = _mock_set(version=3)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(ps))

        result = await update_set(mock_session, _SET_ID, name="new-name", version=3)

        assert result is ps
        assert ps.version == 4
        mock_session.flush.assert_awaited_once()

    async def test_updates_description(self, mock_session: AsyncMock) -> None:
        ps = _mock_set(version=1)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(ps))

        await update_set(mock_session, _SET_ID, description="updated", version=1)

        assert ps.description == "updated"

    async def test_updates_values(self, mock_session: AsyncMock) -> None:
        ps = _mock_set(version=1)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(ps))

        await update_set(mock_session, _SET_ID, values={"new": True}, version=1)

        assert ps.values == {"new": True}

    async def test_returns_none_on_version_mismatch(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await update_set(mock_session, _SET_ID, name="x", version=99)

        assert result is None
        mock_session.flush.assert_not_called()

    async def test_no_updates_bumps_version(self, mock_session: AsyncMock) -> None:
        ps = _mock_set(version=5)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(ps))

        result = await update_set(mock_session, _SET_ID, version=5)

        assert result is ps
        assert ps.version == 6


# ── soft_delete_set ────────────────────────────────────────────────


class TestSoftDeleteSet:
    async def test_sets_deleted_at(self, mock_session: AsyncMock) -> None:
        ps = _mock_set()
        mock_session.execute = AsyncMock(return_value=_exec_returning(ps))

        result = await soft_delete_set(mock_session, _SET_ID)

        assert result is ps
        mock_session.flush.assert_awaited_once()

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_returning(None))

        result = await soft_delete_set(mock_session, uuid.uuid4())

        assert result is None


# ── restore_set ────────────────────────────────────────────────────


class TestRestoreSet:
    async def test_clears_deleted_at(self, mock_session: AsyncMock) -> None:
        ps = _mock_set()
        mock_session.execute = AsyncMock(return_value=_exec_returning(ps))

        result = await restore_set(mock_session, _SET_ID)

        assert result is ps
        mock_session.flush.assert_awaited_once()

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_returning(None))

        result = await restore_set(mock_session, uuid.uuid4())

        assert result is None


# ── delete_set ─────────────────────────────────────────────────────


class TestDeleteSet:
    async def test_deletes_existing_set(self, mock_session: AsyncMock) -> None:
        ps = _mock_set()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(ps))

        result = await delete_set(mock_session, _SET_ID)

        assert result is True
        mock_session.delete.assert_called_once_with(ps)
        mock_session.flush.assert_awaited_once()

    async def test_returns_false_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await delete_set(mock_session, uuid.uuid4())

        assert result is False
        mock_session.delete.assert_not_called()


# ── get_set_references ─────────────────────────────────────────────


class TestGetSetReferences:
    async def test_returns_empty_when_no_snapshots(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}

    async def test_finds_reference_in_parameter_bindings(self, mock_session: AsyncMock) -> None:
        node_id = uuid.uuid4()
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = {str(node_id): {"parameter_set_id": str(_SET_ID)}}
        snap.graph_json = {}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert snap.id in result["snapshots"]
        assert node_id in result["pipeline_nodes"]

    async def test_finds_reference_in_graph_nodes(self, mock_session: AsyncMock) -> None:
        node_id = uuid.uuid4()
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = None
        snap.graph_json = {"nodes": [{"id": str(node_id), "parameter_set_id": str(_SET_ID)}]}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert snap.id in result["snapshots"]
        assert node_id in result["pipeline_nodes"]

    async def test_skips_non_dict_bindings(self, mock_session: AsyncMock) -> None:
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = {"node1": "not-a-dict"}
        snap.graph_json = {}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}

    async def test_skips_non_dict_graph_nodes(self, mock_session: AsyncMock) -> None:
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = None
        snap.graph_json = {"nodes": ["not-a-dict"]}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}

    async def test_skips_graph_json_none(self, mock_session: AsyncMock) -> None:
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = None
        snap.graph_json = None

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}

    async def test_skips_graph_json_not_dict(self, mock_session: AsyncMock) -> None:
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = None
        snap.graph_json = "not-a-dict"

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}

    async def test_skips_already_found_snapshot_in_graph_walk(self, mock_session: AsyncMock) -> None:
        node_id_bindings = uuid.uuid4()
        node_id_graph = uuid.uuid4()
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = {str(node_id_bindings): {"parameter_set_id": str(_SET_ID)}}
        snap.graph_json = {"nodes": [{"id": str(node_id_graph), "parameter_set_id": str(_SET_ID)}]}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert len(result["pipeline_nodes"]) == 1
        assert node_id_bindings in result["pipeline_nodes"]
        assert node_id_graph not in result["pipeline_nodes"]

    async def test_binding_with_wrong_set_id_ignored(self, mock_session: AsyncMock) -> None:
        other_id = uuid.uuid4()
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = {"node1": {"parameter_set_id": str(other_id)}}
        snap.graph_json = {}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}

    async def test_binding_without_parameter_set_id_key(self, mock_session: AsyncMock) -> None:
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.parameter_bindings_json = {"node1": {"other_key": "val"}}
        snap.graph_json = {}

        mock_session.execute = AsyncMock(return_value=_exec_scalars([snap]))

        result = await get_set_references(mock_session, _SET_ID)

        assert result == {"pipeline_nodes": [], "snapshots": []}


# ── apply_updates (from base) ──────────────────────────────────────


class TestApplyUpdates:
    def test_applies_valid_updates(self) -> None:
        entity = MagicMock()
        entity.name = "old"
        entity.description = "old"

        apply_updates(entity, {"name": "new", "description": "new"})

        assert entity.name == "new"
        assert entity.description == "new"

    def test_skips_immutable_fields(self) -> None:
        entity = MagicMock()
        original_id = entity.id
        original_org = entity.organisation_id

        apply_updates(entity, {"id": uuid.uuid4(), "organisation_id": uuid.uuid4()})

        assert entity.id == original_id
        assert entity.organisation_id == original_org

    def test_skips_unknown_fields(self) -> None:
        entity = MagicMock(spec=["name"])
        entity.name = "old"

        apply_updates(entity, {"name": "new", "nonexistent": "val"})

        assert entity.name == "new"
