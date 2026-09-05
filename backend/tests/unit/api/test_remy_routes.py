"""Unit tests for /api/v1/remy endpoints (sessions, messages, stream, UI commands).

Unit tier: no DB — the SQLAlchemy session is a contract-correct AsyncMock,
RLS helpers and CRUD functions are patched at the route-module boundary, and
the model backend is never constructed (StubModelBackend policy).
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import remy as remy_routes
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.models.remy_message import ChatMessage
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


def _principal(*, is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
    )


def _build_client(principal: AuthenticatedPrincipal) -> tuple[TestClient, AsyncMock]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    test_client = TestClient(app)
    test_client.mock_session = mock_session  # type: ignore[attr-defined]
    return test_client, mock_session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_client, _ = _build_client(_principal())
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def system_admin_client() -> Generator[TestClient, None, None]:
    test_client, _ = _build_client(_principal(is_system_admin=True))
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_remy_global_state() -> Generator[None, None, None]:
    yield
    remy_routes._pending_permissions.clear()
    remy_routes._permission_decisions.clear()
    remy_routes._pending_ui_results.clear()
    remy_routes._ui_command_results.clear()
    remy_routes._resume_events.clear()
    remy_routes._session_approvals.clear()
    remy_routes._account_sessions.clear()
    remy_routes._rate_limiters.clear()


def _owned_chat_session(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.account_id = _USER_ID
    s.organisation_id = _ORG_ID
    s.name = "My Session"
    s.session_number = 3
    s.provider = "openai"
    s.model = "gpt-4o"
    s.context_window_tokens = 200000
    s.system_prompt_hash = None
    s.created_at = _NOW
    s.updated_at = _NOW
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _stub_owned_session(mock_session: AsyncMock, chat_session: MagicMock | None) -> None:
    mock_session.get = AsyncMock(return_value=chat_session)


def _ui_driving_enabled(enabled: bool) -> MagicMock:
    flag_registry = MagicMock()
    flag_registry.resolve_flag = AsyncMock(return_value=enabled)
    return flag_registry


# ---------------------------------------------------------------------------
# GET /sessions — list
# ---------------------------------------------------------------------------


def test_list_sessions_returns_paged_items(client: TestClient) -> None:
    s1 = _owned_chat_session()
    total_result = MagicMock()
    total_result.scalar.return_value = 5
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [s1]
    count_rows = [SimpleNamespace(session_id=s1.id, cnt=7)]
    _queue_executes(client.mock_session, total_result, list_result, count_rows)  # type: ignore[attr-defined]

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/remy/sessions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert len(body["items"]) == 1
    assert body["items"][0]["message_count"] == 7
    assert body["items"][0]["name"] == "My Session"


def test_list_sessions_db_error_returns_503(client: TestClient) -> None:
    _queue_executes(client.mock_session)  # type: ignore[attr-defined]
    with (
        patch(
            "modulo.api.routes.remy.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("boom"),
        ),
    ):
        resp = client.get("/api/v1/remy/sessions")
    assert resp.status_code == 503


def test_list_sessions_missing_table_returns_501(client: TestClient) -> None:
    _queue_executes(client.mock_session)  # type: ignore[attr-defined]
    programming_error = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with (
        patch(
            "modulo.api.routes.remy.set_rls_org",
            new_callable=AsyncMock,
            side_effect=programming_error,
        ),
    ):
        resp = client.get("/api/v1/remy/sessions")
    assert resp.status_code == 501


def test_list_sessions_unexpected_error_returns_500(client: TestClient) -> None:
    _queue_executes(client.mock_session)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock, side_effect=RuntimeError("kaboom")),
    ):
        resp = client.get("/api/v1/remy/sessions")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /sessions — create
# ---------------------------------------------------------------------------


def test_create_session_returns_201_with_next_number(client: TestClient) -> None:
    max_sn_result = MagicMock()
    max_sn_result.scalar.return_value = 4
    _queue_executes(client.mock_session, max_sn_result)  # type: ignore[attr-defined]

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/remy/sessions",
            json={"provider": "openai", "model": "gpt-4o", "context_window_tokens": 200000, "name": "New"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "New"
    assert body["provider"] == "openai"
    assert body["session_number"] == 5


def test_create_session_context_window_below_minimum_rejected(client: TestClient) -> None:
    resp = client.post("/api/v1/remy/sessions", json={"context_window_tokens": 10})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET / PATCH / DELETE /sessions/{id}
# ---------------------------------------------------------------------------


def test_get_session_returns_message_count(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    count_result = MagicMock()
    count_result.scalar.return_value = 9
    _queue_executes(client.mock_session, count_result)  # type: ignore[attr-defined]

    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.get(f"/api/v1/remy/sessions/{chat_session.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["message_count"] == 9


def test_get_session_foreign_session_returns_404(client: TestClient) -> None:
    foreign = _owned_chat_session(account_id=uuid.uuid4())
    _stub_owned_session(client.mock_session, foreign)  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.get(f"/api/v1/remy/sessions/{foreign.id}")
    assert resp.status_code == 404


def test_get_session_missing_returns_404(client: TestClient) -> None:
    _stub_owned_session(client.mock_session, None)  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.get(f"/api/v1/remy/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_rename_session_updates_name(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]

    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.patch(f"/api/v1/remy/sessions/{chat_session.id}", json={"name": "Renamed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"


def test_rename_session_blank_name_rejected(client: TestClient) -> None:
    resp = client.patch(f"/api/v1/remy/sessions/{uuid.uuid4()}", json={"name": ""})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_delete_session_removes_in_memory_state(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    sid = str(chat_session.id)
    remy_routes._session_approvals[sid] = {"click": {"page_path": "/", "expires_at": _NOW}}
    remy_routes._rate_limiters[sid] = remy_routes.ActionRateLimiter()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]

    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.delete(f"/api/v1/remy/sessions/{chat_session.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"
    assert sid not in remy_routes._session_approvals
    assert sid not in remy_routes._rate_limiters


# ---------------------------------------------------------------------------
# GET / POST /sessions/{id}/messages
# ---------------------------------------------------------------------------


def test_list_messages_returns_serialised_items(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    total_result = MagicMock()
    total_result.scalar.return_value = 2
    msg = ChatMessage(
        organisation_id=_ORG_ID,
        session_id=chat_session.id,
        role="user",
        content="hello",
    )
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [msg]
    _queue_executes(client.mock_session, total_result, list_result)  # type: ignore[attr-defined]

    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.get(f"/api/v1/remy/sessions/{chat_session.id}/messages")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["role"] == "user"
    assert body["items"][0]["content"] == "hello"


def test_append_message_returns_201(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]

    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/messages",
            json={"role": "user", "content": "hi there"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "user"
    assert body["content"] == "hi there"


def test_append_message_invalid_role_rejected(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/remy/sessions/{uuid.uuid4()}/messages",
        json={"role": "wizard", "content": "hi"},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# POST /sessions/{id}/stream
# ---------------------------------------------------------------------------


def test_stream_session_not_found_returns_404(client: TestClient) -> None:
    _stub_owned_session(client.mock_session, None)  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.post(
            f"/api/v1/remy/sessions/{uuid.uuid4()}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 404


def test_stream_db_error_returns_503(client: TestClient) -> None:
    client.mock_session.get = AsyncMock(side_effect=SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.post(
            f"/api/v1/remy/sessions/{uuid.uuid4()}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 503


def test_stream_missing_provider_rejected(client: TestClient) -> None:
    resp = client.post(f"/api/v1/remy/sessions/{uuid.uuid4()}/stream", json={"content": "hi"})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_stream_returns_sse_error_event_when_init_fails(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    fake_db = AsyncMock()
    init = remy_routes._StreamInit(error_detail="No active openai API key configured")
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.AsyncSession", return_value=fake_db),
        patch("modulo.api.routes.remy._initialise_stream", new_callable=AsyncMock, return_value=init),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: error" in resp.text
    assert "No active openai API key configured" in resp.text


def test_stream_emits_done_event_with_message_id(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    fake_db = AsyncMock()
    init = remy_routes._StreamInit(backend=MagicMock(), parent_msg_id=uuid.uuid4(), messages=[])

    async def fake_loop(ctx: Any, request: Any, backend: Any, messages: Any, parent: Any, state: Any) -> Any:
        state["msg_id"] = "msg-123"
        yield 'event: token\ndata: {"token": "hi"}\n\n'

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.AsyncSession", return_value=fake_db),
        patch("modulo.api.routes.remy._initialise_stream", new_callable=AsyncMock, return_value=init),
        patch("modulo.api.routes.remy._run_stream_loop", fake_loop),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 200, resp.text
    assert "event: done" in resp.text
    assert "msg-123" in resp.text


# ---------------------------------------------------------------------------
# POST /sessions/{id}/permission-response
# ---------------------------------------------------------------------------


def test_permission_response_rejected_when_ui_driving_disabled(client: TestClient) -> None:
    with patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(False)):
        resp = client.post(
            f"/api/v1/remy/sessions/{uuid.uuid4()}/permission-response",
            json={"request_id": str(uuid.uuid4()), "action": "approve"},
        )
    assert resp.status_code == 403
    assert "UI driving is disabled" in resp.json()["detail"]


def test_permission_response_unknown_request_returns_404(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/permission-response",
            json={"request_id": str(uuid.uuid4()), "action": "approve"},
        )
    assert resp.status_code == 404


def test_permission_response_records_decision_and_sets_event(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    remy_routes._pending_permissions[request_id] = (event, str(chat_session.id))
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/permission-response",
            json={"request_id": request_id, "action": "approve"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert event.is_set()
    assert remy_routes._permission_decisions[request_id] == {"action": "approve"}


def test_permission_response_foreign_session_request_returns_403(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    request_id = str(uuid.uuid4())
    remy_routes._pending_permissions[request_id] = (asyncio.Event(), str(uuid.uuid4()))
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/permission-response",
            json={"request_id": request_id, "action": "approve"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /sessions/{id}/ui-command-results, reset-permissions, resume, stop
# ---------------------------------------------------------------------------


def test_ui_command_results_no_pending_event_returns_ok(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/ui-command-results",
            json={"results": [{"id": "1", "name": "click", "success": True}]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_ui_command_results_disabled_ui_driving_returns_403(client: TestClient) -> None:
    with patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(False)):
        resp = client.post(
            f"/api/v1/remy/sessions/{uuid.uuid4()}/ui-command-results",
            json={"results": [{"id": "1", "name": "click", "success": True}]},
        )
    assert resp.status_code == 403


def test_reset_permissions_clears_approvals_and_rate_limiter(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    sid = str(chat_session.id)
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    remy_routes._session_approvals[sid] = {"click": {"page_path": "/", "expires_at": _NOW}}
    remy_routes._rate_limiters[sid] = remy_routes.ActionRateLimiter()
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/reset-permissions")
    assert resp.status_code == 200, resp.text
    assert sid not in remy_routes._session_approvals
    assert sid not in remy_routes._rate_limiters


def test_resume_session_sets_resume_event(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    sid = str(chat_session.id)
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    event = asyncio.Event()
    remy_routes._resume_events[sid] = event
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/resume")
    assert resp.status_code == 200, resp.text
    assert event.is_set()


def test_stop_session_wakes_pending_ui_event(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    sid = str(chat_session.id)
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    event = asyncio.Event()
    remy_routes._pending_ui_results[sid] = event
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stopped"
    assert event.is_set()
    assert sid not in remy_routes._pending_ui_results
    assert len(remy_routes._ui_command_results[sid]) == 1


# ---------------------------------------------------------------------------
# GET /sessions/{id}/audit-trail (system admin only)
# ---------------------------------------------------------------------------


def test_audit_trail_forbidden_for_non_system_admin(client: TestClient) -> None:
    resp = client.get(f"/api/v1/remy/sessions/{uuid.uuid4()}/audit-trail")
    assert resp.status_code == 403


def test_audit_trail_returns_entries_for_system_admin(system_admin_client: TestClient) -> None:
    msg = MagicMock()
    msg.created_at = _NOW
    msg.tool_results_json = {
        "tool_name": "click",
        "success": True,
        "result": {"args": {"selector": "#btn"}, "snapshotBefore": {"url": "https://example.com"}},
    }
    result = MagicMock()
    result.scalars.return_value.all.return_value = [msg]
    _queue_executes(system_admin_client.mock_session, result)  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = system_admin_client.get(f"/api/v1/remy/sessions/{uuid.uuid4()}/audit-trail")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "click"
    assert items[0]["url"] == "https://example.com"
    assert items[0]["success"] is True


# ---------------------------------------------------------------------------
# POST /sessions/{id}/undo
# ---------------------------------------------------------------------------


def test_undo_no_previous_action(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    _queue_executes(client.mock_session, result)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/undo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_action"


def test_undo_navigate_derives_go_back(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    last = MagicMock()
    last.tool_results_json = {"tool_name": "navigate", "result": {"args": {"path": "/pipelines"}}}
    result = MagicMock()
    result.scalar_one_or_none.return_value = last
    _queue_executes(client.mock_session, result)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/undo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "found"
    assert body["undo_action"]["name"] == "go_back"


def test_undo_non_invertible_action(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    last = MagicMock()
    last.tool_results_json = {"tool_name": "screenshot", "result": {"args": {}}}
    result = MagicMock()
    result.scalar_one_or_none.return_value = last
    _queue_executes(client.mock_session, result)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/undo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_inverse"


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions — direct unit tests)
# ---------------------------------------------------------------------------


def _msg(role: str, content: str | None, **extra: Any) -> ChatMessage:
    return ChatMessage(
        organisation_id=_ORG_ID,
        session_id=uuid.uuid4(),
        role=role,
        content=content,
        **extra,
    )


def test_message_to_langchain_all_roles() -> None:
    human = remy_routes._message_to_langchain(_msg("user", "hi"))
    assert isinstance(human, HumanMessage)
    assistant = remy_routes._message_to_langchain(_msg("assistant", "yo"))
    assert isinstance(assistant, AIMessage)
    tool_use = remy_routes._message_to_langchain(
        _msg("tool_use", "", tool_calls_json={"tool_calls": [{"name": "click", "args": {}, "id": "t1"}]})
    )
    assert isinstance(tool_use, AIMessage)
    assert tool_use.tool_calls[0]["name"] == "click"
    tool_result = remy_routes._message_to_langchain(
        _msg("tool_result", "ok", tool_results_json={"tool_call_id": "tc-1"})
    )
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.tool_call_id == "tc-1"
    summary = remy_routes._message_to_langchain(_msg("summary", "so far"))
    assert isinstance(summary, SystemMessage)
    unknown = remy_routes._message_to_langchain(_msg("mystery", "?"))
    assert isinstance(unknown, HumanMessage)


def test_build_backend_unsupported_provider_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        remy_routes._build_backend("not-a-provider", "m", "key")
    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


def test_action_rate_limiter_blocks_after_max_actions() -> None:
    limiter = remy_routes.ActionRateLimiter(max_actions=2, window_seconds=60)
    assert limiter.check() is True
    assert limiter.check() is True
    assert limiter.check() is False


def test_reconstruct_tool_calls_parses_and_tolerates_bad_json() -> None:
    buffers = {1: {"id": "a", "name": "click", "args": '{"selector": "#x"}'}}
    calls = remy_routes._reconstruct_tool_calls(buffers)
    assert calls == [{"id": "a", "name": "click", "args": {"selector": "#x"}}]
    broken = {0: {"id": "b", "name": "fill", "args": "{not json"}}
    calls = remy_routes._reconstruct_tool_calls(broken)
    assert calls == [{"id": "b", "name": "fill", "args": {}}]


def test_prune_context_window_trims_oldest_middle_messages() -> None:
    messages = [HumanMessage(content="x" * 400) for _ in range(5)]
    pruned = remy_routes._prune_context_window(messages, 500)
    assert pruned == 1
    assert len(messages) == 4


def test_accumulate_tool_call_chunks_merges_argument_fragments() -> None:
    buffers: dict[int, dict[str, Any]] = {}
    first = {"index": 0, "id": "t1", "name": "click", "args": '{"sel'}
    second = {"index": 0, "id": None, "name": None, "args": 'ector": "#a"}'}
    other = {"index": 1, "id": "t2", "name": "fill", "args": "{}"}
    remy_routes._accumulate_tool_call_chunks(
        AIMessageChunk(content="", tool_call_chunks=[first, other]),  # type: ignore[arg-type]
        buffers,
    )
    remy_routes._accumulate_tool_call_chunks(
        AIMessageChunk(content="", tool_call_chunks=[second]),  # type: ignore[arg-type]
        buffers,
    )
    assert buffers[0]["args"] == '{"selector": "#a"}'
    assert buffers[1]["name"] == "fill"


def test_check_nogo_matches_page_and_selector_patterns() -> None:
    assert remy_routes._check_nogo("navigate", {}, "/admin/billing") is True
    assert remy_routes._check_nogo("click", {"selector": "#delete-org"}, "/pipelines") is True
    assert remy_routes._check_nogo("extract", {"selector": "#x"}, "/pipelines") is False


def test_tool_allowlist_disabled_outside_allowlists() -> None:
    from modulo.core.remy.config_service import RemyConfig

    config = RemyConfig(allowed_selectors=["#safe"], allowed_page_patterns=["/reports"])
    assert remy_routes._tool_allowlist_disabled(config, "click", {"selector": "#danger"}) is True
    assert remy_routes._tool_allowlist_disabled(config, "click", {"selector": "#safe-btn"}) is False
    assert remy_routes._tool_allowlist_disabled(config, "navigate", {"path": "/admin"}) is True
    assert remy_routes._tool_allowlist_disabled(config, "navigate", {"path": "/reports/1"}) is False


def test_default_tool_permission_by_mode() -> None:
    from modulo.core.remy.config_service import RemyConfig

    locked = RemyConfig(permission_mode="locked_down")
    assert remy_routes._default_tool_permission(locked, "click", {}) == "requires_approval"
    assert remy_routes._default_tool_permission(locked, "navigate", {}) == "always_allowed"
    full_auto = RemyConfig(permission_mode="full_auto", auto_execute_threshold=0.8)
    assert remy_routes._default_tool_permission(full_auto, "click", {"confidence": 0.5}) == "requires_approval"
    assert remy_routes._default_tool_permission(full_auto, "click", {"confidence": 0.9}) == "always_allowed"
    assert remy_routes._default_tool_permission(full_auto, "click", {"confidence": "high"}) == "always_allowed"
    standard = RemyConfig(permission_mode="standard")
    assert remy_routes._default_tool_permission(standard, "press", {}) == "requires_approval"


def test_resolve_tool_permission_overrides_and_destructive_patterns() -> None:
    from modulo.core.remy.config_service import RemyConfig

    override = RemyConfig(permission_mode="locked_down", tool_permissions={"click": "always_allowed"})
    assert remy_routes._resolve_tool_permission(override, "click", {}) == "always_allowed"
    destructive = RemyConfig(permission_mode="standard")
    assert (
        remy_routes._resolve_tool_permission(destructive, "click", {"selector": "#delete-account-btn"}, "")
        == "requires_approval"
    )


def test_clear_session_approvals_for_account_scopes_to_account() -> None:
    other_session = str(uuid.uuid4())
    remy_routes._account_sessions["acct-1"] = {"s-1"}
    remy_routes._session_approvals["s-1"] = {"click": {"page_path": "/", "expires_at": _NOW}}
    remy_routes._session_approvals[other_session] = {"fill": {"page_path": "/", "expires_at": _NOW}}
    remy_routes.clear_session_approvals_for_account("acct-1")
    assert "s-1" not in remy_routes._session_approvals
    assert other_session in remy_routes._session_approvals


def test_get_all_tool_definitions_includes_and_excludes_ui_tools() -> None:
    with patch("modulo.api.routes.remy.get_mcp_tool_definitions", return_value=[{"type": "function"}]):
        with_ui = remy_routes._get_all_tool_definitions(include_ui_tools=True)
        without_ui = remy_routes._get_all_tool_definitions(include_ui_tools=False)
    assert len(with_ui) == len(remy_routes._UI_TOOLS) + 1
    assert without_ui == [{"type": "function"}]


def test_split_permission_payload_and_merge_results() -> None:
    pending = [{"id": "t1", "name": "click", "args": {"selector": "#a"}}]
    payload, req_id = remy_routes._build_permission_request_payload(pending)
    assert payload["request_id"] == req_id
    assert payload["tools"][0]["name"] == "click"
    merged = remy_routes._merge_ui_command_results(
        pending,
        [{"name": "click", "success": True, "result": {"ok": 1}}],
    )
    assert merged[0]["tool_call_id"] == "t1"
    assert merged[0]["success"] is True
    assert remy_routes._tool_result_content({"result": {"a": 1}}) == json.dumps({"a": 1})


def test_resolve_stream_api_key_uses_explicit_override() -> None:
    principal = TenantPrincipal(
        username="u",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", api_key="sk-explicit")
    ctx = remy_routes._StreamContext(AsyncMock(), principal, uuid.uuid4(), req, _make_settings(), MagicMock())
    api_key, error = asyncio.run(remy_routes._resolve_stream_api_key(ctx))
    assert api_key == "sk-explicit"
    assert error is None


def test_resolve_stream_api_key_error_when_no_backend_configured() -> None:
    principal = TenantPrincipal(
        username="u",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    mock_session = _make_mock_session()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    _queue_executes(mock_session, no_row)
    ctx = remy_routes._StreamContext(mock_session, principal, uuid.uuid4(), req, _make_settings(), MagicMock())
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        api_key, error = asyncio.run(remy_routes._resolve_stream_api_key(ctx))
    assert api_key is None
    assert error is not None
    assert "No active openai API key" in error


def test_build_stream_backend_rejects_unsupported_provider() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="bogus", model="m")
    backend, error = remy_routes._build_stream_backend(req, "key")
    assert backend is None
    assert error is not None
    assert "Unsupported provider" in error


# ---------------------------------------------------------------------------
# Async helper coverage (direct unit tests via asyncio.run)
# ---------------------------------------------------------------------------


def _principal_obj() -> TenantPrincipal:
    return TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _stream_ctx(
    *,
    req: remy_routes.StreamRequest | None = None,
    db_session: AsyncMock | None = None,
    chat_session: MagicMock | None = None,
) -> remy_routes._StreamContext:
    if req is None:
        req = remy_routes.StreamRequest(content="hello", provider="openai", model="gpt-4o", api_key="sk")
    if db_session is None:
        db_session = _make_mock_session()
    return remy_routes._StreamContext(
        db_session,
        _principal_obj(),
        uuid.uuid4(),
        req,
        _make_settings(),
        chat_session or MagicMock(),
    )


def _ctx_with_id_assigning_flush() -> remy_routes._StreamContext:
    ctx = _stream_ctx()
    added: list[Any] = []
    ctx.db_session.add = MagicMock(side_effect=added.append)

    async def _assign_ids() -> None:
        for obj in added:
            if isinstance(obj, ChatMessage) and obj.id is None:
                obj.id = uuid.uuid4()

    ctx.db_session.flush = AsyncMock(side_effect=_assign_ids)
    return ctx


def _collect(gen: AsyncGenerator[str, None]) -> list[str]:
    async def _run() -> list[str]:
        out = []
        async for event in gen:
            out.append(event)
        return out

    return asyncio.run(_run())


def test_build_backend_supported_provider_uses_lazy_import() -> None:
    fake_module = MagicMock()
    fake_backend_cls = MagicMock()
    fake_module.OpenAIBackend = fake_backend_cls
    with patch("modulo.api.routes.remy.importlib.import_module", return_value=fake_module):
        backend = remy_routes._build_backend("openai", "gpt-4o", "sk")
    assert backend is fake_backend_cls.return_value
    fake_backend_cls.assert_called_once_with(api_key="sk", model_id="gpt-4o")


def test_resolve_api_key_decrypts_stored_credentials() -> None:
    ctx_session = _make_mock_session()
    row = MagicMock()
    row.scalar_one_or_none.return_value = MagicMock(credentials_ciphertext=b"cipher")
    ctx_session.execute = AsyncMock(return_value=row)
    with patch(
        "modulo.api.routes.remy.decode_stored_secret_scoped",
        new_callable=AsyncMock,
        return_value="decrypted-key",
    ):
        key = asyncio.run(remy_routes._resolve_api_key("openai", _ORG_ID, ctx_session, _VALID_32))
    assert key == "decrypted-key"


def test_resolve_api_key_decrypt_failure_returns_none() -> None:
    ctx_session = _make_mock_session()
    row = MagicMock()
    row.scalar_one_or_none.return_value = MagicMock(credentials_ciphertext=b"cipher")
    ctx_session.execute = AsyncMock(return_value=row)
    with patch(
        "modulo.api.routes.remy.decode_stored_secret_scoped",
        new_callable=AsyncMock,
        side_effect=ValueError("bad cipher"),
    ):
        key = asyncio.run(remy_routes._resolve_api_key("openai", _ORG_ID, ctx_session, _VALID_32))
    assert key is None


def test_call_mcp_tool_success() -> None:
    client_cm = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True}
    client_cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    with patch("modulo.api.routes.remy.pinned_async_client", new_callable=AsyncMock, return_value=client_cm):
        result = asyncio.run(remy_routes._call_mcp_tool("click", {"selector": "#a"}, "mcp-key", "http://base"))
    assert result == {"ok": True}


def test_call_mcp_tool_retries_on_429_then_succeeds() -> None:
    client_cm = AsyncMock()
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers.get.return_value = "0"
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {"ok": True}
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=[resp_429, resp_ok])
    with (
        patch("modulo.api.routes.remy.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.remy.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = asyncio.run(remy_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert result == {"ok": True}


def test_call_mcp_tool_exhausts_retries_on_timeouts() -> None:
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    with (
        patch("modulo.api.routes.remy.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.remy.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(remy_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert exc_info.value.status_code == 502


def test_call_mcp_tool_rejects_non_object_response() -> None:
    client_cm = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = ["not-an-object"]
    client_cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    with (
        patch("modulo.api.routes.remy.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        pytest.raises(ValueError, match="non-object"),
    ):
        asyncio.run(remy_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))


def test_reconstruct_messages_converts_rows() -> None:
    db = _make_mock_session()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        _msg("user", "hi"),
        _msg("assistant", "hello"),
    ]
    db.execute = AsyncMock(return_value=result)
    messages = asyncio.run(remy_routes._reconstruct_messages(db, uuid.uuid4()))
    assert len(messages) == 2
    assert isinstance(messages[0], HumanMessage)
    assert isinstance(messages[1], AIMessage)


def test_is_ui_driving_enabled_defaults_true_on_registry_failure() -> None:
    failing_registry = MagicMock()
    failing_registry.resolve_flag = AsyncMock(side_effect=RuntimeError("registry down"))
    with patch("modulo.api.routes.remy.get_registry", return_value=failing_registry):
        enabled = asyncio.run(remy_routes._is_ui_driving_enabled(_ORG_ID))
    assert enabled is True


def test_is_set_and_clear_session_approvals_in_memory() -> None:
    sid = str(uuid.uuid4())

    async def _scenario() -> None:
        assert await remy_routes._is_approved_for_session(sid, "click", "/reports") is False
        await remy_routes._set_session_approval(sid, "click", "/reports")
        assert await remy_routes._is_approved_for_session(sid, "click", "/reports") is True
        assert await remy_routes._is_approved_for_session(sid, "click", "/other") is False
        await remy_routes._clear_session_approvals(sid)
        assert await remy_routes._is_approved_for_session(sid, "click", "/reports") is False

    asyncio.run(_scenario())


def test_is_approved_for_session_expires_stale_approvals() -> None:
    sid = str(uuid.uuid4())
    remy_routes._session_approvals[sid] = {
        "click": {"page_path": "/reports", "expires_at": datetime.now(UTC) - timedelta(minutes=1)}
    }

    async def _scenario() -> None:
        assert await remy_routes._is_approved_for_session(sid, "click", "/reports") is False

    asyncio.run(_scenario())
    assert sid not in remy_routes._session_approvals


def test_await_permission_decision_approve_for_session() -> None:
    sid = str(uuid.uuid4())
    pending = [{"id": "t1", "name": "click", "args": {}}]
    preset_event = asyncio.Event()
    preset_event.set()
    remy_routes._permission_decisions["req-1"] = {"action": "approve_for_session"}

    async def _scenario() -> list[dict[str, Any]]:
        with patch("modulo.api.routes.remy.asyncio.Event", return_value=preset_event):
            return await remy_routes._await_permission_decision(None, sid, "req-1", pending, "/reports")

    approved = asyncio.run(_scenario())
    assert approved == pending
    assert "click" in remy_routes._session_approvals[sid]


def test_await_permission_decision_reject_returns_empty() -> None:
    sid = str(uuid.uuid4())
    preset_event = asyncio.Event()
    preset_event.set()
    remy_routes._permission_decisions["req-2"] = {"action": "reject"}

    async def _scenario() -> list[dict[str, Any]]:
        with patch("modulo.api.routes.remy.asyncio.Event", return_value=preset_event):
            return await remy_routes._await_permission_decision(None, sid, "req-2", [], "/reports")

    approved = asyncio.run(_scenario())
    assert not approved


def test_await_permission_decision_registry_branch() -> None:
    registry = MagicMock()
    registry.subscribe_permission_response = AsyncMock(return_value={"action": "approve"})
    registry.set_permission_request = AsyncMock()
    approved = asyncio.run(
        remy_routes._await_permission_decision(registry, "sid", "req-3", [{"id": "t", "name": "n", "args": {}}], "/")
    )
    assert len(approved) == 1
    registry.set_permission_request.assert_awaited_once()


def test_wait_for_ui_command_results_in_memory() -> None:
    sid = str(uuid.uuid4())
    event = asyncio.Event()
    event.set()
    remy_routes._ui_command_results[sid] = [{"name": "click", "success": True}]
    results = asyncio.run(remy_routes._wait_for_ui_command_results(None, sid, event))
    assert results == [{"name": "click", "success": True}]


def test_wait_for_ui_command_results_registry_branch() -> None:
    registry = MagicMock()
    registry.subscribe_ui_results = AsyncMock(return_value=True)
    registry.get_and_clear_ui_command_results = AsyncMock(return_value=[{"name": "fill"}])
    results = asyncio.run(remy_routes._wait_for_ui_command_results(registry, "sid", asyncio.Event()))
    assert results == [{"name": "fill"}]


def test_wait_for_stream_resume_in_memory_with_event() -> None:
    sid = str(uuid.uuid4())
    event = asyncio.Event()
    event.set()
    remy_routes._resume_events[sid] = event
    asyncio.run(remy_routes._wait_for_stream_resume(None, sid))
    assert remy_routes._resume_events.get(sid) is event


def test_wait_for_stream_resume_registry_branch() -> None:
    registry = MagicMock()
    registry.subscribe_resume = AsyncMock()
    asyncio.run(remy_routes._wait_for_stream_resume(registry, "sid"))
    registry.subscribe_resume.assert_awaited_once()


def test_run_mcp_tool_calls_without_key_reports_error() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    calls = [{"id": "t1", "name": "search", "args": {}}]
    events = _collect(remy_routes._run_mcp_tool_calls(calls, req, "http://base"))
    assert len(events) == 1
    assert events[0]["success"] is False
    assert "MCP API key" in events[0]["error"]


def test_run_mcp_tool_calls_with_key_executes_and_reports_failures() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", mcp_api_key="mcp-key")
    calls = [{"id": "t1", "name": "search", "args": {}}]
    with patch(
        "modulo.api.routes.remy._call_mcp_tool",
        new_callable=AsyncMock,
        return_value={"result": "found"},
    ):
        events = _collect(remy_routes._run_mcp_tool_calls(calls, req, "http://base"))
    assert events[0]["success"] is True

    with patch("modulo.api.routes.remy._call_mcp_tool", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        events = _collect(remy_routes._run_mcp_tool_calls(calls, req, "http://base"))
    assert events[0]["success"] is False
    assert "RuntimeError" in events[0]["error"]


def test_run_mcp_tool_calls_reraises_http_exception() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", mcp_api_key="mcp-key")
    calls = [{"id": "t1", "name": "search", "args": {}}]
    with (
        patch(
            "modulo.api.routes.remy._call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=502, detail="gateway"),
        ),
        pytest.raises(HTTPException),
    ):
        _collect(remy_routes._run_mcp_tool_calls(calls, req, "http://base"))


def test_run_manifest_calls_with_path_returns_route() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    manifest = {"routes": {"/runs": {"name": "Runs"}}, "elements": {"/runs": [{"tag": "table"}]}}
    calls = [{"id": "t1", "name": "get_manifest", "args": {"path": "/runs"}}]
    with patch("modulo.core.manifest.get_manifest", return_value=manifest):
        events = _collect(remy_routes._run_manifest_calls(calls, req))
    assert events[0]["success"] is True
    assert events[0]["result"]["route"] == {"name": "Runs"}


def test_run_manifest_calls_without_path_returns_summary() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    manifest = {
        "routes": {"/runs": {"name": "Runs", "testid": "runs", "type": "page", "sidebar_group": "BUILD"}},
        "elements": {"/runs": []},
        "sidebar_groups": {"BUILD": {}},
    }
    calls = [{"id": "t1", "name": "get_manifest", "args": {}}]
    with patch("modulo.core.manifest.get_manifest", return_value=manifest):
        events = _collect(remy_routes._run_manifest_calls(calls, req))
    assert events[0]["result"]["routes"]["/runs"]["name"] == "Runs"


def test_run_manifest_calls_excluded_ui_tools_reports_error() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", exclude_ui_tools=True)
    calls = [{"id": "t1", "name": "get_manifest", "args": {}}]
    events = _collect(remy_routes._run_manifest_calls(calls, req))
    assert events[0]["success"] is False


def test_classify_ui_tool_permissions_buckets() -> None:
    from modulo.core.remy.config_service import RemyConfig

    config = RemyConfig(permission_mode="standard")
    sid = str(uuid.uuid4())
    ui_calls = [
        {"id": "t1", "name": "navigate", "args": {}},
        {"id": "t2", "name": "press", "args": {}},
        {"id": "t3", "name": "get_manifest", "args": {}},
    ]

    async def _scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return await remy_routes._classify_ui_tool_permissions(config, ui_calls, "/reports", sid)

    approved, pending = asyncio.run(_scenario())
    assert [tc["id"] for tc in approved] == ["t1", "t3"]
    assert [tc["id"] for tc in pending] == ["t2"]


def test_classify_ui_tool_permissions_skips_disabled() -> None:
    from modulo.core.remy.config_service import RemyConfig

    config = RemyConfig(permission_mode="full_auto", allowed_page_patterns=["/reports"])
    ui_calls = [{"id": "t1", "name": "navigate", "args": {"path": "/admin/billing"}}]

    async def _scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return await remy_routes._classify_ui_tool_permissions(config, ui_calls, "/admin/billing", "sid")

    approved, pending = asyncio.run(_scenario())
    assert not approved
    assert not pending


def test_save_and_add_assistant_messages() -> None:
    ctx = _ctx_with_id_assigning_flush()
    parent_id = asyncio.run(remy_routes._save_stream_user_message(ctx))
    assert parent_id is not None

    msg_id = asyncio.run(remy_routes._add_assistant_message(ctx, "answer text", None, parent_id))
    assert uuid.UUID(msg_id)


def test_finalise_stream_assistant_message_skips_naming_when_named() -> None:
    ctx = _ctx_with_id_assigning_flush()
    ctx.chat_session.name = "Already Named"
    msg_id = asyncio.run(remy_routes._finalise_stream_assistant_message(ctx, "answer", None))
    assert uuid.UUID(msg_id)


def test_auto_name_stream_session_first_message() -> None:
    ctx_db = _make_mock_session()
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    ctx_db.execute = AsyncMock(return_value=count_result)
    chat_session = MagicMock()
    chat_session.name = None
    req = remy_routes.StreamRequest(content="x" * 50, provider="openai", model="gpt-4o", context_window_tokens=200000)
    asyncio.run(remy_routes._auto_name_stream_session(ctx_db, uuid.uuid4(), req, chat_session))
    stmt = ctx_db.execute.await_args_list[-1].args[0]
    assert stmt is not None


def test_auto_name_stream_session_skips_named_session() -> None:
    ctx_db = _make_mock_session()
    ctx_db.execute = AsyncMock()
    chat_session = MagicMock()
    chat_session.name = "Named"
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    asyncio.run(remy_routes._auto_name_stream_session(ctx_db, uuid.uuid4(), req, chat_session))
    ctx_db.execute.assert_not_awaited()


def test_auto_name_stream_session_ten_message_summary() -> None:
    ctx_db = _make_mock_session()
    count_result = MagicMock()
    count_result.scalar.return_value = 10
    first_msg_result = MagicMock()
    first_msg_result.scalar.return_value = "First user message"
    ctx_db.execute = AsyncMock(side_effect=[count_result, first_msg_result])
    chat_session = MagicMock()
    chat_session.name = None
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    asyncio.run(remy_routes._auto_name_stream_session(ctx_db, uuid.uuid4(), req, chat_session))
    assert ctx_db.execute.await_count == 3


def test_build_stream_tools_param_gates_on_support_and_flags() -> None:
    ctx = _stream_ctx()
    no_tools_backend = MagicMock(spec=["stream"])
    assert asyncio.run(remy_routes._build_stream_tools_param(no_tools_backend, ctx)) is None

    tools_backend = MagicMock()
    tools_backend.supports_tools = True
    with (
        patch("modulo.api.routes.remy.build_tool_registry", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.get_mcp_tool_definitions", return_value=[]),
        patch("modulo.api.routes.remy._is_ui_driving_enabled", new_callable=AsyncMock, return_value=False),
    ):
        tools = asyncio.run(remy_routes._build_stream_tools_param(tools_backend, ctx))
    assert not tools

    exclude_req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", exclude_ui_tools=True)
    ctx_exclude = _stream_ctx(req=exclude_req)
    with (
        patch("modulo.api.routes.remy.build_tool_registry", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.get_mcp_tool_definitions", return_value=[]),
        patch("modulo.api.routes.remy._is_ui_driving_enabled", new_callable=AsyncMock, return_value=True),
    ):
        tools = asyncio.run(remy_routes._build_stream_tools_param(tools_backend, ctx_exclude))
    assert not tools


def test_build_stream_system_prompt_uses_skill_loader() -> None:
    ctx = _stream_ctx()
    loader = MagicMock()
    loader.build_system_prompt = AsyncMock(return_value="System prompt text")
    with patch("modulo.api.routes.remy.SkillLoader", return_value=loader):
        prompt = asyncio.run(remy_routes._build_stream_system_prompt(ctx, supports_tools=True))
    assert prompt == "System prompt text"
    loader.build_system_prompt.assert_awaited_once_with(
        org_id=_ORG_ID,
        user_id=_USER_ID,
        page_context=None,
        system_prompt_override=None,
        include_ui_tools_text=False,
    )


def test_initialise_stream_full_flow() -> None:
    db = _make_mock_session()
    ctx = _stream_ctx(db_session=db)
    added: list[Any] = []
    ctx.db_session.add = MagicMock(side_effect=added.append)
    user_msg_id = uuid.uuid4()

    async def _assign_ids() -> None:
        for obj in added:
            if isinstance(obj, ChatMessage) and obj.id is None:
                obj.id = user_msg_id

    ctx.db_session.flush = AsyncMock(side_effect=_assign_ids)
    reconstruct_result = MagicMock()
    reconstruct_result.scalars.return_value.all.return_value = [_msg("user", "earlier")]
    db.execute = AsyncMock(return_value=reconstruct_result)

    stub_backend = MagicMock()
    stub_backend.supports_tools = False
    loader = MagicMock()
    loader.build_system_prompt = AsyncMock(return_value="SP")

    with (
        patch("modulo.api.routes.remy._build_backend", return_value=stub_backend),
        patch("modulo.api.routes.remy.SkillLoader", return_value=loader),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        init = asyncio.run(remy_routes._initialise_stream(ctx))

    assert init.error_detail is None
    assert init.backend is stub_backend
    assert init.parent_msg_id == user_msg_id
    assert isinstance(init.messages[0], SystemMessage)
    assert init.messages[0].content == "SP"
    assert isinstance(init.messages[-1], HumanMessage)


def test_initialise_stream_reports_key_error() -> None:
    ctx = _stream_ctx()
    ctx.req.api_key = None
    with patch(
        "modulo.api.routes.remy._resolve_stream_api_key",
        new_callable=AsyncMock,
        return_value=(None, "No active openai API key configured."),
    ):
        init = asyncio.run(remy_routes._initialise_stream(ctx))
    assert init.error_detail == "No active openai API key configured."


def test_initialise_stream_reports_backend_error() -> None:
    ctx = _stream_ctx()
    with (
        patch("modulo.api.routes.remy._resolve_stream_api_key", new_callable=AsyncMock, return_value=("sk", None)),
        patch(
            "modulo.api.routes.remy._build_stream_backend",
            return_value=(None, "Failed to initialize backend: boom"),
        ),
    ):
        init = asyncio.run(remy_routes._initialise_stream(ctx))
    assert init.error_detail == "Failed to initialize backend: boom"


def test_stream_approved_commands_empty_calls_is_noop() -> None:
    flow = remy_routes._UiToolFlow()
    events = _collect(remy_routes._stream_approved_commands(_stream_ctx(), MagicMock(), [], [], flow))
    assert not events
    assert flow.should_break is False


def test_stream_approved_commands_rate_limited() -> None:
    from modulo.core.remy.config_service import RemyConfig

    ctx = _stream_ctx()
    sid = ctx.session_id_str
    exhausted = remy_routes.ActionRateLimiter(max_actions=1, window_seconds=60)
    exhausted.check()
    remy_routes._rate_limiters[sid] = exhausted
    flow = remy_routes._UiToolFlow()
    config = RemyConfig()
    events = _collect(
        remy_routes._stream_approved_commands(ctx, config, [{"id": "t1", "name": "click", "args": {}}], [], flow)
    )
    assert flow.should_break is True
    assert "Rate limited" in events[0]


def test_stream_approved_commands_executes_and_merges_results() -> None:
    from modulo.core.remy.config_service import RemyConfig

    ctx = _stream_ctx()
    flow = remy_routes._UiToolFlow()
    results = [{"name": "click", "success": True, "result": {"clicked": True}}]
    with patch(
        "modulo.api.routes.remy._wait_for_ui_command_results",
        new_callable=AsyncMock,
        return_value=results,
    ):
        events = _collect(
            remy_routes._stream_approved_commands(
                ctx,
                RemyConfig(),
                [{"id": "t1", "name": "click", "args": {}}],
                [],
                flow,
            )
        )
    assert flow.should_break is False
    assert "event: ui_command_batch" in events[0]
    assert "event: tool_call" in events[1]


def test_stream_approved_commands_user_cancel_breaks_loop() -> None:
    from modulo.core.remy.config_service import RemyConfig

    ctx = _stream_ctx()
    flow = remy_routes._UiToolFlow()
    tool_results: list[dict[str, Any]] = []
    results = [{"name": "click", "success": False, "error": "cancelled_by_user"}]
    with patch(
        "modulo.api.routes.remy._wait_for_ui_command_results",
        new_callable=AsyncMock,
        return_value=results,
    ):
        events = _collect(
            remy_routes._stream_approved_commands(
                ctx,
                RemyConfig(),
                [{"id": "t1", "name": "click", "args": {}}],
                tool_results,
                flow,
            )
        )
    assert flow.should_break is True
    assert "event: abort_summary" in events[-1]


def test_stream_ui_tool_flow_reports_excluded_ui_tools() -> None:
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", exclude_ui_tools=True)
    ctx = _stream_ctx(req=req)
    flow = remy_routes._UiToolFlow()
    tool_results: list[dict[str, Any]] = []
    events = _collect(
        remy_routes._stream_ui_tool_flow(ctx, [{"id": "t1", "name": "click", "args": {}}], tool_results, flow)
    )
    payload = json.loads(events[0].split("data: ")[1])
    assert payload["success"] is False


def test_stream_ui_tool_flow_reports_disabled_flag() -> None:
    ctx = _stream_ctx()
    flow = remy_routes._UiToolFlow()
    tool_results: list[dict[str, Any]] = []
    with patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(False)):
        events = _collect(
            remy_routes._stream_ui_tool_flow(ctx, [{"id": "t1", "name": "click", "args": {}}], tool_results, flow)
        )
    payload = json.loads(events[0].split("data: ")[1])
    assert "UI driving is disabled" in payload["error"]


def test_stream_ui_tool_flow_happy_path_waits_for_resume_when_paused() -> None:
    from modulo.core.remy.config_service import RemyConfig

    ctx = _stream_ctx()
    ctx.chat_session.name = "Named"
    remy_routes._resume_events[ctx.session_id_str] = asyncio.Event()
    remy_routes._resume_events[ctx.session_id_str].set()
    config = RemyConfig()
    config_service = MagicMock()
    config_service.get_config = AsyncMock(return_value=config)
    flow = remy_routes._UiToolFlow()
    tool_results: list[dict[str, Any]] = []
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy.RemyConfigService", return_value=config_service),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.remy._classify_ui_tool_permissions",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch("modulo.api.routes.remy._wait_for_stream_resume", new_callable=AsyncMock),
    ):
        events = _collect(remy_routes._stream_ui_tool_flow(ctx, [], tool_results, flow))
    assert "event: paused" in events[0]
    assert flow.should_break is False


def test_stream_backend_tokens_accumulates_and_tracks_content() -> None:
    backend = MagicMock()

    async def _fake_stream(messages: Any, tools: Any = None) -> AsyncGenerator[Any, None]:
        yield AIMessageChunk(content="hello ")
        yield AIMessageChunk(content="world")

    backend.stream = _fake_stream
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    state: dict[str, Any] = {}
    buffers: dict[int, dict[str, Any]] = {}
    events = _collect(remy_routes._stream_backend_tokens(backend, [], None, request, buffers, state))
    assert len(events) == 2
    assert state["full_content"] == "hello world"


def test_stream_backend_tokens_stops_on_disconnect() -> None:
    backend = MagicMock()

    async def _fake_stream(messages: Any, tools: Any = None) -> AsyncGenerator[Any, None]:
        yield AIMessageChunk(content="hello ")
        yield AIMessageChunk(content="world")

    backend.stream = _fake_stream
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)
    state: dict[str, Any] = {}
    events = _collect(remy_routes._stream_backend_tokens(backend, [], None, request, {}, state))
    assert not events
    assert state["disconnected"] is True


def test_stream_tool_events_splits_tool_families() -> None:
    ctx = _stream_ctx()
    state: dict[str, Any] = {}

    async def _mcp_gen(*_a: Any, **_k: Any) -> AsyncGenerator[dict[str, Any], None]:
        yield {"tool_call_id": "m1", "tool_name": "search", "success": True}

    async def _manifest_gen(*_a: Any, **_k: Any) -> AsyncGenerator[dict[str, Any], None]:
        yield {"tool_call_id": "g1", "tool_name": "get_manifest", "success": True}

    async def _ui_gen(*_a: Any, **_k: Any) -> AsyncGenerator[str, None]:
        yield "event: ui_command_batch\ndata: {}\n\n"

    tool_results: list[dict[str, Any]] = []
    with (
        patch("modulo.api.routes.remy._run_mcp_tool_calls", _mcp_gen),
        patch("modulo.api.routes.remy._run_manifest_calls", _manifest_gen),
        patch("modulo.api.routes.remy._stream_ui_tool_flow", _ui_gen),
    ):
        events = _collect(
            remy_routes._stream_tool_events(
                [{"id": "u1", "name": "click", "args": {}}],
                [{"id": "m1", "name": "search", "args": {}}],
                ctx,
                tool_results,
                state,
            )
        )
    assert len(events) == 3
    assert len(tool_results) == 2


def test_stream_tool_records_should_break_from_ui_flow() -> None:
    ctx = _stream_ctx()
    state: dict[str, Any] = {}
    flow_flag = {"broken": True}

    async def _ui_gen(_ctx: Any, _calls: Any, _results: Any, flow: Any) -> AsyncGenerator[str, None]:
        flow.should_break = flow_flag["broken"]
        return
        yield  # pragma: no cover - makes this an async generator

    with (
        patch("modulo.api.routes.remy._run_mcp_tool_calls", _empty_gen),
        patch("modulo.api.routes.remy._run_manifest_calls", _empty_gen),
        patch("modulo.api.routes.remy._stream_ui_tool_flow", _ui_gen),
    ):
        events = _collect(
            remy_routes._stream_tool_events([{"id": "u1", "name": "click", "args": {}}], [], ctx, [], state)
        )
    assert not events
    assert state["should_break"] is True


async def _empty_gen(*_a: Any, **_k: Any) -> AsyncGenerator[dict[str, Any], None]:
    return
    yield  # pragma: no cover - makes this an async generator


def test_append_turn_messages_extends_conversation() -> None:
    messages: list[Any] = [HumanMessage(content="hi")]
    remy_routes._append_turn_messages(
        messages,
        "response",
        [{"id": "t1", "name": "click", "args": {}}],
        [{"tool_call_id": "t1", "result": {"ok": 1}}],
    )
    assert len(messages) == 3
    assert isinstance(messages[1], AIMessage)
    assert isinstance(messages[2], ToolMessage)


def test_ping_event_if_due_respects_interval() -> None:
    now = time.monotonic()
    state = {"last_ping_at": now - 20}
    ping = remy_routes._ping_event_if_due(state)
    assert ping == "event: ping\ndata: {}\n\n"
    assert remy_routes._ping_event_if_due(state) is None


def test_run_stream_loop_finalises_when_no_tool_calls() -> None:
    ctx = _stream_ctx()

    async def _token_gen(*_a: Any, **_k: Any) -> AsyncGenerator[str, None]:
        yield 'event: token\ndata: {"token": "hi"}\n\n'

    finalise = AsyncMock(return_value="msg-1")
    state: dict[str, Any] = {
        "disconnected": False,
        "full_content": "hi",
        "should_break": False,
        "msg_id": None,
        "last_ping_at": time.monotonic(),
    }
    with (
        patch("modulo.api.routes.remy._build_stream_tools_param", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.remy._stream_backend_tokens", _token_gen),
        patch("modulo.api.routes.remy._finalise_stream_assistant_message", finalise),
    ):
        events = _collect(remy_routes._run_stream_loop(ctx, MagicMock(), MagicMock(), [], None, state))
    assert len(events) == 1
    assert state["msg_id"] == "msg-1"
    finalise.assert_awaited_once()


def test_run_stream_loop_executes_tools_then_finalises() -> None:
    ctx = _stream_ctx()

    async def _token_gen(*_a: Any, **_k: Any) -> AsyncGenerator[str, None]:
        yield "event: token\ndata: {}\n\n"
        yield "event: token\ndata: {}\n\n"

    async def _tool_gen(*_a: Any, **_k: Any) -> AsyncGenerator[str, None]:
        yield "event: tool_call\ndata: {}\n\n"

    persist = AsyncMock(return_value="msg-2")
    finalise = AsyncMock(return_value="msg-3")
    state: dict[str, Any] = {
        "disconnected": False,
        "full_content": "hi",
        "should_break": False,
        "msg_id": None,
        "last_ping_at": time.monotonic() - 30,
    }
    tool_calls = [{"id": "t1", "name": "search", "args": {}}]
    with (
        patch("modulo.api.routes.remy._build_stream_tools_param", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.remy._stream_backend_tokens", _token_gen),
        patch("modulo.api.routes.remy._reconstruct_tool_calls", side_effect=[tool_calls, []]),
        patch("modulo.api.routes.remy._stream_tool_events", _tool_gen),
        patch("modulo.api.routes.remy._persist_assistant_and_tool_messages", persist),
        patch("modulo.api.routes.remy._finalise_stream_assistant_message", finalise),
    ):
        events = _collect(remy_routes._run_stream_loop(ctx, MagicMock(), MagicMock(), [], None, state))
    assert len(events) == 6
    assert state["msg_id"] == "msg-3"
    persist.assert_awaited_once()
    assert any("event: ping" in event for event in events)


def test_stream_event_generator_error_handlers() -> None:
    fake_db = AsyncMock()
    req = remy_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")

    async def _scenario(side_effect: Exception) -> list[str]:
        with (
            patch("modulo.api.routes.remy.AsyncSession", return_value=fake_db),
            patch("modulo.api.routes.remy._initialise_stream", new_callable=AsyncMock, side_effect=side_effect),
        ):
            events: list[str] = []
            gen = remy_routes._stream_event_generator(
                _make_mock_session(), MagicMock(), _principal_obj(), uuid.uuid4(), req, _make_settings(), MagicMock()
            )
            async for event in gen:
                events.append(event)
            return events

    http_events = asyncio.run(_scenario(HTTPException(status_code=400, detail="bad request")))
    assert "bad request" in http_events[0]

    db_events = asyncio.run(_scenario(SQLAlchemyError("db down")))
    assert remy_routes._MSG_DATABASE_ERROR_PLEASE_TRY in db_events[0]

    unexpected_events = asyncio.run(_scenario(RuntimeError("kaboom")))
    assert "unexpected error" in unexpected_events[0]


# ---------------------------------------------------------------------------
# Route-level error mapping (503 paths) and registry branches
# ---------------------------------------------------------------------------


def _failing_executes(session: AsyncMock, exc: Exception) -> None:
    async def _execute(stmt: object, *_a: object, **_k: object) -> Any:
        if "authz_enforce" in str(stmt):
            benign = MagicMock()
            benign.scalar_one_or_none.return_value = None
            return benign
        raise exc

    session.execute = AsyncMock(side_effect=_execute)


def _owned_session_or_error(mock_session: AsyncMock, exc: Exception | None, found: MagicMock | None = None) -> None:
    mock_session.get = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=found)


def test_create_session_db_error_returns_503(client: TestClient) -> None:
    _failing_executes(client.mock_session, SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = client.post(
            "/api/v1/remy/sessions",
            json={"provider": "openai", "model": "gpt-4o", "context_window_tokens": 200000},
        )
    assert resp.status_code == 503


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/v1/remy/sessions/{sid}", None),
        ("patch", "/api/v1/remy/sessions/{sid}", {"name": "X"}),
        ("delete", "/api/v1/remy/sessions/{sid}", None),
        ("get", "/api/v1/remy/sessions/{sid}/messages", None),
        ("post", "/api/v1/remy/sessions/{sid}/messages", {"role": "user", "content": "hi"}),
        ("post", "/api/v1/remy/sessions/{sid}/reset-permissions", None),
        ("post", "/api/v1/remy/sessions/{sid}/resume", None),
        ("post", "/api/v1/remy/sessions/{sid}/stop", None),
        ("post", "/api/v1/remy/sessions/{sid}/undo", None),
    ],
    ids=[
        "get-session",
        "rename",
        "delete",
        "list-messages",
        "append-message",
        "reset-permissions",
        "resume",
        "stop",
        "undo",
    ],
)
def test_session_endpoints_db_error_return_503(
    client: TestClient, method: str, path: str, json_body: dict[str, Any] | None
) -> None:
    _owned_session_or_error(client.mock_session, SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        kwargs: dict[str, Any] = {} if json_body is None or method in ("get", "delete") else {"json": json_body}
        resp = getattr(client, method)(path.format(sid=uuid.uuid4()), **kwargs)
    assert resp.status_code == 503


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("post", "/api/v1/remy/sessions/{sid}/permission-response", {"request_id": "r", "action": "approve"}),
        ("post", "/api/v1/remy/sessions/{sid}/ui-command-results", {"results": []}),
    ],
    ids=["permission-response", "ui-command-results"],
)
def test_ui_command_endpoints_db_error_return_503(
    client: TestClient, method: str, path: str, json_body: dict[str, Any] | None
) -> None:
    _owned_session_or_error(client.mock_session, SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = getattr(client, method)(path.format(sid=uuid.uuid4()), json=json_body)
    assert resp.status_code == 503


def test_audit_trail_db_error_returns_503(system_admin_client: TestClient) -> None:
    _failing_executes(system_admin_client.mock_session, SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
        resp = system_admin_client.get(f"/api/v1/remy/sessions/{uuid.uuid4()}/audit-trail")
    assert resp.status_code == 503


def test_delete_session_registry_branch_clears_session(system_admin_client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _owned_session_or_error(system_admin_client.mock_session, None, chat_session)  # type: ignore[attr-defined]
    registry = MagicMock()
    registry.clear_session = AsyncMock()
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy._get_registry", return_value=registry),
    ):
        resp = system_admin_client.delete(f"/api/v1/remy/sessions/{chat_session.id}")
    assert resp.status_code == 200
    registry.clear_session.assert_awaited_once()


def test_permission_response_registry_branches(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _owned_session_or_error(client.mock_session, None, chat_session)  # type: ignore[attr-defined]
    request_id = str(uuid.uuid4())

    registry_missing = MagicMock()
    registry_missing.get_permission_request = AsyncMock(return_value=None)
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy._get_registry", return_value=registry_missing),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/permission-response",
            json={"request_id": request_id, "action": "approve"},
        )
    assert resp.status_code == 404

    registry_ok = MagicMock()
    registry_ok.get_permission_request = AsyncMock(return_value={"session_id": str(chat_session.id), "tools": []})
    registry_ok.set_permission_decision = AsyncMock()
    registry_ok.publish_permission_response = AsyncMock()
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy._get_registry", return_value=registry_ok),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/permission-response",
            json={"request_id": request_id, "action": "approve_for_session"},
        )
    assert resp.status_code == 200, resp.text
    registry_ok.publish_permission_response.assert_awaited_once()


def test_permission_response_registry_foreign_session_returns_403(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _owned_session_or_error(client.mock_session, None, chat_session)  # type: ignore[attr-defined]
    registry = MagicMock()
    registry.get_permission_request = AsyncMock(return_value={"session_id": str(uuid.uuid4()), "tools": []})
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy._get_registry", return_value=registry),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/permission-response",
            json={"request_id": str(uuid.uuid4()), "action": "approve"},
        )
    assert resp.status_code == 403


def test_ui_command_results_registry_branch(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _owned_session_or_error(client.mock_session, None, chat_session)  # type: ignore[attr-defined]
    registry = MagicMock()
    registry.set_ui_command_results = AsyncMock()
    registry.publish_ui_results = AsyncMock()
    with (
        patch("modulo.api.routes.remy.get_registry", return_value=_ui_driving_enabled(True)),
        patch("modulo.api.routes.remy._get_registry", return_value=registry),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/remy/sessions/{chat_session.id}/ui-command-results",
            json={"results": [{"id": "1", "name": "click", "success": True}]},
        )
    assert resp.status_code == 200, resp.text
    registry.publish_ui_results.assert_awaited_once()


def test_resume_and_stop_registry_branches(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _owned_session_or_error(client.mock_session, None, chat_session)  # type: ignore[attr-defined]
    registry = MagicMock()
    registry.publish_resume = AsyncMock()
    registry.set_ui_command_results = AsyncMock()
    registry.publish_ui_results = AsyncMock()
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.remy._get_registry", return_value=registry),
    ):
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/resume")
        assert resp.status_code == 200, resp.text
        resp = client.post(f"/api/v1/remy/sessions/{chat_session.id}/stop")
        assert resp.status_code == 200, resp.text
    assert registry.publish_resume.await_count == 2
    registry.set_ui_command_results.assert_awaited_once()
