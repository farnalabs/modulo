"""Unit tests for parameter schema and parameter set write MCP tools.

Covers: create_parameter_schema, update_parameter_schema,
delete_parameter_schema, restore_parameter_schema,
create_parameter_set, update_parameter_set,
delete_parameter_set, restore_parameter_set.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import IntegrityError, ProgrammingError

from modulo.api.mcp_server import (
    create_parameter_schema,
    create_parameter_set,
    delete_parameter_schema,
    delete_parameter_set,
    restore_parameter_schema,
    restore_parameter_set,
    update_parameter_schema,
    update_parameter_set,
)
from tests.unit.mcp.helpers import ORG_ID, USER_ID, AuthContext, make_session_context

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _make_mock_schema(
    *,
    schema_id: uuid.UUID | None = None,
    name: str = "deploy-params",
    version: int = 1,
) -> MagicMock:
    s = MagicMock()
    s.id = schema_id or uuid.uuid4()
    s.organisation_id = ORG_ID
    s.name = name
    s.description = "Deployment parameters"
    s.version = version
    s.parameters = [{"name": "region", "type": "string", "required": True}]
    s.created_at = NOW
    s.updated_at = NOW
    s.account_id = USER_ID
    return s


def _make_mock_set(
    *,
    set_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
    name: str = "production",
    version: int = 1,
) -> MagicMock:
    ps = MagicMock()
    ps.id = set_id or uuid.uuid4()
    ps.parameter_schema_id = schema_id or uuid.uuid4()
    ps.organisation_id = ORG_ID
    ps.name = name
    ps.description = "Production values"
    ps.version = version
    ps.schema_version = 3
    ps.values = {"region": "us-east-1"}
    ps.created_at = NOW
    ps.updated_at = NOW
    ps.account_id = USER_ID
    return ps


# ---------------------------------------------------------------------------
# create_parameter_schema
# ---------------------------------------------------------------------------


class TestCreateParameterSchema(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await create_parameter_schema(name="test")
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await create_parameter_schema(name="test")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.create_schema")
    async def test_returns_created_schema(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_create.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_schema(
            name="deploy-params",
            description="Deployment parameters",
            parameters=[{"name": "region", "type": "string", "required": True}],
        )
        assert result["data"]["id"] == str(schema.id)
        assert result["data"]["name"] == "deploy-params"
        assert result["data"]["version"] == 1

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.create_schema")
    async def test_integrity_error_returns_conflict(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_create.side_effect = IntegrityError("dup", {}, Exception())
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_schema(name="duplicate")
        assert result["error"] == "conflict"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.create_schema")
    async def test_programming_error_returns_migration_required(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_create.side_effect = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_schema(name="test")
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.create_schema")
    async def test_generic_error_returns_tool_error(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_create.side_effect = RuntimeError("boom")
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_schema(name="test")
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# update_parameter_schema
# ---------------------------------------------------------------------------


class TestUpdateParameterSchema(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await update_parameter_schema(schema_id="not-a-uuid", version=1)
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_schema(schema_id=str(uuid.uuid4()), version=1)
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_schema.update_schema")
    async def test_returns_updated_schema(
        self,
        mock_update: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        existing = _make_mock_schema()
        mock_get.return_value = existing
        updated = _make_mock_schema(version=2)
        updated.name = "updated-name"
        mock_update.return_value = updated
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_schema(
            schema_id=str(existing.id),
            version=1,
            name="updated-name",
        )
        assert result["data"]["version"] == 2
        assert result["data"]["name"] == "updated-name"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_schema.update_schema")
    async def test_version_conflict_returns_error(
        self,
        mock_update: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        existing = _make_mock_schema()
        mock_get.return_value = existing
        mock_update.return_value = None  # version mismatch
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_schema(
            schema_id=str(existing.id),
            version=1,
            name="updated-name",
        )
        assert result["error"] == "conflict"


# ---------------------------------------------------------------------------
# delete_parameter_schema
# ---------------------------------------------------------------------------


class TestDeleteParameterSchema(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await delete_parameter_schema(schema_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await delete_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_schema.soft_delete_schema")
    async def test_returns_deleted(
        self,
        mock_delete: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        existing = _make_mock_schema()
        mock_get.return_value = existing
        deleted = _make_mock_schema()
        mock_delete.return_value = deleted
        mock_session.return_value = make_session_context(AsyncMock())
        result = await delete_parameter_schema(schema_id=str(existing.id))
        assert result["data"]["deleted"] is True
        assert result["data"]["id"] == str(deleted.id)


# ---------------------------------------------------------------------------
# restore_parameter_schema
# ---------------------------------------------------------------------------


class TestRestoreParameterSchema(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await restore_parameter_schema(schema_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await restore_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_schema.restore_schema")
    async def test_returns_restored(
        self,
        mock_restore: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        existing = _make_mock_schema()
        mock_get.return_value = existing
        restored = _make_mock_schema()
        mock_restore.return_value = restored
        mock_session.return_value = make_session_context(AsyncMock())
        result = await restore_parameter_schema(schema_id=str(existing.id))
        assert result["data"]["id"] == str(restored.id)
        assert result["data"]["version"] == 1


# ---------------------------------------------------------------------------
# create_parameter_set
# ---------------------------------------------------------------------------


class TestCreateParameterSet(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await create_parameter_set(schema_id="not-a-uuid", name="test")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_schema_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_set(schema_id=str(uuid.uuid4()), name="test")
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.create_set")
    async def test_returns_created_set(
        self,
        mock_create: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get.return_value = schema
        ps = _make_mock_set(schema_id=schema.id)
        mock_create.return_value = ps
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_set(
            schema_id=str(schema.id),
            name="production",
            values={"region": "us-east-1"},
        )
        assert result["data"]["id"] == str(ps.id)
        assert result["data"]["name"] == "production"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.create_set")
    async def test_integrity_error_returns_conflict(
        self,
        mock_create: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get.return_value = schema
        mock_create.side_effect = IntegrityError("dup", {}, Exception())
        mock_session.return_value = make_session_context(AsyncMock())
        result = await create_parameter_set(schema_id=str(schema.id), name="duplicate")
        assert result["error"] == "conflict"


# ---------------------------------------------------------------------------
# update_parameter_set
# ---------------------------------------------------------------------------


class TestUpdateParameterSet(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await update_parameter_set(schema_id="bad", set_id="bad", version=1)
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_schema_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_set(
            schema_id=str(uuid.uuid4()),
            set_id=str(uuid.uuid4()),
            version=1,
        )
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.get_set")
    async def test_set_not_found(
        self,
        mock_get_set: AsyncMock,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get_schema.return_value = schema
        mock_get_set.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_set(
            schema_id=str(schema.id),
            set_id=str(uuid.uuid4()),
            version=1,
        )
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_cross_org_schema_reads_as_not_found(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        foreign_schema = _make_mock_schema()
        foreign_schema.organisation_id = uuid.uuid4()  # not ORG_ID
        mock_get_schema.return_value = foreign_schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_set(
            schema_id=str(foreign_schema.id),
            set_id=str(uuid.uuid4()),
            version=1,
        )
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_set.update_set")
    async def test_returns_updated_set(
        self,
        mock_update: AsyncMock,
        mock_get_set: AsyncMock,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get_schema.return_value = schema
        existing = _make_mock_set(schema_id=schema.id)
        mock_get_set.return_value = existing
        updated = _make_mock_set(schema_id=schema.id, version=2)
        updated.name = "updated-set"
        mock_update.return_value = updated
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_set(
            schema_id=str(schema.id),
            set_id=str(existing.id),
            version=1,
            name="updated-set",
        )
        assert result["data"]["version"] == 2
        assert result["data"]["name"] == "updated-set"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_set.update_set")
    async def test_version_conflict_returns_error(
        self,
        mock_update: AsyncMock,
        mock_get_set: AsyncMock,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get_schema.return_value = schema
        existing = _make_mock_set(schema_id=schema.id)
        mock_get_set.return_value = existing
        mock_update.return_value = None  # version mismatch
        mock_session.return_value = make_session_context(AsyncMock())
        result = await update_parameter_set(
            schema_id=str(schema.id),
            set_id=str(existing.id),
            version=1,
            name="updated-set",
        )
        assert result["error"] == "conflict"


# ---------------------------------------------------------------------------
# delete_parameter_set
# ---------------------------------------------------------------------------


class TestDeleteParameterSet(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await delete_parameter_set(schema_id="bad", set_id="bad")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_schema_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await delete_parameter_set(
            schema_id=str(uuid.uuid4()),
            set_id=str(uuid.uuid4()),
        )
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_set.soft_delete_set")
    async def test_returns_deleted(
        self,
        mock_delete: AsyncMock,
        mock_get_set: AsyncMock,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get_schema.return_value = schema
        existing = _make_mock_set(schema_id=schema.id)
        mock_get_set.return_value = existing
        deleted = _make_mock_set(schema_id=schema.id)
        mock_delete.return_value = deleted
        mock_session.return_value = make_session_context(AsyncMock())
        result = await delete_parameter_set(
            schema_id=str(schema.id),
            set_id=str(existing.id),
        )
        assert result["data"]["deleted"] is True
        assert result["data"]["id"] == str(deleted.id)


# ---------------------------------------------------------------------------
# restore_parameter_set
# ---------------------------------------------------------------------------


class TestRestoreParameterSet(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await restore_parameter_set(schema_id="bad", set_id="bad")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_schema_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await restore_parameter_set(
            schema_id=str(uuid.uuid4()),
            set_id=str(uuid.uuid4()),
        )
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.get_set")
    @patch("modulo.db.crud.parameter_set.restore_set")
    async def test_returns_restored(
        self,
        mock_restore: AsyncMock,
        mock_get_set: AsyncMock,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = _make_mock_schema()
        mock_get_schema.return_value = schema
        existing = _make_mock_set(schema_id=schema.id)
        mock_get_set.return_value = existing
        restored = _make_mock_set(schema_id=schema.id)
        mock_restore.return_value = restored
        mock_session.return_value = make_session_context(AsyncMock())
        result = await restore_parameter_set(
            schema_id=str(schema.id),
            set_id=str(existing.id),
        )
        assert result["data"]["id"] == str(restored.id)
        assert result["data"]["version"] == 1
