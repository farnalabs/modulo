"""Exhaustive error-branch coverage for parameter schema & set MCP tools.

Targets the exception handlers and not-found branches inside the 13 CRUD MCP
tools so that every new production line in ``modulo.api.mcp_server`` is covered.
These are pure unit tests (backend/tests/** is excluded from the SonarCloud
analysis scope, so they exercise the source without counting against the
new-code duplication/coverage gate as source).
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.mcp_server import (
    create_parameter_schema,
    create_parameter_set,
    delete_parameter_schema,
    delete_parameter_set,
    get_parameter_schema,
    get_parameter_schema_references,
    get_parameter_set,
    list_parameter_sets,
    restore_parameter_schema,
    restore_parameter_set,
    update_parameter_schema,
    update_parameter_set,
    validate_parameter_schema,
)
from tests.unit.mcp.helpers import AuthContext, make_session_context
from tests.unit.mcp.test_mcp_parameter_schema_write_tools import (
    _make_mock_schema,
    _make_mock_set,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _session():
    return make_session_context(AsyncMock())


def _cross_org_schema():
    s = _make_mock_schema()
    s.organisation_id = uuid.uuid4()  # different org
    return s


def _cross_org_set(schema_id: uuid.UUID):
    ps = _make_mock_set(schema_id=schema_id)
    ps.organisation_id = uuid.uuid4()
    ps.parameter_schema_id = uuid.uuid4()
    return ps


# ---------------------------------------------------------------------------
# create_parameter_schema
# ---------------------------------------------------------------------------


class TestCreateParameterSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.create_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(self, mock_create, mock_session, mock_validate):
        mock_create.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await create_parameter_schema(name="test")
        assert result["error"] == "database_unavailable"


# ---------------------------------------------------------------------------
# get_parameter_schema
# ---------------------------------------------------------------------------


class TestGetParameterSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(self, mock_get, mock_session, mock_validate):
        mock_get.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await get_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_session, mock_validate):
        mock_get.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await get_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# update_parameter_schema
# ---------------------------------------------------------------------------


class TestUpdateParameterSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.update_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get, mock_update, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_update.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await update_parameter_schema(schema_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.update_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_update, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_update.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await update_parameter_schema(schema_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.update_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get, mock_update, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_update.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await update_parameter_schema(schema_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "database_unavailable"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.update_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_conflict_when_version_drift(self, mock_get, mock_update, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_update.return_value = None
        mock_session.return_value = _session()
        result = await update_parameter_schema(schema_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "conflict"


# ---------------------------------------------------------------------------
# delete_parameter_schema
# ---------------------------------------------------------------------------


class TestDeleteParameterSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.soft_delete_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get, mock_delete, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_delete.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await delete_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.soft_delete_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_delete, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_delete.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await delete_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.soft_delete_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get, mock_delete, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_delete.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await delete_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "database_unavailable"


# ---------------------------------------------------------------------------
# restore_parameter_schema
# ---------------------------------------------------------------------------


class TestRestoreParameterSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.restore_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get, mock_restore, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_restore.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await restore_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.restore_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_restore, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_restore.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await restore_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.restore_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get, mock_restore, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_restore.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await restore_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "database_unavailable"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.restore_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found_when_restore_returns_none(self, mock_get, mock_restore, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_restore.return_value = None
        mock_session.return_value = _session()
        result = await restore_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"


# ---------------------------------------------------------------------------
# get_parameter_schema_references
# ---------------------------------------------------------------------------


class TestGetParameterSchemaReferencesErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema_references")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(self, mock_get, mock_refs, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_refs.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await get_parameter_schema_references(schema_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema_references")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_refs, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_refs.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await get_parameter_schema_references(schema_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# validate_parameter_schema
# ---------------------------------------------------------------------------


class TestValidateParameterSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(self, mock_get, mock_session, mock_validate):
        mock_get.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await validate_parameter_schema(schema_id=str(uuid.uuid4()), values={"region": "x"})
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_session, mock_validate):
        mock_get.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await validate_parameter_schema(schema_id=str(uuid.uuid4()), values={"region": "x"})
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# list_parameter_sets
# ---------------------------------------------------------------------------


class TestListParameterSetsErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.list_sets")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(self, mock_get, mock_list, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_list.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await list_parameter_sets(schema_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.list_sets")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_list, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_list.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await list_parameter_sets(schema_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# create_parameter_set
# ---------------------------------------------------------------------------


class TestCreateParameterSetErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.create_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get, mock_create, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_create.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await create_parameter_set(schema_id=str(uuid.uuid4()), name="prod")
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.create_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_create, mock_session, mock_validate):
        mock_get.return_value = _make_mock_schema()
        mock_create.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await create_parameter_set(schema_id=str(uuid.uuid4()), name="prod")
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.create_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get, mock_create, mock_session, mock_validate
    ):
        mock_get.return_value = _make_mock_schema()
        mock_create.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await create_parameter_set(schema_id=str(uuid.uuid4()), name="prod")
        assert result["error"] == "database_unavailable"


# ---------------------------------------------------------------------------
# get_parameter_set
# ---------------------------------------------------------------------------


class TestGetParameterSetErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.get_set")
    async def test_programming_error_returns_migration_required(self, mock_get, mock_session, mock_validate):
        mock_get.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await get_parameter_set(schema_id=str(uuid.uuid4()), set_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.get_set")
    async def test_generic_error_returns_tool_error(self, mock_get, mock_session, mock_validate):
        mock_get.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await get_parameter_set(schema_id=str(uuid.uuid4()), set_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# update_parameter_set
# ---------------------------------------------------------------------------


class TestUpdateParameterSetErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.update_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get_schema, mock_get_set, mock_update, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_update.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await update_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.update_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(
        self, mock_get_schema, mock_get_set, mock_update, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_update.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await update_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.update_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get_schema, mock_get_set, mock_update, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_update.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await update_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "database_unavailable"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.update_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_conflict_when_version_drift(
        self, mock_get_schema, mock_get_set, mock_update, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_update.return_value = None
        mock_session.return_value = _session()
        result = await update_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "conflict"


# ---------------------------------------------------------------------------
# delete_parameter_set
# ---------------------------------------------------------------------------


class TestDeleteParameterSetErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.soft_delete_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get_schema, mock_get_set, mock_delete, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_delete.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await delete_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.soft_delete_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(
        self, mock_get_schema, mock_get_set, mock_delete, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_delete.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await delete_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.soft_delete_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get_schema, mock_get_set, mock_delete, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_delete.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await delete_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "database_unavailable"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.soft_delete_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found_when_delete_returns_none(
        self, mock_get_schema, mock_get_set, mock_delete, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_delete.return_value = None
        mock_session.return_value = _session()
        result = await delete_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"


# ---------------------------------------------------------------------------
# restore_parameter_set
# ---------------------------------------------------------------------------


class TestRestoreParameterSetErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.restore_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_programming_error_returns_migration_required(
        self, mock_get_schema, mock_get_set, mock_restore, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_restore.side_effect = ProgrammingError("SELECT 1", {}, Exception("missing"))
        mock_session.return_value = _session()
        result = await restore_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.restore_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_generic_error_returns_tool_error(
        self, mock_get_schema, mock_get_set, mock_restore, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_restore.side_effect = RuntimeError("boom")
        mock_session.return_value = _session()
        result = await restore_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.restore_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_sqlalchemy_error_returns_database_unavailable(
        self, mock_get_schema, mock_get_set, mock_restore, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_restore.side_effect = SQLAlchemyError("dead")
        mock_session.return_value = _session()
        result = await restore_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "database_unavailable"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.restore_set")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found_when_restore_returns_none(
        self, mock_get_schema, mock_get_set, mock_restore, mock_session, mock_validate
    ):
        sid = uuid.uuid4()
        mock_get_schema.return_value = _make_mock_schema()
        mock_get_set.return_value = _make_mock_set(schema_id=sid)
        mock_restore.return_value = None
        mock_session.return_value = _session()
        result = await restore_parameter_set(schema_id=str(sid), set_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"
