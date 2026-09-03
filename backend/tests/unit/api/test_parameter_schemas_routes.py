"""Unit tests for /api/v1/parameter-schemas and /parameter-sets endpoints.

Complements test_parameter_schemas_endpoint.py (tenant isolation on PUT).
Unit tier: no DB — CRUD functions are patched at the route-module boundary
and the SQLAlchemy session is a contract-correct AsyncMock.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _queue_executes(session: AsyncMock, *results: Any) -> None:
    """Route execute() calls: authz-enforce reads get a benign result, others consume the queue."""
    queue = list(results)

    async def _execute(stmt: object, *_a: object, **_k: object) -> Any:
        if "authz_enforce" in str(stmt):
            benign = MagicMock()
            benign.scalar_one_or_none.return_value = None
            return benign
        if not queue:
            raise AssertionError("Unexpected session.execute() — no more stubbed results")
        return queue.pop(0)

    session.execute = AsyncMock(side_effect=_execute)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    test_client = TestClient(app)
    test_client.mock_session = mock_session  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()


def _make_schema(organisation_id: uuid.UUID = _ORG_ID, **overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.organisation_id = organisation_id
    s.name = "Test Parameter Schema"
    s.description = None
    s.version = 2
    s.parameters = [{"name": "region", "type": "string", "required": True}]
    s.account_id = _USER_ID
    s.created_at = _NOW
    s.updated_at = _NOW
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _make_set(schema_id: uuid.UUID, organisation_id: uuid.UUID = _ORG_ID, **overrides: Any) -> MagicMock:
    ps = MagicMock()
    ps.id = uuid.uuid4()
    ps.parameter_schema_id = schema_id
    ps.organisation_id = organisation_id
    ps.version = 1
    ps.schema_version = 1
    ps.name = "Prod Values"
    ps.description = None
    ps.values = {"region": "eu"}
    ps.account_id = _USER_ID
    ps.created_at = _NOW
    ps.updated_at = _NOW
    for key, value in overrides.items():
        setattr(ps, key, value)
    return ps


# ---------------------------------------------------------------------------
# Schema list / create
# ---------------------------------------------------------------------------


def test_list_schemas_returns_paged_items(client: TestClient) -> None:
    page = PageResult(items=[_make_schema()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.parameter_schemas.list_schemas", new_callable=AsyncMock, return_value=page),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/parameter-schemas")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "Test Parameter Schema"


def test_list_schemas_missing_table_returns_501(client: TestClient) -> None:
    err = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock, side_effect=err):
        resp = client.get("/api/v1/parameter-schemas")
    assert resp.status_code == 501


def test_list_schemas_db_error_returns_503(client: TestClient) -> None:
    with patch(
        "modulo.api.routes.parameter_schemas.set_rls_org",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError(),
    ):
        resp = client.get("/api/v1/parameter-schemas")
    assert resp.status_code == 503


def test_list_schemas_unexpected_error_returns_500(client: TestClient) -> None:
    with patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock, side_effect=RuntimeError):
        resp = client.get("/api/v1/parameter-schemas")
    assert resp.status_code == 500


def test_create_schema_returns_201(client: TestClient) -> None:
    schema = _make_schema(name="Created")
    with (
        patch("modulo.api.routes.parameter_schemas.create_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/parameter-schemas",
            json={"name": "Created", "parameters": [{"name": "p1", "type": "string"}]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Created"


def test_create_schema_duplicate_name_returns_409(client: TestClient) -> None:
    err = IntegrityError("INSERT", {}, Exception("unique"))
    with (
        patch("modulo.api.routes.parameter_schemas.create_schema", new_callable=AsyncMock, side_effect=err),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/parameter-schemas", json={"name": "Dup"})
    assert resp.status_code == 409


def test_create_schema_invalid_param_type_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/parameter-schemas",
        json={"name": "Bad", "parameters": [{"name": "p1", "type": "float"}]},
    )
    assert resp.status_code == 422


def test_create_schema_blank_name_rejected(client: TestClient) -> None:
    resp = client.post("/api/v1/parameter-schemas", json={"name": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Schema get / update / delete / restore
# ---------------------------------------------------------------------------


def test_get_schema_owned_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{schema.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(schema.id)


def test_get_schema_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_schema_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.soft_delete_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/parameter-schemas/{schema.id}")
    assert resp.status_code == 200, resp.text


def test_delete_schema_missing_returns_404(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.soft_delete_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/parameter-schemas/{schema.id}")
    assert resp.status_code == 404


def test_restore_schema_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.restore_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/parameter-schemas/{schema.id}/restore")
    assert resp.status_code == 200, resp.text


def test_restore_schema_not_deleted_returns_404(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.restore_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/parameter-schemas/{schema.id}/restore")
    assert resp.status_code == 404


def test_update_schema_stale_version_returns_409(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.update_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.put(f"/api/v1/parameter-schemas/{schema.id}", json={"version": 1, "name": "Renamed"})
    assert resp.status_code == 409
    assert "modified by another user" in resp.json()["detail"]


def test_update_schema_missing_table_returns_501(client: TestClient) -> None:
    err = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock, side_effect=err):
        resp = client.put(f"/api/v1/parameter-schemas/{uuid.uuid4()}", json={"version": 1})
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# diff / references / validate
# ---------------------------------------------------------------------------


def test_diff_same_version_returns_empty_changes(client: TestClient) -> None:
    schema = _make_schema(version=3)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(
            f"/api/v1/parameter-schemas/{schema.id}/diff",
            params={"from_version": 3, "to_version": 3},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert not body["changes"]


def test_diff_historical_versions_returns_warning(client: TestClient) -> None:
    schema = _make_schema(version=2)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(
            f"/api/v1/parameter-schemas/{schema.id}/diff",
            params={"from_version": 1, "to_version": 5},
        )
    assert resp.status_code == 200, resp.text
    assert "warning" in resp.json()


def test_diff_current_version_lists_parameters(client: TestClient) -> None:
    schema = _make_schema(version=2)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(
            f"/api/v1/parameter-schemas/{schema.id}/diff",
            params={"from_version": 1, "to_version": 2},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_version"] == 2
    assert body["total_parameters"] == 1


def test_diff_invalid_versions_rejected(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(
            f"/api/v1/parameter-schemas/{schema.id}/diff",
            params={"from_version": 0, "to_version": 2},
        )
    assert resp.status_code == 422


def test_schema_references_returns_ids(client: TestClient) -> None:
    schema = _make_schema()
    agent_id = uuid.uuid4()
    set_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch(
            "modulo.api.routes.parameter_schemas.get_schema_references",
            new_callable=AsyncMock,
            return_value={"agents": [agent_id], "sets": [set_id]},
        ),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{schema.id}/references")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agents"] == [{"id": str(agent_id)}]
    assert body["sets"] == [{"id": str(set_id)}]


def test_schema_references_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{uuid.uuid4()}/references")
    assert resp.status_code == 404


def test_validate_values_all_valid(client: TestClient) -> None:
    schema = _make_schema(
        parameters=[
            {"name": "region", "type": "string", "required": True},
            {"name": "retries", "type": "number", "minimum": 0, "maximum": 5},
            {"name": "mode", "type": "select", "options": ["fast", "slow"]},
            {"name": "verbose", "type": "boolean"},
        ]
    )
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/parameter-schemas/{schema.id}/validate",
            json={"values": {"region": "eu", "retries": 3, "mode": "fast", "verbose": True}},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert not body["errors"]


def test_validate_values_collects_all_error_kinds(client: TestClient) -> None:
    schema = _make_schema(
        parameters=[
            {"name": "region", "type": "string", "required": True},
            {"name": "retries", "type": "number", "minimum": 0, "maximum": 5},
            {"name": "mode", "type": "select", "options": ["fast", "slow"]},
            {"name": "verbose", "type": "boolean"},
            {"name": "label", "type": "string"},
        ]
    )
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/parameter-schemas/{schema.id}/validate",
            json={
                "values": {
                    "retries": 99,
                    "mode": "turbo",
                    "verbose": "yes",
                    "label": 42,
                }
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    fields = {e["field"] for e in body["errors"]}
    assert fields == {"region", "retries", "mode", "verbose", "label"}


def test_validate_missing_schema_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/parameter-schemas/{uuid.uuid4()}/validate",
            json={"values": {}},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sets (nested under schema)
# ---------------------------------------------------------------------------


def test_list_sets_returns_items(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.list_sets", new_callable=AsyncMock, return_value=[ps]),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{schema.id}/sets")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "Prod Values"


def test_list_sets_missing_schema_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{uuid.uuid4()}/sets")
    assert resp.status_code == 404


def test_create_set_returns_201(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.create_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/parameter-schemas/{schema.id}/sets",
            json={"name": "Prod Values", "values": {"region": "eu"}},
        )
    assert resp.status_code == 201, resp.text


def test_create_set_duplicate_returns_409(client: TestClient) -> None:
    schema = _make_schema()
    err = IntegrityError("INSERT", {}, Exception("unique"))
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.create_set", new_callable=AsyncMock, side_effect=err),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/parameter-schemas/{schema.id}/sets", json={"name": "Dup"})
    assert resp.status_code == 409


def test_get_set_owned_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(ps.id)


def test_get_set_wrong_schema_returns_404(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(uuid.UUID("00000000-0000-0000-0000-000000000009"))
    with (
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}")
    assert resp.status_code == 404


def test_update_set_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.update_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.put(
            f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}",
            json={"version": 1, "name": "Updated Values"},
        )
    assert resp.status_code == 200, resp.text


def test_update_set_stale_returns_409(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.update_set", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.put(
            f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}",
            json={"version": 1},
        )
    assert resp.status_code == 409
    assert "modified by another user" in resp.json()["detail"]


def test_delete_set_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.soft_delete_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}")
    assert resp.status_code == 200, resp.text


def test_delete_set_missing_returns_404(client: TestClient) -> None:
    schema = _make_schema()
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/parameter-schemas/{schema.id}/sets/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_restore_set_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(schema.id)
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.restore_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}/restore")
    assert resp.status_code == 200, resp.text


def test_restore_set_wrong_schema_returns_404(client: TestClient) -> None:
    schema = _make_schema()
    ps = _make_set(uuid.UUID("00000000-0000-0000-0000-000000000009"))
    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", new_callable=AsyncMock, return_value=schema),
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.restore_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/parameter-schemas/{schema.id}/sets/{ps.id}/restore")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Global set references
# ---------------------------------------------------------------------------


def test_parameter_set_references_returns_raw_refs(client: TestClient) -> None:
    ps = _make_set(uuid.uuid4())
    refs = {"pipelines": [uuid.uuid4()]}
    with (
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=ps),
        patch("modulo.api.routes.parameter_schemas.get_set_references", new_callable=AsyncMock, return_value=refs),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-sets/{ps.id}/references")
    assert resp.status_code == 200, resp.text
    assert "pipelines" in resp.json()


def test_parameter_set_references_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.parameter_schemas.get_set", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/parameter-sets/{uuid.uuid4()}/references")
    assert resp.status_code == 404


def test_parameter_set_references_missing_table_returns_501(client: TestClient) -> None:
    err = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with patch("modulo.api.routes.parameter_schemas.set_rls_org", new_callable=AsyncMock, side_effect=err):
        resp = client.get(f"/api/v1/parameter-sets/{uuid.uuid4()}/references")
    assert resp.status_code == 501
