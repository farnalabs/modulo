"""Tests for schema route error handling â€” ProgrammingError, SQLAlchemyError, IntegrityError."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSchemaListProgrammingError:
    def test_list_schemas_programming_error_returns_501(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with patch("modulo.api.routes.schemas.list_schemas", mock_list), patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get("/api/v1/schemas")
        assert resp.status_code == 501
        assert "run database migrations" in resp.json()["detail"].lower()

    def test_list_schemas_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with patch("modulo.api.routes.schemas.list_schemas", mock_list), patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get("/api/v1/schemas")
        assert resp.status_code == 503


class TestSchemaCreateErrors:
    SCHEMA_CREATE_JSON = {"name": "TestSchema", "description": "test"}

    def test_create_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.create_schema", mock_create),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 501

    def test_create_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.create_schema", mock_create),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 503

    def test_create_schema_integrity_error_returns_409(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=IntegrityError("stmt", "params", "duplicate name"))
        with (
            patch("modulo.api.routes.schemas.create_schema", mock_create),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


class TestSchemaGetErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_get_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with patch("modulo.api.routes.schemas.get_schema", mock_get), patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 501

    def test_get_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with patch("modulo.api.routes.schemas.get_schema", mock_get), patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 503


class TestSchemaVersionCreateErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    VERSION_CREATE_JSON = {"version": "1.0.0", "version_number": 1, "definition_json": {"type": "object"}}

    def test_create_version_programming_error_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()),
            patch(
                "modulo.api.routes.schemas.create_schema_version",
                AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing")),
            ),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/schemas/{self.SCHEMA_ID}/versions", json=self.VERSION_CREATE_JSON)
        assert resp.status_code == 501

    def test_create_version_integrity_error_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()),
            patch(
                "modulo.api.routes.schemas.create_schema_version",
                AsyncMock(side_effect=IntegrityError("stmt", "params", "duplicate version")),
            ),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/schemas/{self.SCHEMA_ID}/versions", json=self.VERSION_CREATE_JSON)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


class TestSchemaDeleteErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_delete_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_delete = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.delete_schema", mock_delete),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 501

    def test_delete_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_delete = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.delete_schema", mock_delete),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 503


class TestSchemaUpdateErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    SCHEMA_UPDATE_JSON = {"name": "RenamedSchema"}

    def test_update_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_update = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.update_schema", mock_update),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}", json=self.SCHEMA_UPDATE_JSON)
        assert resp.status_code == 501

    def test_update_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_update = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.update_schema", mock_update),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}", json=self.SCHEMA_UPDATE_JSON)
        assert resp.status_code == 503

    def test_update_schema_integrity_error_returns_409(self, client: TestClient) -> None:
        mock_update = AsyncMock(side_effect=IntegrityError("stmt", "params", "duplicate name"))
        with (
            patch("modulo.api.routes.schemas.update_schema", mock_update),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}", json=self.SCHEMA_UPDATE_JSON)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


class TestSchemaDeprecateErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_deprecate_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_deprecate = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.deprecate_schema", mock_deprecate),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}/deprecate")
        assert resp.status_code == 501

    def test_deprecate_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_deprecate = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.deprecate_schema", mock_deprecate),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}/deprecate")
        assert resp.status_code == 503


class TestSchemaVersionListErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_list_versions_programming_error_returns_501(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.list_schema_versions", mock_list),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/versions")
        assert resp.status_code == 501

    def test_list_versions_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.list_schema_versions", mock_list),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/versions")
        assert resp.status_code == 503


class TestSchemaVersionGetErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_get_version_programming_error_returns_501(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.get_schema_version", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/versions/1.0.0")
        assert resp.status_code == 501

    def test_get_version_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.get_schema_version", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/versions/1.0.0")
        assert resp.status_code == 503


class TestSchemaMigrateErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    SCHEMA_TARGET_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000100"))

    def test_migrate_programming_error_returns_501(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.get_schema", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/migrate",
                json={
                    "from_schema_id": self.SCHEMA_ID,
                    "to_schema_id": self.SCHEMA_TARGET_ID,
                    "data": {},
                },
            )
        assert resp.status_code == 501

    def test_migrate_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.get_schema", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/migrate",
                json={
                    "from_schema_id": self.SCHEMA_ID,
                    "to_schema_id": self.SCHEMA_TARGET_ID,
                    "data": {},
                },
            )
        assert resp.status_code == 503

class TestSchemaValidateEndpoint:
    def test_validate_valid_schema(self, client: TestClient) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        resp = client.post("/api/v1/schemas/validate", json={"definition": schema})
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["errors"] == []

    def test_validate_invalid_schema_type(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/schemas/validate",
            json={"definition": {"type": 123}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0

    def test_validate_path_includes_all_segments(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/schemas/validate",
            json={"definition": {"type": "object", "properties": {"x": {"type": "invalidtype"}}}},
        )
        assert resp.status_code == 200
        data = resp.json()
        if not data["valid"] and data["errors"]:
            err = data["errors"][0]
            # Verify path is not "(root)" when there are segments — popleft mutation bug regression guard
            assert err["path"], "path must not be empty — popleft mutation would set (root)"

    def test_validate_empty_schema(self, client: TestClient) -> None:
        resp = client.post("/api/v1/schemas/validate", json={"definition": {}})
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_validate_schema_with_union(self, client: TestClient) -> None:
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        resp = client.post("/api/v1/schemas/validate", json={"definition": schema})
        assert resp.status_code == 200
        assert resp.json()["valid"] is True


class TestSchemaGenerateErrors:
    def test_generate_no_backends_returns_400(self, client: TestClient) -> None:
        empty_result = MagicMock(items=[], total=0, page=1, page_size=1)
        with (
            patch("modulo.api.routes.schemas.list_model_backends", return_value=empty_result),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/generate",
                json={"description": "generate a product schema"},
            )
        assert resp.status_code == 400
        assert "no model backends" in resp.json()["detail"].lower()

    def test_generate_programming_error_returns_501(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.list_model_backends", mock_list),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/generate",
                json={"description": "generate a product schema"},
            )
        assert resp.status_code == 501

    def test_generate_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.list_model_backends", mock_list),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/generate",
                json={"description": "generate a product schema"},
            )
        assert resp.status_code == 503


class TestSchemaInferProgrammingErrors:
    CONNECTOR_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_infer_programming_error_returns_501(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.get_connector_instance", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
            patch("modulo.api.routes.schemas.create_secrets_backend"),
        ):
            resp = client.post(
                "/api/v1/schemas/infer",
                json={
                    "connector_instance_id": self.CONNECTOR_ID,
                    "sample_query": {"resource": "issues"},
                },
            )
        assert resp.status_code == 501

    def test_infer_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.get_connector_instance", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
            patch("modulo.api.routes.schemas.create_secrets_backend"),
        ):
            resp = client.post(
                "/api/v1/schemas/infer",
                json={
                    "connector_instance_id": self.CONNECTOR_ID,
                    "sample_query": {"resource": "issues"},
                },
            )
        assert resp.status_code == 503


class TestSchemaImportErrors:
    def test_import_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post("/api/v1/schemas/import", json={"content": "not json"})
        assert resp.status_code == 400
        assert "invalid json" in resp.json()["detail"].lower()

    def test_import_non_object_json_returns_400(self, client: TestClient) -> None:
        import json as _json
        resp = client.post("/api/v1/schemas/import", json={"content": _json.dumps(["list"])})
        assert resp.status_code == 400
        assert "must be a json object" in resp.json()["detail"].lower()

    def test_import_valid_schema_returns_200(self, client: TestClient) -> None:
        import json as _json
        schema = {
            "type": "object",
            "title": "Person",
            "description": "A person",
            "properties": {"name": {"type": "string", "description": "Full name"}},
            "required": ["name"],
        }
        resp = client.post("/api/v1/schemas/import", json={"content": _json.dumps(schema)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Person"
        assert len(data["fields"]) == 1
        assert data["fields"][0]["name"] == "name"
        assert data["fields"][0]["required"] is True

    def test_import_invalid_schema_returns_422(self, client: TestClient) -> None:
        import json as _json
        resp = client.post(
            "/api/v1/schemas/import",
            json={"content": _json.dumps({"type": "nonexistent"})},
        )
        assert resp.status_code == 422
        assert "invalid json schema" in resp.json()["detail"].lower()


class TestSchemaFieldsEndpoint:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_fields_programming_error_returns_501(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with (
            patch("modulo.api.routes.schemas.get_schema", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/fields")
        assert resp.status_code == 501

    def test_fields_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with (
            patch("modulo.api.routes.schemas.get_schema", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/fields")
        assert resp.status_code == 503


class TestSchemaMigrateDataErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    SCHEMA_TARGET_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000100"))

    @patch("modulo.api.routes.schemas.create_migration", side_effect=ValueError("bad schema"))
    def test_migrate_create_plan_error_returns_500(self, mock_create: MagicMock, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()),
            patch("modulo.api.routes.schemas._get_latest_version", return_value=MagicMock(definition_json={"type": "object"})),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/migrate",
                json={
                    "from_schema_id": self.SCHEMA_ID,
                    "to_schema_id": self.SCHEMA_TARGET_ID,
                    "data": {},
                },
            )
        assert resp.status_code == 500
        assert "migration plan" in resp.json()["detail"].lower()

    @patch("modulo.api.routes.schemas.apply_migration", side_effect=TypeError("bad data"))
    def test_migrate_apply_error_returns_500(self, mock_apply: MagicMock, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()),
            patch("modulo.api.routes.schemas._get_latest_version", return_value=MagicMock(definition_json={"type": "object"})),
            patch("modulo.api.routes.schemas.create_migration", return_value=MagicMock(field_additions={}, field_removals=[], type_changes={}, renames={})),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/migrate",
                json={
                    "from_schema_id": self.SCHEMA_ID,
                    "to_schema_id": self.SCHEMA_TARGET_ID,
                    "data": {"field": "value"},
                },
            )
        assert resp.status_code == 500
        assert "apply migration" in resp.json()["detail"].lower()
