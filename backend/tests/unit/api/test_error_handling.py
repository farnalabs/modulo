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
from sqlalchemy.sql import Select, Update

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
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
_RECORD_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
_EVENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")


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


@pytest.fixture
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
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
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Raise on the query itself (not session.begin). The require_permission
    # kill-switch read (resolve_authz_enforce) fail-closes on SQLAlchemyError,
    # so the injected failure must surface on the handler's own DB work for
    # the route's ProgrammingError/SQLAlchemyError mapping to be exercised.
    session.execute = AsyncMock(side_effect=exc)
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
    # Feedback system (all 9 routes)
    (
        "feedback_create_prog",
        "POST",
        f"/api/v1/runs/{_RUN_ID}/feedback",
        ProgrammingError,
        501,
        {"gate_id": "gate-1", "rejection_reason": "Wrong output", "rejected_output": {}, "producing_node_id": "n"},
        "database",
    ),
    (
        "feedback_create_sqla",
        "POST",
        f"/api/v1/runs/{_RUN_ID}/feedback",
        SQLAlchemyError,
        503,
        {"gate_id": "gate-1", "rejection_reason": "Wrong output", "rejected_output": {}, "producing_node_id": "n"},
        None,
    ),
    ("feedback_list_prog", "GET", "/api/v1/feedback", ProgrammingError, 501, None, "database"),
    ("feedback_list_sqla", "GET", "/api/v1/feedback", SQLAlchemyError, 503, None, None),
    ("feedback_inbox_prog", "GET", "/api/v1/feedback/inbox", ProgrammingError, 501, None, "database"),
    ("feedback_inbox_sqla", "GET", "/api/v1/feedback/inbox", SQLAlchemyError, 503, None, None),
    ("feedback_proposals_prog", "GET", "/api/v1/feedback/proposals", ProgrammingError, 501, None, "database"),
    ("feedback_proposals_sqla", "GET", "/api/v1/feedback/proposals", SQLAlchemyError, 503, None, None),
    ("feedback_get_prog", "GET", f"/api/v1/feedback/{_RECORD_ID}", ProgrammingError, 501, None, "database"),
    ("feedback_get_sqla", "GET", f"/api/v1/feedback/{_RECORD_ID}", SQLAlchemyError, 503, None, None),
    (
        "feedback_patch_status_prog",
        "PATCH",
        f"/api/v1/feedback/{_RECORD_ID}/status",
        ProgrammingError,
        501,
        {"status": "resolved"},
        "database",
    ),
    (
        "feedback_patch_status_sqla",
        "PATCH",
        f"/api/v1/feedback/{_RECORD_ID}/status",
        SQLAlchemyError,
        503,
        {"status": "resolved"},
        None,
    ),
    (
        "feedback_detect_gap_prog",
        "POST",
        f"/api/v1/feedback/{_RECORD_ID}/detect-gap",
        ProgrammingError,
        501,
        None,
        "database",
    ),
    (
        "feedback_detect_gap_sqla",
        "POST",
        f"/api/v1/feedback/{_RECORD_ID}/detect-gap",
        SQLAlchemyError,
        503,
        None,
        None,
    ),
    (
        "feedback_inbox_item_prog",
        "GET",
        f"/api/v1/feedback/inbox/{_RECORD_ID}",
        ProgrammingError,
        501,
        None,
        "database",
    ),
    (
        "feedback_inbox_item_sqla",
        "GET",
        f"/api/v1/feedback/inbox/{_RECORD_ID}",
        SQLAlchemyError,
        503,
        None,
        None,
    ),
    (
        "feedback_review_prog",
        "POST",
        f"/api/v1/feedback/inbox/{_RECORD_ID}/review",
        ProgrammingError,
        501,
        {"action": "mark_reviewed"},
        "database",
    ),
    (
        "feedback_review_sqla",
        "POST",
        f"/api/v1/feedback/inbox/{_RECORD_ID}/review",
        SQLAlchemyError,
        503,
        {"action": "mark_reviewed"},
        None,
    ),
    (
        "feedback_publish_proposal_prog",
        "POST",
        f"/api/v1/feedback/proposals/{_RECORD_ID}/publish",
        ProgrammingError,
        501,
        {"name": "x", "eval_type": "regex", "config": {"pattern": "42", "field": "answer"}, "node_id": str(_NODE_ID)},
        "database",
    ),
    (
        "feedback_publish_proposal_sqla",
        "POST",
        f"/api/v1/feedback/proposals/{_RECORD_ID}/publish",
        SQLAlchemyError,
        503,
        {"name": "x", "eval_type": "regex", "config": {"pattern": "42", "field": "answer"}, "node_id": str(_NODE_ID)},
        None,
    ),
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
    ("teams_my_prog", "GET", "/api/v1/teams/my", ProgrammingError, 501, None, "database"),
    ("teams_my_sqla", "GET", "/api/v1/teams/my", SQLAlchemyError, 503, None, "database"),
    (
        "teams_reassign_prog",
        "POST",
        f"/api/v1/teams/{_TEAM_ID}/reassign-org",
        ProgrammingError,
        501,
        None,
        "database",
    ),
    (
        "admin_team_reassign_all_prog",
        "POST",
        f"/api/v1/admin/teams/{_TEAM_ID}/reassign-all",
        ProgrammingError,
        501,
        None,
        "database",
    ),
    (
        "teams_reassign_sqla",
        "POST",
        f"/api/v1/teams/{_TEAM_ID}/reassign-org",
        SQLAlchemyError,
        503,
        None,
        "database",
    ),
    (
        "admin_team_reassign_all_sqla",
        "POST",
        f"/api/v1/admin/teams/{_TEAM_ID}/reassign-all",
        SQLAlchemyError,
        503,
        None,
        "database",
    ),
    # Webhook triggers — the receive/replay routes authenticate via
    # get_current_tenant_user_optional (no require_permission kill-switch read),
    # so the injected session failure surfaces on the handler's own DB work.
    # cleanup_expired IS require_permission-gated and is therefore excluded here
    # (its kill-switch read fail-closes before the handler runs).
    (
        "webhook_receive_prog",
        "POST",
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
        ProgrammingError,
        501,
        {"event": "push"},
        "database",
    ),
    (
        "webhook_receive_sqla",
        "POST",
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
        SQLAlchemyError,
        503,
        {"event": "push"},
        None,
    ),
    (
        "webhook_replay_prog",
        "POST",
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{_EVENT_ID}",
        ProgrammingError,
        501,
        {},
        "database",
    ),
    (
        "webhook_replay_sqla",
        "POST",
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{_EVENT_ID}",
        SQLAlchemyError,
        503,
        {},
        None,
    ),
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


def test_update_schema_patch_can_clear_nullable_field(client: TestClient) -> None:
    """PATCH with an explicit null must clear a nullable field, not no-op.

    The update endpoint uses ``model_dump(exclude_unset=True)`` — a key that is
    present in the request with value ``null`` counts as "set" and is applied,
    so setting ``abstract_name`` back to ``None`` is a valid, supported update.
    """
    schema = MagicMock()
    schema.id = _SCHEMA_ID
    schema.organisation_id = _ORG_ID
    schema.name = "inferred-schema"
    schema.description = None
    schema.abstract_name = None
    schema.folder_id = None
    schema.account_id = _USER_ID
    schema.created_at = MagicMock()
    schema.updated_at = MagicMock()
    schema.deprecated = False
    schema.deprecated_at = None

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.update_schema", new_callable=AsyncMock, return_value=schema) as mock_update,
    ):
        resp = client.patch(
            f"/api/v1/schemas/{_SCHEMA_ID}",
            json={"abstract_name": None},
        )

    assert resp.status_code == 200
    mock_update.assert_awaited_once()
    updates = mock_update.await_args.args[2]
    assert updates == {"abstract_name": None}


# ── HITL auth: 401 (unauthenticated) and 403 (insufficient role) ─────────────


@pytest.fixture
def unauth_hitl_client() -> Generator[TestClient, None, None]:
    """Client with NO auth override — unauthenticated requests → 401."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_hitl_client() -> Generator[TestClient, None, None]:
    """Client authenticated as a viewer — HITL claim/approve/reject → 403."""
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
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestHitlAuth:
    """HITL claim/approve/reject endpoints enforce auth: 401 unauth, 403 role."""

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            (f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim", {"expiry_minutes": 15}),
            (f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve", {"claim_token": "tok"}),
            (f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/reject", {"claim_token": "tok", "reason": "no"}),
        ],
        ids=["claim", "approve", "reject"],
    )
    def test_unauthenticated_returns_401(self, unauth_hitl_client: TestClient, path: str, body: dict) -> None:
        resp = unauth_hitl_client.post(path, json=body)
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            (f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim", {"expiry_minutes": 15}),
            (f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve", {"claim_token": "tok"}),
            (f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/reject", {"claim_token": "tok", "reason": "no"}),
        ],
        ids=["claim", "approve", "reject"],
    )
    def test_viewer_returns_403(self, viewer_hitl_client: TestClient, path: str, body: dict) -> None:
        resp = viewer_hitl_client.post(path, json=body)
        assert resp.status_code == 403


# ── Team bulk resource reassignment (PRD §9.3 Team Deletion Policy) ──────────


class TestReassignTeamResources:
    """POST /api/v1/teams/{id}/reassign-org — bulk "Reassign all to org-wide"."""

    URL = f"/api/v1/teams/{_TEAM_ID}/reassign-org"

    def _reassign_session(self, team_exists: bool, rowcounts: list[int]) -> AsyncMock:
        session = _make_mock_session()
        counts = iter(rowcounts)

        async def _execute(stmt, *args: object, **kwargs: object):
            if "authz_enforce" in str(stmt):
                result = MagicMock()
                result.scalar_one_or_none.return_value = None
                return result
            if isinstance(stmt, Select):
                result = MagicMock()
                result.scalar_one_or_none.return_value = MagicMock() if team_exists else None
                return result
            if isinstance(stmt, Update):
                result = MagicMock()
                result.rowcount = next(counts, 0)
                return result
            # RLS set_config TextClause and any other statement: no-op result.
            result = MagicMock()
            result.rowcount = 0
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = AsyncMock(side_effect=_execute)
        return session

    def test_reassign_all_returns_count(self, client: TestClient) -> None:
        """Rows across pipeline/connector/model-backend/library are reassigned."""
        session = self._reassign_session(team_exists=True, rowcounts=[2, 0, 1, 0])
        _override_session(client, session)
        resp = client.post(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["team_id"] == str(_TEAM_ID)
        assert body["reassigned"] == 3

    def test_reassign_is_idempotent(self, client: TestClient) -> None:
        """A second pass finds zero owned rows and reports reassigned=0."""
        session = self._reassign_session(team_exists=True, rowcounts=[0, 0, 0, 0])
        _override_session(client, session)
        resp = client.post(self.URL)
        assert resp.status_code == 200
        assert resp.json()["reassigned"] == 0

    def test_reassign_flips_visibility_to_org(self, client: TestClient) -> None:
        """Every reassign UPDATE must also set visibility='org'.

        The four resource tables carry a check constraint
        ``'visibility = org OR owner_team_id IS NOT NULL'``; clearing
        ``owner_team_id`` without flipping ``visibility`` would violate it and
        the reassignment would fail with a misleading 409.
        """
        updates: list[str] = []
        counts = iter([1, 1, 1, 1])

        async def _execute(stmt, *args: object, **kwargs: object):
            if "authz_enforce" in str(stmt):
                result = MagicMock()
                result.scalar_one_or_none.return_value = None
                return result
            if isinstance(stmt, Select):
                result = MagicMock()
                result.scalar_one_or_none.return_value = MagicMock()
                return result
            if isinstance(stmt, Update):
                updates.append(str(stmt))
                result = MagicMock()
                result.rowcount = next(counts, 0)
                return result
            result = MagicMock()
            result.rowcount = 0
            result.scalar_one_or_none.return_value = None
            return result

        session = _make_mock_session()
        session.execute = AsyncMock(side_effect=_execute)
        _override_session(client, session)
        resp = client.post(self.URL)
        assert resp.status_code == 200
        assert resp.json()["reassigned"] == 4
        assert len(updates) == 4
        for stmt in updates:
            assert "visibility" in stmt
            assert "org" in stmt

    def test_reassign_unknown_team_returns_404(self, client: TestClient) -> None:
        session = self._reassign_session(team_exists=False, rowcounts=[])
        _override_session(client, session)
        resp = client.post(self.URL)
        assert resp.status_code == 404

    def test_reassign_requires_admin(self) -> None:
        session = self._reassign_session(team_exists=True, rowcounts=[1])
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="operator",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )
        app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
            username="operator",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )
        app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
            username="operator",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )
        try:

            async def _session_override() -> AsyncGenerator[AsyncMock, None]:
                yield session

            app.dependency_overrides[get_db_session] = _session_override
            resp = TestClient(app).post(self.URL)
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 403


def test_model_backend_create_persists_health_check_on_save(client: TestClient) -> None:
    """PRD 8.1 - creating a model backend runs a test-inference health check on
    save and persists ``last_health_check_at`` / ``last_health_check_error`` on
    the entity. The create succeeds even when the check reports a failure
    (non-blocking - the graph validator surfaces the recorded error later)."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from cryptography.fernet import Fernet

    from modulo.api.dependencies import get_db_session
    from modulo.db.models.model_backend import ModelBackend
    from modulo.settings import Settings, get_settings

    backend_id = uuid.uuid4()
    now = datetime.now(UTC)
    mb = ModelBackend(
        id=backend_id,
        organisation_id=_ORG_ID,
        name="test-backend",
        display_name="Test Backend",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_USER_ID,
    )
    mb.created_at = now
    mb.updated_at = now
    mb.last_health_check_at = None
    mb.last_health_check_error = None

    mock_session = _make_mock_session()
    dup_result = MagicMock()
    dup_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = dup_result

    async def _fake_get(entity_cls: object, identity: object) -> object:
        if entity_cls is ModelBackend:
            return mb
        return MagicMock()

    mock_session.get = AsyncMock(side_effect=_fake_get)

    async def override_session() -> AsyncMock:
        yield mock_session

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=Fernet.generate_key().decode(),
        modulo_admin_password="testpass",
    )

    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=mb),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.api.routes.model_backends.create_secrets_backend", return_value=AsyncMock()),
        patch(
            "modulo.api.routes.model_backends._run_health_check_on_save",
            new=AsyncMock(return_value=("unhealthy", "401 Incorrect API key provided")),
        ),
    ):
        client.app.dependency_overrides[get_settings] = lambda: settings
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "test-backend",
                "display_name": "Test Backend",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_key": "sk-test",
            },
        )

    assert resp.status_code == 201
    assert mb.last_health_check_at is not None
    assert mb.last_health_check_error == "401 Incorrect API key provided"


def test_model_backend_update_persists_health_check_on_key_rotation(client: TestClient) -> None:
    """PRD 8.1 - PATCHing a new API key re-runs the health check on save and
    records the post-rotation result on the entity."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from cryptography.fernet import Fernet

    from modulo.api.dependencies import get_db_session
    from modulo.db.models.model_backend import ModelBackend
    from modulo.settings import Settings, get_settings

    backend_id = uuid.uuid4()
    now = datetime.now(UTC)
    mb = ModelBackend(
        id=backend_id,
        organisation_id=_ORG_ID,
        name="test-backend",
        display_name="Test Backend",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_USER_ID,
    )
    mb.created_at = now
    mb.updated_at = now
    mb.last_health_check_at = None
    mb.last_health_check_error = None

    mock_session = _make_mock_session()
    mock_session.execute.return_value = MagicMock()

    async def _fake_get(entity_cls: object, identity: object) -> object:
        if entity_cls is ModelBackend:
            return mb
        return MagicMock()

    mock_session.get = AsyncMock(side_effect=_fake_get)

    async def override_session() -> AsyncMock:
        yield mock_session

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=Fernet.generate_key().decode(),
        modulo_admin_password="testpass",
    )

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=mb),
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=mb),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.api.routes.model_backends.create_secrets_backend", return_value=AsyncMock()),
        patch(
            "modulo.api.routes.model_backends._run_health_check_on_save",
            new=AsyncMock(return_value=("unhealthy", "429 rate limit exceeded")),
        ),
    ):
        client.app.dependency_overrides[get_settings] = lambda: settings
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/model-backends/{backend_id}",
            json={"api_key": "sk-rotated"},
        )

    assert resp.status_code == 200
    assert mb.last_health_check_at is not None
    assert mb.last_health_check_error == "429 rate limit exceeded"


async def test_run_health_check_on_save_ok() -> None:
    """A provider that responds OK maps to ('ok', None)."""
    from modulo.api.routes.model_backends import _run_health_check_on_save
    from modulo.model_backends.base import HealthResult

    fake = AsyncMock()
    fake.health_check = AsyncMock(return_value=HealthResult(ok=True))
    with patch("modulo.api.routes.model_backends._build_backend", return_value=fake):
        status_, detail = await _run_health_check_on_save("openai", "gpt-4o", "sk-test", {})

    assert status_ == "ok"
    assert detail is None


async def test_run_health_check_on_save_unhealthy() -> None:
    """A provider-reported failure (auth / quota / status code) maps to
    ('unhealthy', <provider detail>)."""
    from modulo.api.routes.model_backends import _run_health_check_on_save
    from modulo.model_backends.base import HealthResult

    fake = AsyncMock()
    fake.health_check = AsyncMock(return_value=HealthResult(ok=False, detail="401 Incorrect API key provided"))
    with patch("modulo.api.routes.model_backends._build_backend", return_value=fake):
        status_, detail = await _run_health_check_on_save("openai", "gpt-4o", "sk-test", {})

    assert status_ == "unhealthy"
    assert detail == "401 Incorrect API key provided"


async def test_run_health_check_on_save_not_applicable_when_build_fails() -> None:
    """A provider the REST API cannot construct from an api_key alone (vertexai
    needs 'project', bedrock aws keys, watsonx 'project_id', azure 'azure_endpoint')
    maps to ('not_applicable', None) — NOT a health failure, so the graph
    validator must never surface MODEL_BACKEND_UNHEALTHY for it."""
    from modulo.api.routes.model_backends import _run_health_check_on_save

    def _raise(*args: object, **kwargs: object) -> object:
        raise ValueError("Missing 'project' in credentials for provider 'vertexai'")

    with patch("modulo.api.routes.model_backends._build_backend", side_effect=_raise):
        status_, detail = await _run_health_check_on_save("vertexai", "gemini-pro", "sk-test", {})

    assert status_ == "not_applicable"
    assert detail is None


async def test_run_health_check_on_save_exception_maps_unhealthy() -> None:
    """An exception raised by the provider health check (network error, timeout,
    provider SDK failure) maps to ('unhealthy', <truncated message>)."""
    from modulo.api.routes.model_backends import _run_health_check_on_save

    fake = AsyncMock()
    fake.health_check = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("modulo.api.routes.model_backends._build_backend", return_value=fake):
        status_, detail = await _run_health_check_on_save("openai", "gpt-4o", "sk-test", {})

    assert status_ == "unhealthy"
    assert detail == "boom"


def test_model_backend_recheck_health_persists_result(client: TestClient) -> None:
    """POST /api/v1/model-backends/{id}/health-check re-runs the check against
    the stored credential and persists the result — clearing a sticky error that
    previously could only be cleared by PATCHing a new API key."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from cryptography.fernet import Fernet

    from modulo.api.dependencies import get_db_session
    from modulo.db.models.model_backend import ModelBackend
    from modulo.settings import Settings, get_settings

    backend_id = uuid.uuid4()
    now = datetime.now(UTC)
    mb = ModelBackend(
        id=backend_id,
        organisation_id=_ORG_ID,
        name="test-backend",
        display_name="Test Backend",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_USER_ID,
    )
    mb.created_at = now
    mb.updated_at = now
    mb.last_health_check_at = now
    mb.last_health_check_error = "Health check timed out"

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=Fernet.generate_key().decode(),
        modulo_admin_password="testpass",
    )
    mb.credentials_ciphertext = Fernet(settings.fernet_key.encode()).encrypt(b"sk-test")

    mock_session = _make_mock_session()
    mock_session.get = AsyncMock(return_value=mb)

    async def override_session() -> AsyncMock:
        yield mock_session

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", new=AsyncMock(return_value=mb)),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch(
            "modulo.api.routes.model_backends._run_health_check_on_save",
            new=AsyncMock(return_value=("ok", None)),
        ) as mock_health,
    ):
        client.app.dependency_overrides[get_settings] = lambda: settings
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/model-backends/{backend_id}/health-check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["detail"] is None
    assert body["checked_at"] is not None
    assert mb.last_health_check_error is None
    assert mb.last_health_check_at is not None
    assert mock_health.await_args.args[2] == "sk-test"


def test_model_backend_recheck_health_clears_not_applicable_error(client: TestClient) -> None:
    """A provider the API cannot construct re-runs to 'not_applicable', which
    clears the sticky error and unblocks runs (never MODEL_BACKEND_UNHEALTHY)."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from cryptography.fernet import Fernet

    from modulo.api.dependencies import get_db_session
    from modulo.db.models.model_backend import ModelBackend
    from modulo.settings import Settings, get_settings

    backend_id = uuid.uuid4()
    now = datetime.now(UTC)
    mb = ModelBackend(
        id=backend_id,
        organisation_id=_ORG_ID,
        name="test-backend",
        display_name="Test Backend",
        provider="vertexai",
        model_id="gemini-pro",
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_USER_ID,
    )
    mb.created_at = now
    mb.updated_at = now
    mb.last_health_check_at = now
    mb.last_health_check_error = "Missing 'project' in credentials for provider 'vertexai'"

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=Fernet.generate_key().decode(),
        modulo_admin_password="testpass",
    )

    mock_session = _make_mock_session()
    mock_session.get = AsyncMock(return_value=mb)

    async def override_session() -> AsyncMock:
        yield mock_session

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", new=AsyncMock(return_value=mb)),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch(
            "modulo.api.routes.model_backends._run_health_check_on_save",
            new=AsyncMock(return_value=("not_applicable", None)),
        ),
    ):
        client.app.dependency_overrides[get_settings] = lambda: settings
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/model-backends/{backend_id}/health-check")

    assert resp.status_code == 200
    assert resp.json()["status"] == "not_applicable"
    assert mb.last_health_check_error is None


def test_model_backend_recheck_health_404_when_missing(client: TestClient) -> None:
    """Re-checking a backend that does not exist returns 404 before any check."""
    from unittest.mock import AsyncMock, patch

    from modulo.api.dependencies import get_db_session
    from modulo.settings import Settings, get_settings

    mock_session = _make_mock_session()

    async def override_session() -> AsyncMock:
        yield mock_session

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        client.app.dependency_overrides[get_settings] = lambda: settings
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/model-backends/{uuid.uuid4()}/health-check")

    assert resp.status_code == 404


def test_delete_model_backend_blocked_by_non_terminal_run_snapshot(client: TestClient) -> None:
    """PRD §8.1 deletion protection — a backend pinned by a PipelineSnapshot tied
    to a non-terminal run cannot be hard-deleted (the in-flight run may still
    resolve the pinned backend, e.g. after a HITL pause/resume)."""
    from unittest.mock import AsyncMock, patch

    from modulo.api.dependencies import get_db_session

    backend_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    mock_session = _make_mock_session()

    async def override_session() -> AsyncMock:
        yield mock_session

    with (
        patch(
            "modulo.api.routes.model_backends.list_backends_referencing_fallback",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "modulo.api.routes.model_backends._list_snapshots_referencing_backend",
            new=AsyncMock(return_value=[{"snapshot_id": snapshot_id, "pipeline_id": uuid.uuid4()}]),
        ),
        patch("modulo.api.routes.model_backends.delete_model_backend") as mock_delete,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/model-backends/{backend_id}")

    assert resp.status_code == 409
    assert "non-terminal run" in resp.json()["detail"]
    assert str(snapshot_id) in resp.json()["detail"]
    mock_delete.assert_not_awaited()


def test_delete_model_backend_allowed_when_snapshot_runs_terminal(client: TestClient) -> None:
    """A backend whose only referencing snapshots are tied to terminal runs
    hard-deletes normally (204) — deletion protection is scoped to in-flight runs."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from modulo.api.dependencies import get_db_session
    from modulo.db.models.model_backend import ModelBackend

    backend_id = uuid.uuid4()
    now = datetime.now(UTC)
    mb = ModelBackend(
        id=backend_id,
        organisation_id=_ORG_ID,
        name="test-backend",
        display_name="Test Backend",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_USER_ID,
    )
    mb.created_at = now
    mb.updated_at = now

    mock_session = _make_mock_session()

    async def override_session() -> AsyncMock:
        yield mock_session

    with (
        patch(
            "modulo.api.routes.model_backends.list_backends_referencing_fallback",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "modulo.api.routes.model_backends._list_snapshots_referencing_backend",
            new=AsyncMock(return_value=[]),
        ),
        patch("modulo.api.routes.model_backends.get_model_backend", new=AsyncMock(return_value=mb)),
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=True),
        patch(
            "modulo.api.routes.model_backends.append_audit_event_isolated",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/model-backends/{backend_id}")

    assert resp.status_code == 204


class TestTriggerPaginationValidation:
    """Query-param bounds on the trigger listing endpoints.

    FastAPI ``Query(ge=...)`` / ``Query(le=...)`` reject out-of-range values
    with 422 *before* the handler runs, so the mocked DB session is never
    touched on these paths.
    """

    def test_list_triggers_page_below_one_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/triggers?page=0")
        assert resp.status_code == 422

    def test_list_triggers_page_size_zero_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/triggers?page_size=0")
        assert resp.status_code == 422

    def test_list_triggers_page_size_above_100_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/triggers?page_size=101")
        assert resp.status_code == 422

    def test_list_trigger_events_limit_above_100_returns_422(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/events?limit=101")
        assert resp.status_code == 422

    def test_list_trigger_events_limit_below_one_returns_422(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/events?limit=0")
        assert resp.status_code == 422


class TestCronTimezoneValidation:
    """Cron timezone validation: the validator returns an error string for an
    invalid IANA timezone, and the API surfaces it as 422.

    ``validate_cron_expression`` is the shared validator used by both
    ``create_trigger`` and ``update_cron_config`` via ``_validated_next_fire``,
    which raises HTTPException(422) on any non-None error string.
    """

    def test_invalid_timezone_returns_error_string(self) -> None:
        from modulo.core.cron_helpers import validate_cron_expression

        error = validate_cron_expression("0 0 * * *", timezone="Not/AZone")
        assert error is not None
        assert "Invalid timezone" in error

    def test_valid_timezone_returns_none(self) -> None:
        from modulo.core.cron_helpers import validate_cron_expression

        assert validate_cron_expression("0 0 * * *", timezone="America/New_York") is None

    def test_invalid_timezone_on_cron_config_update_returns_422(self, client: TestClient) -> None:
        # Stub the trigger select so the handler reaches _validated_next_fire,
        # which maps the invalid-timezone error string to HTTP 422.
        trigger = MagicMock()
        trigger.id = _TRIGGER_ID
        trigger.organisation_id = _ORG_ID
        trigger.trigger_type = "cron"
        trigger.cron_expression = "0 0 * * *"
        trigger.cron_timezone = "Not/AZone"
        trigger.deleted_at = None

        session = _make_mock_session()
        result = MagicMock()
        result.scalar_one_or_none.return_value = trigger
        session.execute = AsyncMock(return_value=result)

        with (
            patch("modulo.api.routes.triggers.set_rls_org"),
            patch("modulo.api.routes.triggers.compute_next_fire") as mock_next,
        ):
            _override_session(client, session)
            resp = client.patch(
                f"/api/v1/triggers/{_TRIGGER_ID}/cron",
                json={"cron_timezone": "Not/AZone", "cron_expression": "0 0 * * *"},
            )

        assert resp.status_code == 422
        mock_next.assert_not_called()


class TestRunRetentionValidation:
    """Run-retention config must be bounded — a 0-day or >365-day retention is rejected."""

    @pytest.mark.parametrize("retention_days", [0, -1, 366, 1000])
    def test_out_of_range_retention_days_returns_422(self, client: TestClient, retention_days: int) -> None:
        resp = client.put("/api/v1/admin/runs/retention", json={"retention_days": retention_days})
        assert resp.status_code == 422

    def test_valid_retention_days_accepted_by_validation(self) -> None:
        from modulo.api.routes.admin import UpdateRetentionRequest

        for days in (7, 90, 365):
            req = UpdateRetentionRequest(retention_days=days)
            assert req.retention_days == days
