"""Unit tests for /api/v1/schema-folders endpoints and schema folder assignment."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "b" * 32)
os.environ.setdefault("FERNET_KEY", "b" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_SF_PATCH_PREFIX = "modulo.api.routes.schema_folders."
_SCHEMAS_PATCH_PREFIX = "modulo.api.routes.schemas."

_VALID_32 = "b" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FOLDER_ID = uuid.uuid4()
_SCHEMA_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _prevent_db_auth_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent ``_verify_identity`` from connecting to a real database."""
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_folder() -> MagicMock:
    f = MagicMock()
    f.id = _FOLDER_ID
    f.organisation_id = _ORG_ID
    f.name = "Analytics"
    f.parent_id = None
    f.sort_order = 0
    f.account_id = uuid.uuid4()
    f.created_at = _NOW
    f.updated_at = _NOW
    return f


def _make_schema() -> MagicMock:
    s = MagicMock()
    s.id = _SCHEMA_ID
    s.organisation_id = _ORG_ID
    s.name = "User Event"
    s.description = None
    s.abstract_name = None
    s.folder_id = _FOLDER_ID
    s.account_id = uuid.uuid4()
    s.created_by = s.account_id
    s.created_at = _NOW
    s.updated_at = _NOW
    s.deprecated = False
    s.deprecated_at = None
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

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
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


# ---------------------------------------------------------------------------
# Schema folder CRUD round-trip through the real FastAPI routes
# ---------------------------------------------------------------------------


def test_schema_folder_list_round_trip(client: TestClient) -> None:
    folder = _make_folder()
    with patch(f"{_SF_PATCH_PREFIX}list_folders", return_value=[folder]) as mock_list:
        resp = client.get("/api/v1/schema-folders")
    mock_list.assert_called_once()
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["id"] == str(_FOLDER_ID)
    assert body[0]["name"] == "Analytics"


def test_schema_folder_create_round_trip(client: TestClient) -> None:
    folder = _make_folder()
    with patch(f"{_SF_PATCH_PREFIX}create_folder", return_value=folder) as mock_create:
        resp = client.post(
            "/api/v1/schema-folders",
            json={"name": "Analytics", "parent_id": None},
        )
    mock_create.assert_called_once()
    assert resp.status_code == 201
    assert resp.json()["name"] == "Analytics"


def test_schema_folder_rename_round_trip(client: TestClient) -> None:
    folder = _make_folder()
    folder.name = "Data"
    with patch(f"{_SF_PATCH_PREFIX}update_folder", return_value=folder) as mock_update:
        resp = client.patch(f"/api/v1/schema-folders/{_FOLDER_ID}", json={"name": "Data"})
    mock_update.assert_called_once()
    assert resp.status_code == 200
    assert resp.json()["name"] == "Data"


def test_schema_folder_reorder_round_trip(client: TestClient) -> None:
    folder = _make_folder()
    folder.sort_order = 3
    with patch(f"{_SF_PATCH_PREFIX}update_folder", return_value=folder) as mock_update:
        resp = client.patch(f"/api/v1/schema-folders/{_FOLDER_ID}/move", json={"sort_order": 3})
    mock_update.assert_called_once()
    assert resp.status_code == 200
    assert resp.json()["sort_order"] == 3


def test_schema_folder_delete_round_trip(client: TestClient) -> None:
    with patch(f"{_SF_PATCH_PREFIX}delete_folder", return_value=True) as mock_delete:
        resp = client.delete(f"/api/v1/schema-folders/{_FOLDER_ID}")
    mock_delete.assert_called_once()
    assert resp.status_code == 204


def test_schema_folder_delete_missing_returns_404(client: TestClient) -> None:
    with patch(f"{_SF_PATCH_PREFIX}delete_folder", return_value=False):
        resp = client.delete(f"/api/v1/schema-folders/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_schema_folder_update_missing_returns_404(client: TestClient) -> None:
    with patch(f"{_SF_PATCH_PREFIX}update_folder", return_value=None):
        resp = client.patch(f"/api/v1/schema-folders/{uuid.uuid4()}", json={"name": "X"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Schema folder assignment / filtering
# ---------------------------------------------------------------------------


def test_list_schemas_passes_folder_id_filter(client: TestClient) -> None:
    schema = _make_schema()
    page = MagicMock(items=[schema], total=1, page=1, page_size=100)
    with patch(f"{_SCHEMAS_PATCH_PREFIX}list_schemas", return_value=page) as mock_list:
        resp = client.get(f"/api/v1/schemas?folder_id={_FOLDER_ID}")
    assert resp.status_code == 200
    kwargs = mock_list.call_args.kwargs
    assert kwargs["folder_id"] == _FOLDER_ID
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["folder_id"] == str(_FOLDER_ID)


def test_move_schema_to_folder_round_trip(client: TestClient) -> None:
    schema = _make_schema()
    schema.folder_id = None
    with patch(f"{_SCHEMAS_PATCH_PREFIX}move_schema_to_folder", return_value=schema) as mock_move:
        resp = client.patch(f"/api/v1/schemas/{_SCHEMA_ID}/folder", json={"folder_id": str(_FOLDER_ID)})
    mock_move.assert_called_once()
    assert resp.status_code == 200
    assert resp.json()["id"] == str(_SCHEMA_ID)


def test_move_schema_to_folder_unknown_folder_returns_422(client: TestClient) -> None:
    with patch(f"{_SCHEMAS_PATCH_PREFIX}move_schema_to_folder", side_effect=ValueError("Folder not found")):
        resp = client.patch(f"/api/v1/schemas/{_SCHEMA_ID}/folder", json={"folder_id": str(uuid.uuid4())})
    assert resp.status_code == 422


def test_move_schema_to_folder_missing_schema_returns_404(client: TestClient) -> None:
    with patch(f"{_SCHEMAS_PATCH_PREFIX}move_schema_to_folder", return_value=None):
        resp = client.patch(f"/api/v1/schemas/{uuid.uuid4()}/folder", json={"folder_id": None})
    assert resp.status_code == 404
