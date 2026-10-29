"""Unit tests for /api/v1/assistant endpoints (sessions, messages, stream, UI commands).

Unit tier: no DB — the SQLAlchemy session is a contract-correct AsyncMock,
RLS helpers and CRUD functions are patched at the route-module boundary, and
the model backend is never constructed (StubModelBackend policy).
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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
from modulo.api.routes import assistant as assistant_routes
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.models.assistant_message import ChatMessage
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
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.assistant.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/assistant/sessions")

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
            "modulo.api.routes.assistant.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("boom"),
        ),
    ):
        resp = client.get("/api/v1/assistant/sessions")
    assert resp.status_code == 503


def test_list_sessions_missing_table_returns_501(client: TestClient) -> None:
    _queue_executes(client.mock_session)  # type: ignore[attr-defined]
    programming_error = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with (
        patch(
            "modulo.api.routes.assistant.set_rls_org",
            new_callable=AsyncMock,
            side_effect=programming_error,
        ),
    ):
        resp = client.get("/api/v1/assistant/sessions")
    assert resp.status_code == 501


def test_list_sessions_unexpected_error_returns_500(client: TestClient) -> None:
    _queue_executes(client.mock_session)  # type: ignore[attr-defined]
    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock, side_effect=RuntimeError("kaboom")),
    ):
        resp = client.get("/api/v1/assistant/sessions")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /sessions — create
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET / PATCH / DELETE /sessions/{id}
# ---------------------------------------------------------------------------


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

    with patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock):
        resp = client.get(f"/api/v1/assistant/sessions/{chat_session.id}/messages")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["role"] == "user"
    assert body["items"][0]["content"] == "hello"


def test_append_message_returns_201(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]

    with patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock):
        resp = client.post(
            f"/api/v1/assistant/sessions/{chat_session.id}/messages",
            json={"role": "user", "content": "hi there"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "user"
    assert body["content"] == "hi there"


def test_append_message_invalid_role_rejected(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/assistant/sessions/{uuid.uuid4()}/messages",
        json={"role": "wizard", "content": "hi"},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# POST /sessions/{id}/stream
# ---------------------------------------------------------------------------


def test_stream_db_error_returns_503(client: TestClient) -> None:
    client.mock_session.get = AsyncMock(side_effect=SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    with patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock):
        resp = client.post(
            f"/api/v1/assistant/sessions/{uuid.uuid4()}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 503


def test_stream_missing_provider_rejected(client: TestClient) -> None:
    resp = client.post(f"/api/v1/assistant/sessions/{uuid.uuid4()}/stream", json={"content": "hi"})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_stream_returns_sse_error_event_when_init_fails(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    _stub_owned_session(client.mock_session, chat_session)  # type: ignore[attr-defined]
    fake_db = AsyncMock()
    init = assistant_routes._StreamInit(error_detail="No active openai API key configured")
    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.assistant.AsyncSession", return_value=fake_db),
        patch("modulo.api.routes.assistant._initialise_stream", new_callable=AsyncMock, return_value=init),
    ):
        resp = client.post(
            f"/api/v1/assistant/sessions/{chat_session.id}/stream",
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
    init = assistant_routes._StreamInit(backend=MagicMock(), parent_msg_id=uuid.uuid4(), messages=[])

    async def fake_loop(ctx: Any, request: Any, backend: Any, messages: Any, parent: Any, state: Any) -> Any:
        state["msg_id"] = "msg-123"
        yield 'event: token\ndata: {"token": "hi"}\n\n'

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.assistant.AsyncSession", return_value=fake_db),
        patch("modulo.api.routes.assistant._initialise_stream", new_callable=AsyncMock, return_value=init),
        patch("modulo.api.routes.assistant._run_stream_loop", fake_loop),
    ):
        resp = client.post(
            f"/api/v1/assistant/sessions/{chat_session.id}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 200, resp.text
    assert "event: done" in resp.text
    assert "msg-123" in resp.text


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
    human = assistant_routes._message_to_langchain(_msg("user", "hi"))
    assert isinstance(human, HumanMessage)
    assistant = assistant_routes._message_to_langchain(_msg("assistant", "yo"))
    assert isinstance(assistant, AIMessage)
    tool_use = assistant_routes._message_to_langchain(
        _msg("tool_use", "", tool_calls_json={"tool_calls": [{"name": "click", "args": {}, "id": "t1"}]})
    )
    assert isinstance(tool_use, AIMessage)
    assert tool_use.tool_calls[0]["name"] == "click"
    tool_result = assistant_routes._message_to_langchain(
        _msg("tool_result", "ok", tool_results_json={"tool_call_id": "tc-1"})
    )
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.tool_call_id == "tc-1"
    summary = assistant_routes._message_to_langchain(_msg("summary", "so far"))
    assert isinstance(summary, SystemMessage)
    unknown = assistant_routes._message_to_langchain(_msg("mystery", "?"))
    assert isinstance(unknown, HumanMessage)


def test_build_backend_unsupported_provider_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        assistant_routes._build_backend("not-a-provider", "m", "key")
    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


def test_reconstruct_tool_calls_parses_and_tolerates_bad_json() -> None:
    buffers = {1: {"id": "a", "name": "click", "args": '{"selector": "#x"}'}}
    calls = assistant_routes._reconstruct_tool_calls(buffers)
    assert calls == [{"id": "a", "name": "click", "args": {"selector": "#x"}}]
    broken = {0: {"id": "b", "name": "fill", "args": "{not json"}}
    calls = assistant_routes._reconstruct_tool_calls(broken)
    assert calls == [{"id": "b", "name": "fill", "args": {}}]


def test_prune_context_window_trims_oldest_middle_messages() -> None:
    messages = [HumanMessage(content="x" * 400) for _ in range(5)]
    pruned = assistant_routes._prune_context_window(messages, 500)
    assert pruned == 1
    assert len(messages) == 4


def test_accumulate_tool_call_chunks_merges_argument_fragments() -> None:
    buffers: dict[int, dict[str, Any]] = {}
    first = {"index": 0, "id": "t1", "name": "click", "args": '{"sel'}
    second = {"index": 0, "id": None, "name": None, "args": 'ector": "#a"}'}
    other = {"index": 1, "id": "t2", "name": "fill", "args": "{}"}
    assistant_routes._accumulate_tool_call_chunks(
        AIMessageChunk(content="", tool_call_chunks=[first, other]),  # type: ignore[arg-type]
        buffers,
    )
    assistant_routes._accumulate_tool_call_chunks(
        AIMessageChunk(content="", tool_call_chunks=[second]),  # type: ignore[arg-type]
        buffers,
    )
    assert buffers[0]["args"] == '{"selector": "#a"}'
    assert buffers[1]["name"] == "fill"


def test_resolve_stream_api_key_uses_explicit_override() -> None:
    principal = TenantPrincipal(
        username="u",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", api_key="sk-explicit")
    ctx = assistant_routes._StreamContext(AsyncMock(), principal, uuid.uuid4(), req, _make_settings(), MagicMock())
    api_key, error = asyncio.run(assistant_routes._resolve_stream_api_key(ctx))
    assert api_key == "sk-explicit"
    assert error is None


def test_resolve_stream_api_key_error_when_no_backend_configured() -> None:
    principal = TenantPrincipal(
        username="u",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    mock_session = _make_mock_session()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    _queue_executes(mock_session, no_row)
    ctx = assistant_routes._StreamContext(mock_session, principal, uuid.uuid4(), req, _make_settings(), MagicMock())
    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.assistant.set_rls_user_context", new_callable=AsyncMock),
    ):
        api_key, error = asyncio.run(assistant_routes._resolve_stream_api_key(ctx))
    assert api_key is None
    assert error is not None
    assert "No active openai API key" in error


def test_build_stream_backend_rejects_unsupported_provider() -> None:
    req = assistant_routes.StreamRequest(content="hi", provider="bogus", model="m")
    backend, error = assistant_routes._build_stream_backend(req, "key")
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
    req: assistant_routes.StreamRequest | None = None,
    db_session: AsyncMock | None = None,
    chat_session: MagicMock | None = None,
) -> assistant_routes._StreamContext:
    if req is None:
        req = assistant_routes.StreamRequest(content="hello", provider="openai", model="gpt-4o", api_key="sk")
    if db_session is None:
        db_session = _make_mock_session()
    return assistant_routes._StreamContext(
        db_session,
        _principal_obj(),
        uuid.uuid4(),
        req,
        _make_settings(),
        chat_session or MagicMock(),
    )


def _ctx_with_id_assigning_flush() -> assistant_routes._StreamContext:
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
    with patch("modulo.api.routes.assistant.importlib.import_module", return_value=fake_module):
        backend = assistant_routes._build_backend("openai", "gpt-4o", "sk")
    assert backend is fake_backend_cls.return_value
    fake_backend_cls.assert_called_once_with(api_key="sk", model_id="gpt-4o")


def test_resolve_api_key_decrypts_stored_credentials() -> None:
    ctx_session = _make_mock_session()
    row = MagicMock()
    row.scalar_one_or_none.return_value = MagicMock(credentials_ciphertext=b"cipher")
    ctx_session.execute = AsyncMock(return_value=row)
    with patch(
        "modulo.api.routes.assistant.decode_stored_secret_scoped",
        new_callable=AsyncMock,
        return_value="decrypted-key",
    ):
        key = asyncio.run(assistant_routes._resolve_api_key("openai", _ORG_ID, ctx_session, _VALID_32))
    assert key == "decrypted-key"


def test_resolve_api_key_decrypt_failure_returns_none() -> None:
    ctx_session = _make_mock_session()
    row = MagicMock()
    row.scalar_one_or_none.return_value = MagicMock(credentials_ciphertext=b"cipher")
    ctx_session.execute = AsyncMock(return_value=row)
    with patch(
        "modulo.api.routes.assistant.decode_stored_secret_scoped",
        new_callable=AsyncMock,
        side_effect=ValueError("bad cipher"),
    ):
        key = asyncio.run(assistant_routes._resolve_api_key("openai", _ORG_ID, ctx_session, _VALID_32))
    assert key is None


def test_call_mcp_tool_success() -> None:
    client_cm = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True}
    client_cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    with patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm):
        result = asyncio.run(assistant_routes._call_mcp_tool("click", {"selector": "#a"}, "mcp-key", "http://base"))
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
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.assistant.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert result == {"ok": True}


def test_call_mcp_tool_exhausts_retries_on_timeouts() -> None:
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    with (
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.assistant.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert exc_info.value.status_code == 502


def test_call_mcp_tool_rejects_non_object_response() -> None:
    client_cm = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = ["not-an-object"]
    client_cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    with (
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        pytest.raises(ValueError, match="non-object"),
    ):
        asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))


def test_reconstruct_messages_converts_rows() -> None:
    db = _make_mock_session()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        _msg("user", "hi"),
        _msg("assistant", "hello"),
    ]
    db.execute = AsyncMock(return_value=result)
    messages = asyncio.run(assistant_routes._reconstruct_messages(db, uuid.uuid4()))
    assert len(messages) == 2
    assert isinstance(messages[0], HumanMessage)
    assert isinstance(messages[1], AIMessage)


def test_run_mcp_tool_calls_without_key_reports_error() -> None:
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    calls = [{"id": "t1", "name": "search", "args": {}}]
    events = _collect(assistant_routes._run_mcp_tool_calls(calls, req, "http://base"))
    assert len(events) == 1
    assert events[0]["success"] is False
    assert "MCP API key" in events[0]["error"]


def test_run_mcp_tool_calls_with_key_executes_and_reports_failures() -> None:
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", mcp_api_key="mcp-key")
    calls = [{"id": "t1", "name": "search", "args": {}}]
    with patch(
        "modulo.api.routes.assistant._call_mcp_tool",
        new_callable=AsyncMock,
        return_value={"result": "found"},
    ):
        events = _collect(assistant_routes._run_mcp_tool_calls(calls, req, "http://base"))
    assert events[0]["success"] is True

    with patch("modulo.api.routes.assistant._call_mcp_tool", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        events = _collect(assistant_routes._run_mcp_tool_calls(calls, req, "http://base"))
    assert events[0]["success"] is False
    assert "RuntimeError" in events[0]["error"]


def test_run_mcp_tool_calls_reraises_http_exception() -> None:
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", mcp_api_key="mcp-key")
    calls = [{"id": "t1", "name": "search", "args": {}}]
    with (
        patch(
            "modulo.api.routes.assistant._call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=502, detail="gateway"),
        ),
        pytest.raises(HTTPException),
    ):
        _collect(assistant_routes._run_mcp_tool_calls(calls, req, "http://base"))


def test_save_and_add_assistant_messages() -> None:
    ctx = _ctx_with_id_assigning_flush()
    parent_id = asyncio.run(assistant_routes._save_stream_user_message(ctx))
    assert parent_id is not None

    msg_id = asyncio.run(assistant_routes._add_assistant_message(ctx, "answer text", None, parent_id))
    assert uuid.UUID(msg_id)


def test_finalise_stream_assistant_message_skips_naming_when_named() -> None:
    ctx = _ctx_with_id_assigning_flush()
    ctx.chat_session.name = "Already Named"
    msg_id = asyncio.run(assistant_routes._finalise_stream_assistant_message(ctx, "answer", None))
    assert uuid.UUID(msg_id)


def test_build_stream_system_prompt_uses_skill_loader() -> None:
    ctx = _stream_ctx()
    loader = MagicMock()
    loader.build_system_prompt = AsyncMock(return_value="System prompt text")
    with patch("modulo.api.routes.assistant.SkillLoader", return_value=loader):
        prompt = asyncio.run(assistant_routes._build_stream_system_prompt(ctx, supports_tools=True))
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
        patch("modulo.api.routes.assistant._build_backend", return_value=stub_backend),
        patch("modulo.api.routes.assistant.SkillLoader", return_value=loader),
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        init = asyncio.run(assistant_routes._initialise_stream(ctx))

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
        "modulo.api.routes.assistant._resolve_stream_api_key",
        new_callable=AsyncMock,
        return_value=(None, "No active openai API key configured."),
    ):
        init = asyncio.run(assistant_routes._initialise_stream(ctx))
    assert init.error_detail == "No active openai API key configured."


def test_initialise_stream_reports_backend_error() -> None:
    ctx = _stream_ctx()
    with (
        patch("modulo.api.routes.assistant._resolve_stream_api_key", new_callable=AsyncMock, return_value=("sk", None)),
        patch(
            "modulo.api.routes.assistant._build_stream_backend",
            return_value=(None, "Failed to initialize backend: boom"),
        ),
    ):
        init = asyncio.run(assistant_routes._initialise_stream(ctx))
    assert init.error_detail == "Failed to initialize backend: boom"


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
    events = _collect(assistant_routes._stream_backend_tokens(backend, [], None, request, buffers, state))
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
    events = _collect(assistant_routes._stream_backend_tokens(backend, [], None, request, {}, state))
    assert not events
    assert state["disconnected"] is True


def test_append_turn_messages_extends_conversation() -> None:
    messages: list[Any] = [HumanMessage(content="hi")]
    assistant_routes._append_turn_messages(
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
    ping = assistant_routes._ping_event_if_due(state)
    assert ping == "event: ping\ndata: {}\n\n"
    assert assistant_routes._ping_event_if_due(state) is None


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
        patch("modulo.api.routes.assistant._build_stream_tools_param", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.assistant._stream_backend_tokens", _token_gen),
        patch("modulo.api.routes.assistant._finalise_stream_assistant_message", finalise),
    ):
        events = _collect(assistant_routes._run_stream_loop(ctx, MagicMock(), MagicMock(), [], None, state))
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
        patch("modulo.api.routes.assistant._build_stream_tools_param", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.assistant._stream_backend_tokens", _token_gen),
        patch("modulo.api.routes.assistant._reconstruct_tool_calls", side_effect=[tool_calls, []]),
        patch("modulo.api.routes.assistant._stream_tool_events", _tool_gen),
        patch("modulo.api.routes.assistant._persist_assistant_and_tool_messages", persist),
        patch("modulo.api.routes.assistant._finalise_stream_assistant_message", finalise),
    ):
        events = _collect(assistant_routes._run_stream_loop(ctx, MagicMock(), MagicMock(), [], None, state))
    assert len(events) == 6
    assert state["msg_id"] == "msg-3"
    persist.assert_awaited_once()
    assert any("event: ping" in event for event in events)


def test_stream_event_generator_error_handlers() -> None:
    fake_db = AsyncMock()
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")

    async def _scenario(side_effect: Exception) -> list[str]:
        with (
            patch("modulo.api.routes.assistant.AsyncSession", return_value=fake_db),
            patch("modulo.api.routes.assistant._initialise_stream", new_callable=AsyncMock, side_effect=side_effect),
        ):
            events: list[str] = []
            gen = assistant_routes._stream_event_generator(
                _make_mock_session(), MagicMock(), _principal_obj(), uuid.uuid4(), req, _make_settings(), MagicMock()
            )
            async for event in gen:
                events.append(event)
            return events

    http_events = asyncio.run(_scenario(HTTPException(status_code=400, detail="bad request")))
    assert "bad request" in http_events[0]

    db_events = asyncio.run(_scenario(SQLAlchemyError("db down")))
    assert assistant_routes._MSG_DATABASE_ERROR_PLEASE_TRY in db_events[0]

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


# ---------------------------------------------------------------------------
# Additional coverage: registry and route error paths.
# ---------------------------------------------------------------------------


def test_get_registry_returns_none_when_no_redis_url() -> None:
    with patch("modulo.api.routes.assistant.get_settings") as settings_cls:
        settings_cls.return_value = MagicMock(redis_url="")
        assistant_routes._redis_registry = None
        result = assistant_routes._get_registry()
    assert result is None
    assistant_routes._redis_registry = None


def test_stream_chat_programming_error_returns_501(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    client.mock_session.get = AsyncMock(return_value=chat_session)  # type: ignore[attr-defined]
    programming_error = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with patch(
        "modulo.api.routes.assistant.set_rls_org",
        new_callable=AsyncMock,
        side_effect=programming_error,
    ):
        resp = client.post(
            f"/api/v1/assistant/sessions/{chat_session.id}/stream",
            json={"content": "hi", "provider": "openai", "model": "gpt-4o"},
        )
    assert resp.status_code == 501


def test_list_messages_programming_error_returns_501(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    client.mock_session.get = AsyncMock(return_value=chat_session)  # type: ignore[attr-defined]
    with patch(
        "modulo.api.routes.assistant.set_rls_org",
        new_callable=AsyncMock,
        side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
    ):
        resp = client.get(f"/api/v1/assistant/sessions/{chat_session.id}/messages")
    assert resp.status_code == 501


def test_append_message_programming_error_returns_501(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    client.mock_session.get = AsyncMock(return_value=chat_session)  # type: ignore[attr-defined]
    with patch(
        "modulo.api.routes.assistant.set_rls_org",
        new_callable=AsyncMock,
        side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
    ):
        resp = client.post(
            f"/api/v1/assistant/sessions/{chat_session.id}/messages",
            json={"role": "user", "content": "hi"},
        )
    assert resp.status_code == 501


def test_append_message_unexpected_error_returns_500(client: TestClient) -> None:
    chat_session = _owned_chat_session()
    client.mock_session.get = AsyncMock(return_value=chat_session)  # type: ignore[attr-defined]
    with patch(
        "modulo.api.routes.assistant.set_rls_org",
        new_callable=AsyncMock,
        side_effect=RuntimeError("kaboom"),
    ):
        resp = client.post(
            f"/api/v1/assistant/sessions/{chat_session.id}/messages",
            json={"role": "user", "content": "hi"},
        )
    assert resp.status_code == 500


def test_accumulate_tool_call_chunk_new_index() -> None:
    buffers: dict[int, dict[str, Any]] = {}
    chunk = {"index": 0, "id": "t1", "name": "click", "args": '{"s": 1}'}
    assistant_routes._accumulate_one_tool_call(chunk, buffers)
    assert buffers[0]["id"] == "t1"
    assert buffers[0]["args"] == '{"s": 1}'


def test_accumulate_tool_call_chunk_appends_args() -> None:
    buffers: dict[int, dict[str, Any]] = {0: {"id": "t1", "name": "click", "args": "a"}}
    chunk = {"index": 0, "id": None, "name": None, "args": "b"}
    assistant_routes._accumulate_one_tool_call(chunk, buffers)
    assert buffers[0]["args"] == "ab"


def test_message_to_langchain_tool_result_non_string_call_id() -> None:
    msg = _msg("tool_result", "ok", tool_results_json={"tool_call_id": 123})
    result = assistant_routes._message_to_langchain(msg)
    assert isinstance(result, ToolMessage)
    assert not result.tool_call_id


def test_tool_result_content_without_result_or_error() -> None:
    assert assistant_routes._tool_result_content({}) == json.dumps("")


def _timeout_without_leaking_awaitable(awaitable: Any, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.wait_for`` stand-in that times out and closes the awaitable.

    Callers evaluate ``event.wait()`` BEFORE handing it to ``wait_for``; raising
    straight from the double leaves that coroutine un-awaited, which surfaces as
    ``PytestUnraisableExceptionWarning: coroutine object Event.wait`` at GC.
    Closing it reproduces the timeout without leaking.
    """
    awaitable.close()
    raise TimeoutError


def test_stream_event_generator_http_exception_from_init() -> None:
    fake_db = AsyncMock()
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")

    async def _scenario() -> list[str]:
        with (
            patch("modulo.api.routes.assistant.AsyncSession", return_value=fake_db),
            patch(
                "modulo.api.routes.assistant._initialise_stream",
                new_callable=AsyncMock,
                side_effect=HTTPException(status_code=400, detail="bad request"),
            ),
        ):
            events: list[str] = []
            gen = assistant_routes._stream_event_generator(
                _make_mock_session(), MagicMock(), _principal_obj(), uuid.uuid4(), req, _make_settings(), MagicMock()
            )
            async for event in gen:
                events.append(event)
            return events

    events = asyncio.run(_scenario())
    assert "bad request" in events[0]


def test_stream_event_generator_programming_error() -> None:
    fake_db = AsyncMock()
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    programming_error = ProgrammingError("SELECT 1", {}, Exception("missing table"))

    async def _scenario() -> list[str]:
        with (
            patch("modulo.api.routes.assistant.AsyncSession", return_value=fake_db),
            patch(
                "modulo.api.routes.assistant._initialise_stream",
                new_callable=AsyncMock,
                side_effect=programming_error,
            ),
        ):
            events: list[str] = []
            gen = assistant_routes._stream_event_generator(
                _make_mock_session(), MagicMock(), _principal_obj(), uuid.uuid4(), req, _make_settings(), MagicMock()
            )
            async for event in gen:
                events.append(event)
            return events

    events = asyncio.run(_scenario())
    assert "not available" in events[0]


def test_stream_event_generator_backend_none_emits_error() -> None:
    fake_db = AsyncMock()
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o")
    init = assistant_routes._StreamInit(backend=None, parent_msg_id=uuid.uuid4(), messages=[])

    async def _scenario() -> list[str]:
        with (
            patch("modulo.api.routes.assistant.AsyncSession", return_value=fake_db),
            patch("modulo.api.routes.assistant._initialise_stream", new_callable=AsyncMock, return_value=init),
        ):
            events: list[str] = []
            gen = assistant_routes._stream_event_generator(
                _make_mock_session(), MagicMock(), _principal_obj(), uuid.uuid4(), req, _make_settings(), MagicMock()
            )
            async for event in gen:
                events.append(event)
            return events

    events = asyncio.run(_scenario())
    assert "error" in events[0]


def test_call_mcp_tool_retries_on_502_then_succeeds() -> None:
    client_cm = AsyncMock()
    resp_502 = MagicMock()
    resp_502.status_code = 502
    http_error = httpx.HTTPStatusError("bad gateway", request=MagicMock(), response=resp_502)
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {"ok": True}
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=[http_error, resp_ok])
    with (
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.assistant.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert result == {"ok": True}


def test_call_mcp_tool_exhausts_retries_on_503() -> None:
    client_cm = AsyncMock()
    resp_503 = MagicMock()
    resp_503.status_code = 503
    http_error = httpx.HTTPStatusError("service unavailable", request=MagicMock(), response=resp_503)
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=http_error)
    with (
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.assistant.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert exc_info.value.status_code == 502


def test_call_mcp_tool_request_error_retries_then_fails() -> None:
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=httpx.RequestError("connection refused"))
    with (
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        patch("modulo.api.routes.assistant.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))
    assert exc_info.value.status_code == 502


def test_call_mcp_tool_non_retriable_400_raises_immediately() -> None:
    client_cm = AsyncMock()
    resp_400 = MagicMock()
    resp_400.status_code = 400
    http_error = httpx.HTTPStatusError("bad request", request=MagicMock(), response=resp_400)
    client_cm.__aenter__.return_value.post = AsyncMock(side_effect=http_error)
    with (
        patch("modulo.api.routes.assistant.pinned_async_client", new_callable=AsyncMock, return_value=client_cm),
        pytest.raises(httpx.HTTPStatusError),
    ):
        asyncio.run(assistant_routes._call_mcp_tool("click", {}, "mcp-key", "http://base"))


def test_persist_assistant_and_tool_messages() -> None:
    ctx = _ctx_with_id_assigning_flush()
    tool_calls = [{"id": "t1", "name": "click", "args": {}}]
    tool_results = [{"tool_call_id": "t1", "tool_name": "click", "success": True, "result": {"ok": 1}}]
    msg_id = asyncio.run(
        assistant_routes._persist_assistant_and_tool_messages(ctx, "response", tool_calls, tool_results, None)
    )
    assert uuid.UUID(msg_id)


def test_run_mcp_tool_calls_with_key_empty_calls() -> None:
    req = assistant_routes.StreamRequest(content="hi", provider="openai", model="gpt-4o", mcp_api_key="k")
    events = _collect(assistant_routes._run_mcp_tool_calls([], req, "http://base"))
    assert events == []
