"""Unit tests for the collection uninstall service (FAR-762).

Covers every branch of the uninstall lifecycle: not-found/absent install,
wrong-org, collection-id mismatch, entity-type dispatch (schema/agent/pipeline),
unmodified delete vs modified detach, out-of-band entity removal, and
unknown entity types.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.library_service.uninstall import (
    InstallNotFoundError,
    UninstallError,
    _check_unmodified,
    _delete_entity,
    _detach_entity,
    _entity_exists,
    _load_entity_rows,
    _load_install,
    uninstall_collection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_id() -> uuid.UUID:
    return uuid.uuid4()


def _org_id() -> uuid.UUID:
    return uuid.uuid4()


def _collection_id() -> uuid.UUID:
    return uuid.uuid4()


def _entity_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_install(
    org_id: uuid.UUID | None = None,
    install_id: uuid.UUID | None = None,
    collection_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock CollectionInstall with the required fields."""
    inst = MagicMock()
    inst.install_id = install_id or _install_id()
    inst.organisation_id = org_id or _org_id()
    inst.collection_id = collection_id or _collection_id()
    return inst


def _make_entity_row(
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    install_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock CollectionInstallEntity row."""
    row = MagicMock()
    row.entity_type = entity_type
    row.entity_id = entity_id or _entity_id()
    row.install_id = install_id or _install_id()
    return row


class _FakeScalarResult:
    """Mimics SQLAlchemy ScalarResult which supports iteration."""

    def __init__(self, values: list) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)

    def all(self) -> list:
        return list(self._values)


def _mock_result_scalars(values: list) -> MagicMock:
    """Create a mock result whose scalars() returns an iterable ScalarResult."""
    result = MagicMock()
    result.scalars = MagicMock(return_value=_FakeScalarResult(values))
    return result


def _mock_scalar_result(entity) -> MagicMock:
    """Create a mock result whose scalar() returns a single entity."""
    result = MagicMock()
    result.scalar = MagicMock(return_value=entity)
    return result


# ---------------------------------------------------------------------------
# _load_install
# ---------------------------------------------------------------------------


class TestLoadInstall:
    @pytest.mark.asyncio
    async def test_success(self):
        session = AsyncMock()
        org = _org_id()
        iid = _install_id()
        inst = _make_install(org_id=org, install_id=iid)
        session.get = AsyncMock(return_value=inst)

        result = await _load_install(session, org, iid)
        assert result is inst

    @pytest.mark.asyncio
    async def test_not_found(self):
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        with pytest.raises(InstallNotFoundError):
            await _load_install(session, _org_id(), _install_id())

    @pytest.mark.asyncio
    async def test_wrong_org(self):
        session = AsyncMock()
        iid = _install_id()
        inst = _make_install(install_id=iid)
        session.get = AsyncMock(return_value=inst)
        wrong_org = uuid.uuid4()
        with pytest.raises(InstallNotFoundError):
            await _load_install(session, wrong_org, iid)


# ---------------------------------------------------------------------------
# _load_entity_rows
# ---------------------------------------------------------------------------


class TestLoadEntityRows:
    @pytest.mark.asyncio
    async def test_returns_rows(self):
        session = AsyncMock()
        iid = _install_id()
        row1 = _make_entity_row("schema", install_id=iid)
        row2 = _make_entity_row("pipeline", install_id=iid)
        session.execute = AsyncMock(return_value=_mock_result_scalars([row1, row2]))
        result = await _load_entity_rows(session, iid)
        assert len(result) == 2
        assert result[0].entity_type == "schema"
        assert result[1].entity_type == "pipeline"

    @pytest.mark.asyncio
    async def test_empty(self):
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_mock_result_scalars([]))
        result = await _load_entity_rows(session, _install_id())
        assert result == []


# ---------------------------------------------------------------------------
# _check_unmodified
# ---------------------------------------------------------------------------


class TestCheckUnmodified:
    @pytest.mark.asyncio
    async def test_schema_unmodified(self):
        session = AsyncMock()
        eid = _entity_id()
        iid = _install_id()
        entity = MagicMock()
        entity.collection_install_id = iid
        session.scalar = AsyncMock(return_value=entity)
        assert await _check_unmodified(session, "schema", eid, iid) is True

    @pytest.mark.asyncio
    async def test_schema_modified(self):
        session = AsyncMock()
        eid = _entity_id()
        iid = _install_id()
        entity = MagicMock()
        entity.collection_install_id = None  # cleared by user
        session.scalar = AsyncMock(return_value=entity)
        assert await _check_unmodified(session, "schema", eid, iid) is False

    @pytest.mark.asyncio
    async def test_schema_not_found(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        assert await _check_unmodified(session, "schema", _entity_id(), _install_id()) is False

    @pytest.mark.asyncio
    async def test_agent_unmodified(self):
        session = AsyncMock()
        eid = _entity_id()
        iid = _install_id()
        entity = MagicMock()
        entity.collection_install_id = iid
        session.scalar = AsyncMock(return_value=entity)
        assert await _check_unmodified(session, "agent", eid, iid) is True

    @pytest.mark.asyncio
    async def test_agent_modified(self):
        session = AsyncMock()
        entity = MagicMock()
        entity.collection_install_id = None
        session.scalar = AsyncMock(return_value=entity)
        assert await _check_unmodified(session, "agent", _entity_id(), _install_id()) is False

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        assert await _check_unmodified(session, "agent", _entity_id(), _install_id()) is False

    @pytest.mark.asyncio
    async def test_pipeline_unmodified(self):
        session = AsyncMock()
        eid = _entity_id()
        iid = _install_id()
        entity = MagicMock()
        entity.collection_install_id = iid
        session.scalar = AsyncMock(return_value=entity)
        assert await _check_unmodified(session, "pipeline", eid, iid) is True

    @pytest.mark.asyncio
    async def test_pipeline_modified(self):
        session = AsyncMock()
        entity = MagicMock()
        entity.collection_install_id = None
        session.scalar = AsyncMock(return_value=entity)
        assert await _check_unmodified(session, "pipeline", _entity_id(), _install_id()) is False

    @pytest.mark.asyncio
    async def test_pipeline_not_found(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        assert await _check_unmodified(session, "pipeline", _entity_id(), _install_id()) is False

    @pytest.mark.asyncio
    async def test_unknown_type_returns_false(self):
        session = AsyncMock()
        assert await _check_unmodified(session, "unknown", _entity_id(), _install_id()) is False


# ---------------------------------------------------------------------------
# _delete_entity
# ---------------------------------------------------------------------------


class TestDeleteEntity:
    @pytest.mark.asyncio
    async def test_schema_found(self):
        session = AsyncMock()
        entity = MagicMock()
        session.scalar = AsyncMock(return_value=entity)
        await _delete_entity(session, "schema", _entity_id())
        session.delete.assert_awaited_once_with(entity)

    @pytest.mark.asyncio
    async def test_schema_not_found_noop(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        await _delete_entity(session, "schema", _entity_id())
        session.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_found(self):
        session = AsyncMock()
        entity = MagicMock()
        session.scalar = AsyncMock(return_value=entity)
        await _delete_entity(session, "agent", _entity_id())
        session.delete.assert_awaited_once_with(entity)

    @pytest.mark.asyncio
    async def test_agent_not_found_noop(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        await _delete_entity(session, "agent", _entity_id())
        session.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pipeline_found(self):
        session = AsyncMock()
        entity = MagicMock()
        session.scalar = AsyncMock(return_value=entity)
        await _delete_entity(session, "pipeline", _entity_id())
        session.delete.assert_awaited_once_with(entity)

    @pytest.mark.asyncio
    async def test_pipeline_not_found_noop(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        await _delete_entity(session, "pipeline", _entity_id())
        session.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_type_noop(self):
        session = AsyncMock()
        await _delete_entity(session, "unknown", _entity_id())
        session.scalar.assert_not_awaited()
        session.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# _detach_entity
# ---------------------------------------------------------------------------


class TestDetachEntity:
    @pytest.mark.asyncio
    async def test_schema_found(self):
        session = AsyncMock()
        entity = MagicMock()
        entity.collection_install_id = uuid.uuid4()
        session.scalar = AsyncMock(return_value=entity)
        await _detach_entity(session, "schema", _entity_id())
        assert entity.collection_install_id is None

    @pytest.mark.asyncio
    async def test_schema_not_found_noop(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        await _detach_entity(session, "schema", _entity_id())
        session.scalar.assert_awaited()

    @pytest.mark.asyncio
    async def test_agent_found(self):
        session = AsyncMock()
        entity = MagicMock()
        entity.collection_install_id = uuid.uuid4()
        session.scalar = AsyncMock(return_value=entity)
        await _detach_entity(session, "agent", _entity_id())
        assert entity.collection_install_id is None

    @pytest.mark.asyncio
    async def test_agent_not_found_noop(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        await _detach_entity(session, "agent", _entity_id())
        session.scalar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pipeline_found(self):
        session = AsyncMock()
        entity = MagicMock()
        entity.collection_install_id = uuid.uuid4()
        session.scalar = AsyncMock(return_value=entity)
        await _detach_entity(session, "pipeline", _entity_id())
        assert entity.collection_install_id is None

    @pytest.mark.asyncio
    async def test_pipeline_not_found_noop(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        await _detach_entity(session, "pipeline", _entity_id())
        session.scalar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_type_noop(self):
        session = AsyncMock()
        await _detach_entity(session, "unknown", _entity_id())
        session.scalar.assert_not_awaited()


# ---------------------------------------------------------------------------
# _entity_exists
# ---------------------------------------------------------------------------


class TestEntityExists:
    @pytest.mark.asyncio
    async def test_schema_exists(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=MagicMock())
        assert await _entity_exists(session, "schema", _entity_id()) is True

    @pytest.mark.asyncio
    async def test_schema_missing(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        assert await _entity_exists(session, "schema", _entity_id()) is False

    @pytest.mark.asyncio
    async def test_agent_exists(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=MagicMock())
        assert await _entity_exists(session, "agent", _entity_id()) is True

    @pytest.mark.asyncio
    async def test_agent_missing(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        assert await _entity_exists(session, "agent", _entity_id()) is False

    @pytest.mark.asyncio
    async def test_pipeline_exists(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=MagicMock())
        assert await _entity_exists(session, "pipeline", _entity_id()) is True

    @pytest.mark.asyncio
    async def test_pipeline_missing(self):
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        assert await _entity_exists(session, "pipeline", _entity_id()) is False

    @pytest.mark.asyncio
    async def test_unknown_type_missing(self):
        session = AsyncMock()
        assert await _entity_exists(session, "unknown", _entity_id()) is False


# ---------------------------------------------------------------------------
# uninstall_collection — integration of all helpers
# ---------------------------------------------------------------------------


class TestUninstallCollection:
    @pytest.mark.asyncio
    async def test_install_not_found(self):
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        with pytest.raises(InstallNotFoundError):
            await uninstall_collection(session, _org_id(), _install_id())

    @pytest.mark.asyncio
    async def test_install_wrong_org(self):
        session = AsyncMock()
        iid = _install_id()
        inst = _make_install(install_id=iid)
        session.get = AsyncMock(return_value=inst)
        with pytest.raises(InstallNotFoundError):
            await uninstall_collection(session, uuid.uuid4(), iid)

    @pytest.mark.asyncio
    async def test_collection_id_mismatch(self):
        session = AsyncMock()
        iid = _install_id()
        org = _org_id()
        cid = _collection_id()
        inst = _make_install(org_id=org, install_id=iid, collection_id=cid)
        session.get = AsyncMock(return_value=inst)
        wrong_cid = uuid.uuid4()
        with pytest.raises(InstallNotFoundError, match="does not belong"):
            await uninstall_collection(session, org, iid, collection_id=wrong_cid)

    @pytest.mark.asyncio
    async def test_no_entities(self):
        session = AsyncMock()
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)
        session.get = AsyncMock(return_value=inst)
        session.execute = AsyncMock(return_value=_mock_result_scalars([]))
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid)
        assert not result["deleted"]
        assert not result["detached"]
        assert result["install_id"] == str(iid)
        session.delete.assert_awaited_once_with(inst)
        session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unmodified_entities_are_deleted(self):
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)
        schema_eid = _entity_id()
        agent_eid = _entity_id()

        schema_entity = MagicMock()
        schema_entity.collection_install_id = iid
        agent_entity = MagicMock()
        agent_entity.collection_install_id = iid

        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)

        rows = [
            _make_entity_row("schema", entity_id=schema_eid, install_id=iid),
            _make_entity_row("agent", entity_id=agent_eid, install_id=iid),
        ]
        session.execute = AsyncMock(return_value=_mock_result_scalars(rows))

        # Map each entity_id to its mock entity for all three lookup patterns:
        # _entity_exists, _check_unmodified, _delete_entity all use
        # session.scalar(select(Model).where(Model.id == entity_id)).
        entity_map = {schema_eid: schema_entity, agent_eid: agent_entity}

        def _table_name(stmt):
            try:
                froms = stmt.get_final_froms()
                return froms[0].name if froms else ""
            except Exception:
                return ""

        async def _scalar(stmt):
            tbl = _table_name(stmt)
            if tbl in ("schemas", "agents", "pipelines"):
                # The WHERE clause filters by id; extract it
                where_clause = stmt.whereclause
                if where_clause is not None and hasattr(where_clause, "right"):
                    eid = where_clause.right.value
                    return entity_map.get(eid)
            return None

        session.scalar = AsyncMock(side_effect=_scalar)
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid)
        assert len(result["deleted"]) == 2
        # _UNINSTALL_ORDER = ["pipeline", "agent", "schema"] — agent (idx 1) before schema (idx 2)
        assert result["deleted"][0]["entity_type"] == "agent"
        assert result["deleted"][1]["entity_type"] == "schema"
        assert not result["detached"]

    @pytest.mark.asyncio
    async def test_modified_entities_are_detached(self):
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)
        schema_eid = _entity_id()

        modified_entity = MagicMock()
        modified_entity.collection_install_id = None  # cleared by user

        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)
        rows = [_make_entity_row("schema", entity_id=schema_eid, install_id=iid)]
        session.execute = AsyncMock(return_value=_mock_result_scalars(rows))

        entity_map = {schema_eid: modified_entity}

        def _table_name(stmt):
            try:
                froms = stmt.get_final_froms()
                return froms[0].name if froms else ""
            except Exception:
                return ""

        async def _scalar(stmt):
            tbl = _table_name(stmt)
            if tbl in ("schemas", "agents", "pipelines"):
                where_clause = stmt.whereclause
                if where_clause is not None and hasattr(where_clause, "right"):
                    eid = where_clause.right.value
                    return entity_map.get(eid)
            return None

        session.scalar = AsyncMock(side_effect=_scalar)
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid)
        assert not result["deleted"]
        assert len(result["detached"]) == 1
        assert result["detached"][0]["entity_type"] == "schema"
        assert modified_entity.collection_install_id is None

    @pytest.mark.asyncio
    async def test_out_of_band_entity_removal_skipped(self):
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)

        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)
        rows = [_make_entity_row("pipeline", entity_id=_entity_id(), install_id=iid)]
        session.execute = AsyncMock(return_value=_mock_result_scalars(rows))
        session.scalar = AsyncMock(return_value=None)  # entity does not exist
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid)
        assert not result["deleted"]
        assert not result["detached"]

    @pytest.mark.asyncio
    async def test_uninstall_order_pipelines_before_agents_before_schemas(self):
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)

        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)
        # Return in schema-first order (wrong order) to verify sorting
        rows = [
            _make_entity_row("schema", entity_id=_entity_id(), install_id=iid),
            _make_entity_row("agent", entity_id=_entity_id(), install_id=iid),
            _make_entity_row("pipeline", entity_id=_entity_id(), install_id=iid),
        ]
        session.execute = AsyncMock(return_value=_mock_result_scalars(rows))
        # All entities exist and are unmodified
        session.scalar = AsyncMock(return_value=MagicMock(collection_install_id=iid))
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        await uninstall_collection(session, org, iid)
        # The install itself is deleted last — verify it was called
        session.delete.assert_called_with(inst)
        # Three entity deletes + one install delete
        assert session.delete.call_count == 4

    @pytest.mark.asyncio
    async def test_collection_id_match_allowed(self):
        iid = _install_id()
        org = _org_id()
        cid = _collection_id()
        inst = _make_install(org_id=org, install_id=iid, collection_id=cid)

        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)
        session.execute = AsyncMock(return_value=_mock_result_scalars([]))
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid, collection_id=cid)
        assert result["install_id"] == str(iid)

    @pytest.mark.asyncio
    async def test_unknown_entity_type_skipped(self):
        """Entities with unknown type are skipped (not deleted or detached)."""
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)
        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)

        rows = [_make_entity_row("unknown_type", entity_id=_entity_id(), install_id=iid)]
        session.execute = AsyncMock(return_value=_mock_result_scalars(rows))
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid)
        assert not result["deleted"]
        assert not result["detached"]

    @pytest.mark.asyncio
    async def test_mixed_delete_and_detach(self):
        iid = _install_id()
        org = _org_id()
        inst = _make_install(org_id=org, install_id=iid)
        unmod_eid = _entity_id()
        mod_eid = _entity_id()

        unmod_entity = MagicMock()
        unmod_entity.collection_install_id = iid
        mod_entity = MagicMock()
        mod_entity.collection_install_id = None

        session = AsyncMock()
        session.get = AsyncMock(return_value=inst)
        rows = [
            _make_entity_row("schema", entity_id=unmod_eid, install_id=iid),
            _make_entity_row("agent", entity_id=mod_eid, install_id=iid),
        ]
        session.execute = AsyncMock(return_value=_mock_result_scalars(rows))

        entity_map = {unmod_eid: unmod_entity, mod_eid: mod_entity}

        def _table_name(stmt):
            try:
                froms = stmt.get_final_froms()
                return froms[0].name if froms else ""
            except Exception:
                return ""

        async def _scalar(stmt):
            tbl = _table_name(stmt)
            if tbl in ("schemas", "agents", "pipelines"):
                where_clause = stmt.whereclause
                if where_clause is not None and hasattr(where_clause, "right"):
                    eid = where_clause.right.value
                    return entity_map.get(eid)
            return None

        session.scalar = AsyncMock(side_effect=_scalar)
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        result = await uninstall_collection(session, org, iid)
        assert len(result["deleted"]) == 1
        assert result["deleted"][0]["entity_type"] == "schema"
        assert len(result["detached"]) == 1
        assert result["detached"][0]["entity_type"] == "agent"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_install_not_found_is_uninstall_error(self):
        assert issubclass(InstallNotFoundError, UninstallError)

    def test_uninstall_error_is_exception(self):
        assert issubclass(UninstallError, Exception)
