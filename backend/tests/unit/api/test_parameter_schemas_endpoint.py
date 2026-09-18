"""Unit tests for /api/v1/parameter-schemas endpoints (tenant isolation)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
_SCHEMA_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_schema(organisation_id: uuid.UUID = _ORG_ID) -> MagicMock:
    s = MagicMock()
    s.id = _SCHEMA_ID
    s.organisation_id = organisation_id
    s.name = "Test Parameter Schema"
    s.description = None
    s.version = 1
    s.parameters = []
    s.account_id = uuid.uuid4()
    s.created_by = s.account_id
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
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


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_update_parameter_schema_owned_returns_200(client: TestClient) -> None:
    schema = _make_schema(organisation_id=_ORG_ID)
    captured: dict[str, object] = {}

    async def _get_schema(*_a: object, **_k: object) -> MagicMock:
        return schema

    async def _update_schema(*a: object, **_k: object) -> MagicMock:
        captured["called"] = True
        return schema

    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", _get_schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", _noop),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", _noop),
        patch("modulo.api.routes.parameter_schemas.update_schema", _update_schema),
    ):
        resp = client.put(
            f"/api/v1/parameter-schemas/{schema.id}",
            json={"version": 1, "name": "Renamed"},
        )
    assert resp.status_code == 200
    assert captured.get("called") is True


def test_update_parameter_schema_cross_org_returns_404(client: TestClient) -> None:
    # A parameter schema belonging to a DIFFERENT organisation must be rejected
    # with 404 and must never reach the update_schema CRUD path.
    schema = _make_schema(organisation_id=_OTHER_ORG_ID)
    captured: dict[str, object] = {}

    async def _get_schema(*_a: object, **_k: object) -> MagicMock:
        return schema

    async def _update_schema(*a: object, **_k: object) -> MagicMock:
        captured["called"] = True
        return schema

    with (
        patch("modulo.api.routes.parameter_schemas.get_schema", _get_schema),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", _noop),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", _noop),
        patch("modulo.api.routes.parameter_schemas.update_schema", _update_schema),
    ):
        resp = client.put(
            f"/api/v1/parameter-schemas/{schema.id}",
            json={"version": 1, "name": "Renamed"},
        )
    assert resp.status_code == 404
    assert captured.get("called") is None
