"""Collapsed error-handling tests.

Replaces 46 individual per-route test files with parametrized test cases.
Each case tests that a route returns the correct status when a DB error
(ProgrammingError, SQLAlchemyError, IntegrityError, Exception) is raised.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.dashboard import _in_memory_cache
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000070")
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000080")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000090")
_EVAL_DEF_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")


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
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _raise_session(exc: Exception) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=exc)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _override_session(client: TestClient, session: AsyncMock) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    client.app.dependency_overrides[get_db_session] = _get_session

    class _MockFactory:
        def __init__(self, s: AsyncMock) -> None:
            self._session = s

        def __call__(self):
            return self

        async def __aenter__(self) -> AsyncMock:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            pass

    client.app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)


def _make_exc(error_type: type) -> Exception:
    if issubclass(error_type, IntegrityError):
        return IntegrityError("stmt", {}, Exception("mock constraint violation"))
    if issubclass(error_type, ProgrammingError):
        return ProgrammingError("stmt", {}, Exception("mock table does not exist"))
    if issubclass(error_type, SQLAlchemyError):
        return SQLAlchemyError("mock", "mock", "mock")
    return error_type("mock unexpected error")


# ── Session-level parametrized tests ───────────────────────────────────────

SESSION_CASES: list[tuple[str, str, str, type, int, dict | None, str | None]] = [
    # Pipelines
    ("pipelines_list_prog", "GET", "/api/v1/pipelines", ProgrammingError, 501, None, "database"),
    ("pipelines_list_sqla", "GET", "/api/v1/pipelines", SQLAlchemyError, 503, None, None),
    ("pipelines_create_prog", "POST", "/api/v1/pipelines", ProgrammingError, 501, {"name": "Test"}, "database"),
    ("pipelines_get_prog", "GET", f"/api/v1/pipelines/{_PIPELINE_ID}", ProgrammingError, 501, None, "database"),
    (
        "pipelines_update_prog",
        "PATCH",
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        ProgrammingError,
        501,
        {"name": "x"},
        "database",
    ),
    ("pipelines_delete_prog", "DELETE", f"/api/v1/pipelines/{_PIPELINE_ID}", ProgrammingError, 501, None, "database"),
    # Runs
    ("runs_list_prog", "GET", "/api/v1/runs", ProgrammingError, 501, None, "database"),
    (
        "runs_trigger_prog",
        "POST",
        "/api/v1/runs",
        ProgrammingError,
        501,
        {"pipeline_id": str(_PIPELINE_ID), "input_payload": {}},
        "database",
    ),
    ("runs_get_prog", "GET", f"/api/v1/runs/{_RUN_ID}", ProgrammingError, 501, None, "database"),
    ("runs_get_sqla", "GET", f"/api/v1/runs/{_RUN_ID}", SQLAlchemyError, 503, None, "database"),
    ("runs_cancel_prog", "POST", f"/api/v1/runs/{_RUN_ID}/cancel", ProgrammingError, 501, None, "database"),
    ("runs_io_prog", "GET", f"/api/v1/runs/{_RUN_ID}/io", ProgrammingError, 501, None, "database"),
    # Stages
    ("stages_list_prog", "GET", "/api/v1/stages", ProgrammingError, 501, None, "database"),
    ("stages_list_sqla", "GET", "/api/v1/stages", SQLAlchemyError, 503, None, "database"),
    ("stages_create_exc", "POST", "/api/v1/stages", ValueError, 500, {"name": "Test Stage"}, "unexpected error"),
    # Evals
    (
        "evals_create_prog",
        "POST",
        "/api/v1/evals",
        ProgrammingError,
        501,
        {"pipeline_id": str(_PIPELINE_ID), "name": "Test", "eval_type": "regex"},
        "database",
    ),
    (
        "evals_create_sqla",
        "POST",
        "/api/v1/evals",
        SQLAlchemyError,
        503,
        {"pipeline_id": str(_PIPELINE_ID), "name": "Test", "eval_type": "regex"},
        "database",
    ),
    ("evals_list_prog", "GET", "/api/v1/evals", ProgrammingError, 501, None, "database"),
    ("evals_list_sqla", "GET", "/api/v1/evals", SQLAlchemyError, 503, None, "database"),
    ("evals_get_prog", "GET", f"/api/v1/evals/{_EVAL_DEF_ID}", ProgrammingError, 501, None, "database"),
    ("evals_get_sqla", "GET", f"/api/v1/evals/{_EVAL_DEF_ID}", SQLAlchemyError, 503, None, "database"),
    ("evals_update_prog", "PUT", f"/api/v1/evals/{_EVAL_DEF_ID}", ProgrammingError, 501, {"name": "x"}, "database"),
    ("evals_update_sqla", "PUT", f"/api/v1/evals/{_EVAL_DEF_ID}", SQLAlchemyError, 503, {"name": "x"}, "database"),
    ("evals_delete_prog", "DELETE", f"/api/v1/evals/{_EVAL_DEF_ID}", ProgrammingError, 501, None, "database"),
    ("evals_delete_sqla", "DELETE", f"/api/v1/evals/{_EVAL_DEF_ID}", SQLAlchemyError, 503, None, "database"),
    # Environments
    ("env_list_prog", "GET", "/api/v1/environments", ProgrammingError, 501, None, "database"),
    ("env_list_sqla", "GET", "/api/v1/environments", SQLAlchemyError, 503, None, "database"),
    (
        "env_create_prog",
        "POST",
        "/api/v1/environments",
        ProgrammingError,
        501,
        {"name": "test", "image_ref": "python:3.12-slim"},
        "database",
    ),
    (
        "env_create_sqla",
        "POST",
        "/api/v1/environments",
        SQLAlchemyError,
        503,
        {"name": "test", "image_ref": "python:3.12-slim"},
        "database",
    ),
    (
        "env_create_integrity",
        "POST",
        "/api/v1/environments",
        IntegrityError,
        409,
        {"name": "test", "image_ref": "python:3.12-slim"},
        "already exists",
    ),
    ("env_get_prog", "GET", f"/api/v1/environments/{_PROFILE_ID}", ProgrammingError, 501, None, "database"),
    ("env_get_sqla", "GET", f"/api/v1/environments/{_PROFILE_ID}", SQLAlchemyError, 503, None, "database"),
    (
        "env_update_prog",
        "PATCH",
        f"/api/v1/environments/{_PROFILE_ID}",
        ProgrammingError,
        501,
        {"name": "x"},
        "database",
    ),
    (
        "env_update_sqla",
        "PATCH",
        f"/api/v1/environments/{_PROFILE_ID}",
        SQLAlchemyError,
        503,
        {"name": "x"},
        "database",
    ),
    (
        "env_update_integrity",
        "PATCH",
        f"/api/v1/environments/{_PROFILE_ID}",
        IntegrityError,
        409,
        {"name": "x"},
        "already exists",
    ),
    ("env_delete_prog", "DELETE", f"/api/v1/environments/{_PROFILE_ID}", ProgrammingError, 501, None, "database"),
    ("env_delete_sqla", "DELETE", f"/api/v1/environments/{_PROFILE_ID}", SQLAlchemyError, 503, None, "database"),
    # Dashboard
    ("dashboard_summary_prog", "GET", "/api/v1/dashboard/summary", ProgrammingError, 501, None, "database"),
    ("dashboard_summary_sqla", "GET", "/api/v1/dashboard/summary", SQLAlchemyError, 503, None, "database"),
    ("dashboard_trends_prog", "GET", "/api/v1/dashboard/trends?days=7", ProgrammingError, 501, None, "database"),
    ("dashboard_trends_sqla", "GET", "/api/v1/dashboard/trends?days=7", SQLAlchemyError, 503, None, "database"),
    (
        "dashboard_daily_prog",
        "GET",
        "/api/v1/dashboard/daily-run-counts?days=7",
        ProgrammingError,
        501,
        None,
        "database",
    ),
    (
        "dashboard_daily_sqla",
        "GET",
        "/api/v1/dashboard/daily-run-counts?days=7",
        SQLAlchemyError,
        503,
        None,
        "database",
    ),
    # Teams
    ("teams_list_prog", "GET", "/api/v1/teams", ProgrammingError, 501, None, "database"),
    ("teams_create_prog", "POST", "/api/v1/teams", ProgrammingError, 501, {"name": "x"}, "database"),
]


class TestSessionLevelErrors:
    """Routes where session.begin() raises."""

    @pytest.mark.parametrize(
        ("test_id", "method", "url", "error_type", "expected_status", "body", "detail_check"),
        SESSION_CASES,
        ids=[c[0] for c in SESSION_CASES],
    )
    def test_session_error(
        self,
        client: TestClient,
        test_id: str,
        method: str,
        url: str,
        error_type: type[Exception],
        expected_status: int,
        body: dict | None,
        detail_check: str | None,
    ) -> None:
        exc = _make_exc(error_type)
        session = _raise_session(exc)
        _override_session(client, session)

        _in_memory_cache.clear()

        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PUT":
            resp = client.put(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        else:
            pytest.fail(f"Unknown method: {method}")

        assert resp.status_code == expected_status
        if detail_check and resp.status_code != 500:
            detail = resp.json().get("detail", "")
            if isinstance(detail, str):
                assert detail_check in detail.lower()


# ── Patch-level parametrized tests ────────────────────────────────────────

PATCH_CASES: list[tuple[str, str, str, type, int, dict | None, str | None, str | None]] = [
    # Schemas
    (
        "schemas_list_prog",
        "GET",
        "/api/v1/schemas",
        ProgrammingError,
        501,
        None,
        "database",
        "modulo.api.routes.schemas.list_schemas",
    ),
    (
        "schemas_list_sqla",
        "GET",
        "/api/v1/schemas",
        SQLAlchemyError,
        503,
        None,
        None,
        "modulo.api.routes.schemas.list_schemas",
    ),
    (
        "schemas_create_prog",
        "POST",
        "/api/v1/schemas",
        ProgrammingError,
        501,
        {"name": "Test", "description": "test"},
        "database",
        "modulo.api.routes.schemas.create_schema",
    ),
    (
        "schemas_create_sqla",
        "POST",
        "/api/v1/schemas",
        SQLAlchemyError,
        503,
        {"name": "Test", "description": "test"},
        None,
        "modulo.api.routes.schemas.create_schema",
    ),
    (
        "schemas_create_integrity",
        "POST",
        "/api/v1/schemas",
        IntegrityError,
        409,
        {"name": "Test", "description": "test"},
        "already exists",
        "modulo.api.routes.schemas.create_schema",
    ),
    (
        "schemas_get_prog",
        "GET",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        ProgrammingError,
        501,
        None,
        "database",
        "modulo.api.routes.schemas.get_schema",
    ),
    (
        "schemas_get_sqla",
        "GET",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        SQLAlchemyError,
        503,
        None,
        None,
        "modulo.api.routes.schemas.get_schema",
    ),
    (
        "schemas_update_prog",
        "PATCH",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        ProgrammingError,
        501,
        {"name": "x"},
        "database",
        "modulo.api.routes.schemas.update_schema",
    ),
    (
        "schemas_update_sqla",
        "PATCH",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        SQLAlchemyError,
        503,
        None,
        None,
        "modulo.api.routes.schemas.update_schema",
    ),
    (
        "schemas_update_integrity",
        "PATCH",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        IntegrityError,
        409,
        {"name": "x"},
        "already exists",
        "modulo.api.routes.schemas.update_schema",
    ),
    (
        "schemas_delete_prog",
        "DELETE",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        ProgrammingError,
        501,
        None,
        "database",
        "modulo.api.routes.schemas.delete_schema",
    ),
    (
        "schemas_delete_sqla",
        "DELETE",
        f"/api/v1/schemas/{_SCHEMA_ID}",
        SQLAlchemyError,
        503,
        None,
        None,
        "modulo.api.routes.schemas.delete_schema",
    ),
    (
        "schemas_deprecate_prog",
        "PATCH",
        f"/api/v1/schemas/{_SCHEMA_ID}/deprecate",
        ProgrammingError,
        501,
        None,
        "database",
        "modulo.api.routes.schemas.deprecate_schema",
    ),
    (
        "schemas_deprecate_sqla",
        "PATCH",
        f"/api/v1/schemas/{_SCHEMA_ID}/deprecate",
        SQLAlchemyError,
        503,
        None,
        None,
        "modulo.api.routes.schemas.deprecate_schema",
    ),
]


class TestPatchLevelErrors:
    """Routes where a CRUD function is patched to raise."""

    @pytest.mark.parametrize(
        ("test_id", "method", "url", "error_type", "expected_status", "body", "detail_check", "mock_target"),
        PATCH_CASES,
        ids=[c[0] for c in PATCH_CASES],
    )
    def test_patch_error(
        self,
        client: TestClient,
        test_id: str,
        method: str,
        url: str,
        error_type: type[Exception],
        expected_status: int,
        body: dict | None,
        detail_check: str | None,
        mock_target: str,
    ) -> None:
        exc = _make_exc(error_type)
        mock_fn = AsyncMock(side_effect=exc)

        with patch(mock_target, mock_fn), patch("modulo.api.routes.schemas.set_rls_org"):
            if method == "GET":
                resp = client.get(url)
            elif method == "POST":
                resp = client.post(url, json=body or {})
            elif method == "PUT":
                resp = client.put(url, json=body or {})
            elif method == "PATCH":
                resp = client.patch(url, json=body or {})
            elif method == "DELETE":
                resp = client.delete(url)

        assert resp.status_code == expected_status
        if detail_check and resp.status_code != 500:
            detail = resp.json().get("detail", "")
            if isinstance(detail, str):
                assert detail_check in detail.lower()
