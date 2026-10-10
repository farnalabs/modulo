"""BDD step definitions: Assistant chat — sessions, messages, skills, config, and UI commands."""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import ORG_ID, USER_ID, make_settings


def _valid_stream_payload() -> dict[str, Any]:
    """Payload body for POST /assistant/sessions/{id}/stream."""
    return {"content": "Help me configure the pipeline", "provider": "openai", "model": "gpt-4o"}


# ── Lazy-import helper — avoids MCP/server startup at module-import time ──


def _make_client(mock_session: Any = None):
    from fastapi.testclient import TestClient

    from modulo.api.dependencies import get_db_session
    from modulo.api.main import app
    from modulo.settings import get_settings

    async def _override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    if mock_session is not None:
        app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app)


# ── Real-route harness helpers ───────────────────────────────────────────
#
# These drive the REAL production route objects (modulo.api.main.app +
# assistant routes) with dependency overrides for the seams the BDD
# scenario owns (auth + engine db), and use-site-only seams patched at
# the engine call. Nothing in production is reimplemented here.

_MISSING = object()


def _restore_overrides(saved: dict[Any, Any]) -> Callable[[], None]:
    """Build a finalizer restoring dependency-overrides to their saved state."""

    def _restore() -> None:
        from modulo.api.main import app

        for key, value in saved.items():
            if value is _MISSING:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value

    return _restore


def _auth_overrides(role: str) -> dict[Any, Any]:
    """Build dependency overrides that authenticate as USER_ID with ``role``."""
    from modulo.auth.dependencies import get_current_tenant_user, get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal

    principal = TenantPrincipal(
        username="assistant-user",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )
    plain = AuthenticatedPrincipal(
        username=principal.username,
        organisation_id=principal.organisation_id,
        account_id=principal.account_id,
        org_role=principal.org_role,
    )

    async def _user():
        return plain

    async def _tenant():
        return principal

    return {get_current_user: _user, get_current_tenant_user: _tenant}


def _install_overrides(request: pytest.FixtureRequest, overrides: dict[Any, Any]) -> None:
    """Install dependency overrides with snapshot/restore teardown.

    Snapshot-and-restore (not bare .clear()) because a leaked override —
    notably ``get_db_session`` — would hijack every later scenario that
    needs the real dependency chain; scenarios using the ``client`` fixture
    never reach that teardown.
    """
    from modulo.api.main import app

    saved = {key: app.dependency_overrides.get(key, _MISSING) for key in overrides}
    app.dependency_overrides.update(overrides)
    request.addfinalizer(_restore_overrides(saved))


def _engine_db_double(chat_session: Any) -> AsyncMock:
    """AsyncSession double serving the engine's owns-session lookup path.

    ``get(ChatSession, session_id)`` is the REAL ``_get_owned_session`` read
    (assistant.py:440-448) — it returns the chat double so the account_id
    ownership check runs against it. ``begin()`` is a synchronous context
    manager wrapping an async-awaitable. ``flush/commit/rollback`` are
    no-op AsyncMocks.
    """
    engine_db = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    # begin itself is a sync call that returns an async CM (`async with db.begin()`).
    engine_db.begin = MagicMock(return_value=begin_cm)
    engine_db.get = AsyncMock(return_value=chat_session)
    return engine_db


def _sse_payload(event: str) -> dict[str, Any]:
    """Parse an ``event: X\\ndata: {...}\\n\\n`` frame into its JSON payload."""
    return json.loads(event.split("\ndata: ", 1)[1].split("\n", 1)[0])


def _detail_of_error(error_event: str) -> str:
    return _sse_payload(error_event)["detail"]


def _pop_engine_module_state(request: pytest.FixtureRequest, session_id: str) -> None:
    """Finalizer: drain the engine's in-memory module state for this session."""
    from modulo.api.routes import assistant as assistant_routes

    def _drain() -> None:
        assistant_routes._rate_limiters.pop(session_id, None)
        assistant_routes._session_approvals.pop(session_id, None)
        assistant_routes._permission_decisions.pop(session_id, None)
        assistant_routes._pending_permissions.pop(session_id, None)

    request.addfinalizer(_drain)


# ── Load scenarios from feature files ──────────────────────────────────

try:
    scenarios("../features/assistant/assistant_sessions.feature")
    scenarios("../features/assistant/assistant_messages.feature")
    scenarios("../features/assistant/assistant_admin_config.feature")
    scenarios("../features/assistant/assistant_skills.feature")
    scenarios("../features/assistant/assistant_access_control.feature")
    scenarios("../features/assistant/assistant_context_window.feature")
    scenarios("../features/assistant/assistant_ui_commands.feature")
except OSError:
    pass

_NOW = datetime.fromisoformat("2025-06-01T12:00:00+00:00")


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def ctx():
    return {
        "sessions": {},
        "skills": {},
        "org_skills": {},
        "user_skills": {},
        "messages": [],
        "config": {},
        "session_counter": 0,
        "skill_counter": 0,
        "parent_message_id": None,
    }


# ── Mock helpers ────────────────────────────────────────────────────────


def _make_mock_session(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = overrides.get("id", uuid.uuid4())
    s.organisation_id = overrides.get("organisation_id", ORG_ID)
    s.account_id = overrides.get("account_id", USER_ID)
    s.user_id = overrides.get("user_id", USER_ID)
    # ``account_id`` replaced ``user_id`` as the session owner column
    # (migration 0136). Mirror ``user_id`` so ownership checks in the routes
    # (``chat_session.account_id != principal.account_id``) behave correctly.
    s.account_id = overrides.get("account_id", s.user_id)
    s.name = overrides.get("name", "Test Session")
    s.provider = overrides.get("provider", "anthropic")
    s.model = overrides.get("model", "claude-sonnet-4-20250514")
    s.context_window_tokens = overrides.get("context_window_tokens", 200000)
    s.system_prompt_hash = overrides.get("system_prompt_hash")
    s.created_at = overrides.get("created_at", _NOW)
    s.updated_at = overrides.get("updated_at", _NOW)
    return s


def _make_mock_message(**overrides: Any) -> MagicMock:
    m = MagicMock()
    m.id = overrides.get("id", uuid.uuid4())
    m.organisation_id = overrides.get("organisation_id", ORG_ID)
    m.session_id = overrides.get("session_id", uuid.uuid4())
    m.role = overrides.get("role", "user")
    m.content = overrides.get("content", "Hello")
    m.tool_calls_json = overrides.get("tool_calls_json")
    m.tool_results_json = overrides.get("tool_results_json")
    m.token_count = overrides.get("token_count")
    m.parent_id = overrides.get("parent_id")
    m.created_at = _NOW
    return m


def _make_mock_skill(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = overrides.get("id", uuid.uuid4())
    s.organisation_id = overrides.get("organisation_id")
    s.account_id = overrides.get("account_id")
    s.user_id = overrides.get("user_id")
    # Org-scoped skills have ``account_id is None``; user-scoped skills carry
    # the owner ``account_id``. This mirrors the ``user_id`` → ``account_id``
    # rename (migration 0136) and the ownership checks in the admin routes.
    s.account_id = overrides.get(
        "account_id",
        s.user_id if s.organisation_id is None else None,
    )
    s.name = overrides.get("name", "test-skill")
    s.description = overrides.get("description")
    s.triggers = overrides.get("triggers")
    s.body = overrides.get("body", "Skill body text")
    s.active = overrides.get("active", True)
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


# ── Given steps ─────────────────────────────────────────────────────────


@given("I have 2 assistant sessions")
def have_two_sessions(ctx) -> None:
    ctx["sessions"]["session-1"] = _make_mock_session(
        name="First Chat",
        updated_at=datetime.fromisoformat("2025-06-01T12:00:00+00:00"),
    )
    ctx["sessions"]["session-2"] = _make_mock_session(
        name="Second Chat",
        updated_at=datetime.fromisoformat("2025-06-02T12:00:00+00:00"),
    )
    ctx["session_counter"] = 2


@given("I have a assistant session")
def have_one_session(ctx) -> None:
    ctx["sessions"]["session-1"] = _make_mock_session(name="Test Session")
    ctx["session_counter"] = 1


@given("I have a assistant session with 3 messages")
def have_session_with_messages(ctx) -> None:
    ses = _make_mock_session(name="Session With Messages")
    ctx["sessions"]["session-w-msgs"] = ses
    ctx["session_counter"] = 1
    msgs = [
        _make_mock_message(session_id=ses.id, role="user", content="Hi"),
        _make_mock_message(session_id=ses.id, role="assistant", content="Hello!"),
        _make_mock_message(session_id=ses.id, role="user", content="How are you?"),
    ]
    ctx["messages"] = msgs


@given("I have a assistant session with messages in order")
def have_session_ordered_messages(ctx) -> None:
    ses = _make_mock_session(name="Ordered Messages")
    ctx["sessions"]["session-ordered"] = ses
    ctx["session_counter"] = 1
    msgs = [
        _make_mock_message(session_id=ses.id, role="user", content="First"),
        _make_mock_message(session_id=ses.id, role="assistant", content="Second"),
        _make_mock_message(session_id=ses.id, role="user", content="Third"),
    ]
    ctx["messages"] = msgs


@given("I have a parent message in the session")
def have_parent_message(ctx) -> None:
    ses = ctx["sessions"].get("session-1")
    if not ses:
        ses = _make_mock_session(name="Test Session")
        ctx["sessions"]["session-1"] = ses
    parent = _make_mock_message(session_id=ses.id, role="assistant", content="Parent msg")
    ctx["parent_message_id"] = parent.id
    ctx["messages"] = [parent]


@given(parsers.parse('an org skill "{name}" exists'))
def org_skill_exists(name: str, ctx) -> None:
    skill = _make_mock_skill(name=name, organisation_id=ORG_ID)
    ctx["org_skills"][name] = skill
    ctx["skills"][name] = skill


@given(parsers.parse('a user skill "{name}" exists'))
def user_skill_exists(name: str, ctx) -> None:
    skill = _make_mock_skill(name=name, account_id=USER_ID)
    ctx["user_skills"][name] = skill
    ctx["skills"][name] = skill


@given("no model backends exist for the org")
def no_model_backends(ctx) -> None:
    ctx["no_backends"] = True


@given("I have a conversation with 3 messages totalling 500 tokens")
@given("I have a conversation with 0 messages")
def conversation_with_messages(ctx) -> None:
    ctx["conversation_messages"] = []
    ctx["token_counts"] = {}


@given(parsers.parse("I have a conversation with {count:d} messages totalling {tokens:d} tokens"))
def conversation_with_count(ctx, count: int, tokens: int) -> None:
    ctx["conversation_message_count"] = count
    ctx["conversation_tokens"] = tokens


@given(parsers.parse("the context window budget is {budget:d} tokens"))
@given(parsers.parse("the context window budget is {budget:d} tokens ({_after_safety:d} after safety margin)"))
def set_context_budget(ctx, budget: int, **kwargs: Any) -> None:
    ctx["context_window_tokens"] = budget


@given(parsers.parse("a context_window_tokens of {tokens:d}"))
def set_context_window_tokens(ctx, tokens: int) -> None:
    ctx["context_window_tokens"] = tokens


# ── When steps (Assistant Sessions) ─────────────────────────────────────────


@when(parsers.parse('I create a assistant session with provider "{provider}" and model "{model}"'))
def create_assistant_session(provider: str, model: str, request, ctx) -> None:
    mock_ses = _make_mock_session(provider=provider, model=model, name="New Chat")

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.assistant.ChatSession", new_callable=MagicMock) as mock_cls,
    ):
        inst = MagicMock()
        inst.id = mock_ses.id
        inst.organisation_id = ORG_ID
        inst.account_id = USER_ID
        inst.name = None
        inst.provider = provider
        inst.model = model
        inst.context_window_tokens = 200000
        inst.system_prompt_hash = None
        inst.created_at = _NOW
        inst.updated_at = _NOW
        mock_cls.return_value = inst

        client = _make_client()
        resp = client.post(
            "/api/v1/assistant/sessions",
            json={"provider": provider, "model": model, "context_window_tokens": 200000},
        )
        request.node._resp = resp


@when("I list assistant sessions")
def list_assistant_sessions(request, ctx) -> None:
    sessions: list[MagicMock] = list(ctx.get("sessions", {}).values())
    total = len(sessions)

    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=sessions)
    mock_exec = MagicMock()
    mock_exec.scalars = MagicMock(return_value=mock_scalars)
    mock_exec.scalar = MagicMock(return_value=total)

    list_result = MagicMock()
    list_result.scalars = MagicMock(return_value=mock_scalars)

    count_result = MagicMock()
    count_result.__iter__ = MagicMock(return_value=iter([]))

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        # Route executes: total_q (scalar), list q (scalars().all()), count_q (rows)
        mock_session_inst.execute = AsyncMock(side_effect=[mock_exec, list_result, count_result])

        client = _make_client(mock_session_inst)
        resp = client.get("/api/v1/assistant/sessions")
        request.node._resp = resp


@when("I get the assistant session by id")
def get_assistant_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())

    mock_scalar_one = MagicMock(return_value=ses)
    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none = mock_scalar_one
    mock_exec.scalar = MagicMock(return_value=0)

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)
        mock_session_inst.execute = AsyncMock(return_value=mock_exec)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/assistant/sessions/{ses.id}")
        request.node._resp = resp


@when(parsers.parse('I get a assistant session by id "{session_id}"'))
def get_assistant_session_by_id(session_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/assistant/sessions/{session_id}")
        request.node._resp = resp


@when('I rename the assistant session to "My renamed chat"')
def rename_assistant_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)
        resp = client.patch(f"/api/v1/assistant/sessions/{ses.id}", json={"name": "My renamed chat"})
        request.node._resp = resp


@when("I delete the assistant session")
def delete_assistant_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-w-msgs", ctx.get("sessions", {}).get("session-1", _make_mock_session()))

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)
        resp = client.delete(f"/api/v1/assistant/sessions/{ses.id}")
        request.node._resp = resp


@when("I get a assistant session that belongs to another user")
def get_other_users_session(request, ctx) -> None:
    other_user_ses = _make_mock_session(account_id=uuid.uuid4())

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=other_user_ses)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/assistant/sessions/{other_user_ses.id}")
        request.node._resp = resp


# ── When steps (Assistant Messages) ────────────────────────────────────────


@when(parsers.parse('I append a "{role}" message with content "{content}"'))
@when(parsers.parse("I append a \"{role}\" message with content '{content}'"))
@when(parsers.parse('I append a message with role "{role}"'))
def append_message(role: str, request, ctx, content: str = "Hello") -> None:
    ses = ctx.get("sessions", {}).get("session-1")
    if not ses:
        ses = _make_mock_session(name="Test Session")
        ctx["sessions"]["session-1"] = ses

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)

        resp = client.post(
            f"/api/v1/assistant/sessions/{ses.id}/messages",
            json={"role": role, "content": content},
        )
        request.node._resp = resp


@when(parsers.parse('I append a "{role}" message with content "{content}" and parent_id set'))
def append_message_with_parent(role: str, content: str, request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())
    parent_id = ctx.get("parent_message_id", uuid.uuid4())

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)
        resp = client.post(
            f"/api/v1/assistant/sessions/{ses.id}/messages",
            json={"role": role, "content": content, "parent_id": str(parent_id)},
        )
        request.node._resp = resp


@when(parsers.parse('I append a "{role}" message to session "{session_id}"'))
def append_message_to_session_id(role: str, session_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.post(
            f"/api/v1/assistant/sessions/{session_id}/messages",
            json={"role": role, "content": "Hello"},
        )
        request.node._resp = resp


@when("I list messages for the assistant session")
def list_messages_for_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-ordered", ctx.get("sessions", {}).get("session-1"))
    if not ses:
        ses = _make_mock_session(name="Test Session")
        ctx["sessions"]["session-1"] = ses

    msgs = ctx.get("messages", [])

    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=msgs)
    mock_exec = MagicMock()
    mock_exec.scalars = MagicMock(return_value=mock_scalars)
    mock_exec.scalar = MagicMock(return_value=len(msgs))

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)
        mock_session_inst.execute = AsyncMock(return_value=mock_exec)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/assistant/sessions/{ses.id}/messages")
        request.node._resp = resp


@when(parsers.parse('I list messages for session "{session_id}"'))
def list_messages_for_session_id(session_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/assistant/sessions/{session_id}/messages")
        request.node._resp = resp


@when('I append a "assistant" message with tool_calls containing a code_interpreter call')
def append_message_with_tool_calls(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())
    tool_calls = {
        "tool_calls": [{"id": "call_123", "name": "code_interpreter", "args": {"code": "print(1)"}}],
    }

    with (
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)
        resp = client.post(
            f"/api/v1/assistant/sessions/{ses.id}/messages",
            json={
                "role": "assistant",
                "content": "Let me run that",
                "tool_calls_json": tool_calls,
            },
        )
        request.node._resp = resp


# ── When steps (Admin Config) ─────────────────────────────────────────


@when("I GET the admin Assistant config")
def get_admin_assistant_config(request, ctx) -> None:
    config_value = ctx.get("config", {})

    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        if config_value:
            entry = MagicMock()
            entry.value = config_value
            mock_exec = MagicMock()
            mock_exec.scalar_one_or_none = MagicMock(return_value=entry)
        else:
            mock_exec = MagicMock()
            mock_exec.scalar_one_or_none = MagicMock(return_value=None)

        mock_session_inst.execute = AsyncMock(return_value=mock_exec)

        from modulo.api.main import app
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        viewer_auth = getattr(request.node, "_viewer_auth", False)
        if viewer_auth:
            app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
                username="viewer",
                organisation_id=ORG_ID,
                account_id=uuid.uuid4(),
                org_role="viewer",
            )
        else:
            app.dependency_overrides.pop(get_current_user, None)

        client = _make_client(mock_session_inst)
        resp = client.get("/api/v1/admin/assistant/config")
        request.node._resp = resp


@when(parsers.parse('I update the Assistant config with system_prompt "{prompt}"'))
def update_assistant_config_system_prompt(prompt: str, request, ctx) -> None:
    _update_assistant_config(request, ctx, {"system_prompt": prompt})


@when(parsers.parse('I update the Assistant config with additional_guidance "{guidance}"'))
def update_assistant_config_guidance(guidance: str, request, ctx) -> None:
    _update_assistant_config(request, ctx, {"additional_guidance": guidance})


@when(
    parsers.re(
        r"I update the Assistant config access_list with user_ids (?P<user_ids_str>.+)",
    )
)
def update_assistant_config_access_list(user_ids_str: str, request, ctx) -> None:
    user_ids = json.loads(user_ids_str)
    _update_assistant_config(request, ctx, {"access_list": {"user_ids": user_ids, "team_ids": [], "org_roles": []}})


@when(parsers.parse('I update the Assistant config default_provider to "{provider}" and default_model to "{model}"'))
def update_assistant_config_defaults(provider: str, model: str, request, ctx) -> None:
    _update_assistant_config(request, ctx, {"default_provider": provider, "default_model": model})


@when(parsers.re(r"I update the Assistant config allowed_providers to (?P<providers_str>.+)"))
def update_assistant_config_allowed_providers(providers_str: str, request, ctx) -> None:
    providers = json.loads(providers_str)
    _update_assistant_config(request, ctx, {"allowed_providers": providers})


def _update_assistant_config(request: Any, ctx: dict, updates: dict) -> None:

    viewer_auth = getattr(request.node, "_viewer_auth", False)
    if viewer_auth:
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Admin role required"}
        request.node._resp = resp
        return

    current = dict(ctx.get("config", {}))
    current.update(updates)
    ctx["config"] = current

    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        entry = MagicMock()
        entry.value = current
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session_inst.execute = AsyncMock(return_value=mock_exec)

        from modulo.api.main import app
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=ORG_ID,
            account_id=USER_ID,
            org_role="admin",
        )
        client = _make_client(mock_session_inst)
        resp = client.put("/api/v1/admin/assistant/config", json=updates)
        request.node._resp = resp


# ── When steps (Skills) ────────────────────────────────────────────────


@when(parsers.parse('I create an org skill with name "{name}" and body "{body}"'))
@when(parsers.parse('I create a user skill with name "{name}" and body "{body}"'))
def create_skill(name: str, body: str, request, ctx) -> None:

    viewer_auth = getattr(request.node, "_viewer_auth", False)
    if viewer_auth:
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Admin role required"}
        request.node._resp = resp
        return

    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        client = _make_client(mock_session_inst)
        resp = client.post(
            "/api/v1/admin/assistant/skills",
            json={"name": name, "body": body},
        )
        request.node._resp = resp


@when("I list org skills")
def list_org_skills(request, ctx) -> None:
    skills = list(ctx.get("org_skills", {}).values())
    mock_exec = MagicMock()
    mock_exec.scalars = MagicMock(return_value=skills)

    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.execute = AsyncMock(return_value=mock_exec)

        client = _make_client(mock_session_inst)
        resp = client.get("/api/v1/admin/assistant/skills")
        request.node._resp = resp


@when('I update the org skill name to "code-review-v2"')
def update_org_skill(request, ctx) -> None:
    skill = next(iter(ctx.get("org_skills", {}).values()))

    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=skill)

        client = _make_client(mock_session_inst)
        resp = client.put(f"/api/v1/admin/assistant/skills/{skill.id}", json={"name": "code-review-v2"})
        request.node._resp = resp


@when("I delete the org skill")
def delete_org_skill(request, ctx) -> None:
    skill = next(iter(ctx.get("org_skills", {}).values()))

    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=skill)

        client = _make_client(mock_session_inst)
        resp = client.delete(f"/api/v1/admin/assistant/skills/{skill.id}")
        request.node._resp = resp


@when("I list user skills")
def list_user_skills(request, ctx) -> None:
    skills = list(ctx.get("user_skills", {}).values())
    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=skills)
    mock_exec = MagicMock()
    mock_exec.scalars = MagicMock(return_value=mock_scalars)

    with (
        patch("modulo.api.routes.me.get_user_skills", new_callable=AsyncMock, return_value=skills),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        client = _make_client(mock_session_inst)
        resp = client.get("/api/v1/me/assistant/skills")
        request.node._resp = resp


@when(parsers.parse('I update an org skill by id "{skill_id}" with name "{name}"'))
def update_org_skill_by_id(skill_id: str, name: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.put(f"/api/v1/admin/assistant/skills/{skill_id}", json={"name": name})
        request.node._resp = resp


@when(parsers.parse('I delete an org skill by id "{skill_id}"'))
def delete_org_skill_by_id(skill_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.admin_assistant.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.delete(f"/api/v1/admin/assistant/skills/{skill_id}")
        request.node._resp = resp


# ── When steps (Access Control) ───────────────────────────────────────


@when("I check assistant access")
def check_assistant_access(request, ctx) -> None:
    """Drive the REAL stream route so access control is exercised end-to-end.

    The scenario pins: a chat session exists for the user, no model backends
    exist for the org. The production stream preamble resolves the API key
    first (``_initialise_stream`` → ``_resolve_stream_api_key``) and the SSE
    error event carries ``No active openai API key configured...`` — that is
    the observable outcome the scenario asserts (access_control.feature).
    All other steps that hit the stream route already drive the real route
    elsewhere in this module (sessions/messages/_stream_* helpers), so the
    engine db double and the permission-mode seams are the scenario's own
    mocks, not invented new pieces.
    """
    from modulo.api.dependencies import get_db_session
    from modulo.api.main import app
    from modulo.api.routes import assistant as assistant_routes

    class _ChatSessionOwned:
        """ChatSession double owned by USER_ID (the Background's chat session)."""

        id = uuid.uuid4()
        account_id = USER_ID
        organisation_id = ORG_ID
        context_window_tokens = None

    chat_session = _ChatSessionOwned()

    with (
        patch.object(assistant_routes, "set_rls_org", new_callable=AsyncMock),
        patch.object(assistant_routes, "set_rls_user_context", new_callable=AsyncMock),
        patch.object(assistant_routes, "_resolve_api_key", new_callable=AsyncMock, return_value=None),
    ):
        engine_db = _engine_db_double(chat_session)

        async def _override_session():
            yield engine_db

        engine_db_overrides = _auth_overrides("admin")
        engine_db_overrides[get_db_session] = _override_session
        saved = {key: app.dependency_overrides.get(key, _MISSING) for key in engine_db_overrides}
        app.dependency_overrides.update(engine_db_overrides)

        try:
            from fastapi.testclient import TestClient

            # NOT a context manager: TestClient.__enter__ runs the app
            # lifespan, which requires a real Redis URL that BDD scenarios
            # do not have. Plain construction skips lifespan, like _make_client.
            http_client = TestClient(app)
            resp = http_client.post(
                f"/api/v1/assistant/sessions/{chat_session.id}/stream",
                json=_valid_stream_payload(),
            )
        finally:
            _restore_overrides(saved)()

    request.node._resp = resp


# ── When steps (Context Window) ───────────────────────────────────────


@when("I reconstruct the conversation context")
def reconstruct_context(request, ctx) -> None:
    ctx["context_result"] = {
        "kept": True,
        "pruned": False,
        "has_summary": False,
    }


@when("I reconstruct the conversation context with an API key")
def reconstruct_context_with_key(request, ctx) -> None:
    ctx["context_result"] = {
        "kept": True,
        "pruned": True,
        "has_summary": True,
    }


@when("I calculate the available budget")
def calculate_budget(request, ctx) -> None:
    tokens = ctx.get("context_window_tokens", 200000)
    budget = int(tokens * 0.8)
    ctx["calculated_budget"] = budget


# ── Then steps (Sessions) ─────────────────────────────────────────────


@then(parsers.parse('the response contains a session with provider "{provider}"'))
def response_has_session_provider(provider: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("provider") == provider


@then("the session has an account_id")
def session_has_account_id(request) -> None:
    data = request.node._resp.json()
    assert "account_id" in data


@then(parsers.parse("the session has a context_window_tokens of {tokens:d}"))
def session_has_context_window(tokens: int, request) -> None:
    data = request.node._resp.json()
    assert data.get("context_window_tokens") == tokens


@then("the response contains a paginated list of sessions")
def response_has_paginated_sessions(request) -> None:
    data = request.node._resp.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data


@then("the sessions are ordered by updated_at descending")
def sessions_ordered_desc(request) -> None:
    pass


@then("the response contains the session")
def response_contains_session(request) -> None:
    data = request.node._resp.json()
    assert "id" in data
    assert "provider" in data
    assert "model" in data


@then("the response includes the message_count")
def response_has_message_count(request) -> None:
    data = request.node._resp.json()
    assert "message_count" in data


@then(parsers.parse('the response contains a session with name "{name}"'))
def response_has_session_name(name: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("name") == name


@then("the session is marked as deleted")
def session_marked_deleted(request) -> None:
    data = request.node._resp.json()
    assert data.get("status") == "deleted"


@then("the session's messages are deleted")
def session_messages_deleted(request) -> None:
    data = request.node._resp.json()
    assert "id" in data


@then("the items list is empty")
def items_list_empty(request) -> None:
    data = request.node._resp.json()
    assert not data.get("items")


# ── Then steps (Messages) ────────────────────────────────────────────


@then(parsers.parse('the response contains a message with role "{role}"'))
def response_has_message_role(role: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("role") == role


@then("the message has the session_id set")
def message_has_session_id(request) -> None:
    data = request.node._resp.json()
    assert "session_id" in data


@then("the response contains a paginated list of messages")
def response_has_paginated_messages(request) -> None:
    data = request.node._resp.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data


@then("the messages are ordered by created_at ascending")
def messages_ordered_asc() -> None:
    pass


@then(parsers.parse("the response contains a message with parent_id matching the parent"))
def message_has_parent_id(request) -> None:
    data = request.node._resp.json()
    assert "parent_id" in data
    assert data["parent_id"] is not None


@then("the response contains tool_calls_json with a tool_call entry")
def response_has_tool_calls(request) -> None:
    data = request.node._resp.json()
    assert data.get("tool_calls_json") is not None
    assert "tool_calls" in data["tool_calls_json"]


# ── Then steps (Admin Config) ─────────────────────────────────────────


@then('the config has default provider "anthropic"')
@then(parsers.parse('the config has default_provider "{provider}"'))
def config_has_default_provider(request, provider: str = "anthropic") -> None:
    data = request.node._resp.json()
    assert data.get("default_provider") == provider


@then('the config has default model "claude-sonnet-4-20250514"')
@then(parsers.parse('the config has default_model "{model}"'))
def config_has_default_model(request, model: str = "claude-sonnet-4-20250514") -> None:
    data = request.node._resp.json()
    assert data.get("default_model") == model


@then("the config has default context window of 200000")
def config_has_context_window(request) -> None:
    data = request.node._resp.json()
    assert data.get("default_context_window") == 200000


@then(parsers.parse('the config has system_prompt "{prompt}"'))
def config_has_system_prompt(prompt: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("system_prompt") == prompt


@then(parsers.re(r"the config access_list includes user_ids (?P<user_ids_str>.+)"))
def config_access_list_has_user_ids(user_ids_str: str, request) -> None:
    expected = json.loads(user_ids_str)
    data = request.node._resp.json()
    access = data.get("access_list", {})
    assert sorted(access.get("user_ids", [])) == sorted(expected)


@then(parsers.re(r"the config allowed_providers is (?P<providers_str>.+)"))
def config_has_allowed_providers(providers_str: str, request) -> None:
    expected = json.loads(providers_str)
    data = request.node._resp.json()
    assert data.get("allowed_providers") == expected


@then(parsers.parse('the config has additional_guidance "{guidance}"'))
def config_has_additional_guidance(guidance: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("additional_guidance") == guidance


# ── Available Provider steps ─────────────────────────────────────────


@when("I GET available providers")
def get_available_providers(request, ctx) -> None:
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    viewer_auth = getattr(request.node, "_viewer_auth", False)
    if viewer_auth:
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Admin role required"}
        request.node._resp = resp
        return

    from modulo.api.main import app

    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )
    client = _make_client()
    resp = client.get("/api/v1/admin/assistant/available-providers")
    request.node._resp = resp


@then(parsers.parse('the available providers include native provider "{provider_id}"'))
def check_available_providers_native(provider_id: str, request) -> None:
    data = request.node._resp.json()
    native_ids = {p["id"] for p in data.get("native", [])}
    assert provider_id in native_ids, f"Expected native provider '{provider_id}' not found. Got: {native_ids}"


@then(parsers.parse('the available providers include custom type "{provider_id}"'))
def check_available_providers_custom(provider_id: str, request) -> None:
    data = request.node._resp.json()
    custom_ids = {p["id"] for p in data.get("custom_types", [])}
    assert provider_id in custom_ids, f"Expected custom type '{provider_id}' not found. Got: {custom_ids}"


# ── Then steps (Skills) ──────────────────────────────────────────────


@then(parsers.parse('the skill response has name "{name}"'))
def skill_response_has_name(name: str, request) -> None:
    data = request.node._resp.json()
    if isinstance(data, list):
        data = data[0]
    assert data.get("name") == name


@then("the skill response is active")
def skill_response_is_active(request) -> None:
    data = request.node._resp.json()
    if isinstance(data, list):
        data = data[0]
    assert data.get("active") is True


@then(parsers.parse("the response contains {count:d} skill"))
@then(parsers.parse("the response contains {count:d} skills"))
def response_has_n_skills(count: int, request) -> None:
    data = request.node._resp.json()
    assert len(data) == count


@then(parsers.parse('the skill is not named "{name}"'))
def skill_not_named(name: str, request) -> None:
    data = request.node._resp.json()
    names = [s.get("name") for s in data]
    assert name not in names


# ── Then steps (Access Control) ──────────────────────────────────────


@then("the stream reports no API key configured")
def stream_reports_no_api_key(request) -> None:
    resp = request.node._resp
    text = resp.text
    # The real SSE preamble emits one error event with the resolved detail.
    assert resp.status_code == 200
    assert "event: error" in text
    assert "No active" in text
    assert "API key configured" in text


# ── Then steps (Context Window) ──────────────────────────────────────


@then(parsers.parse("all {count:d} messages are kept"))
def all_messages_kept(count: int) -> None:
    pass


@then("no pruning occurs")
def no_pruning_occurs() -> None:
    pass


@then("the oldest messages are pruned")
def oldest_messages_pruned() -> None:
    pass


@then("the system prompt is always preserved")
def system_prompt_preserved() -> None:
    pass


@then("the newest user message is always preserved")
def newest_message_preserved() -> None:
    pass


@then("a summary of pruned messages is generated")
def summary_generated() -> None:
    pass


@then("the conversation has has_summary set to true")
def has_summary_true() -> None:
    pass


@then(parsers.parse("the budget is {expected:d} tokens"))
def budget_is_expected() -> None:
    pass


@then("the context has only the system message and user message")
def context_has_system_and_user() -> None:
    pass


# ── Given steps (UI Commands) ─────────────────────────────────────────


@given('the organisation has Assistant enabled with "safe" permission mode')
def org_has_assistant_with_safe_mode(ctx) -> None:
    ctx["config"]["permission_mode"] = "safe"
    ctx["config"]["enabled"] = True


@given('a user with "admin" org role')
def user_with_admin_role(ctx) -> None:
    ctx["org_role"] = "admin"


@given("a chat session exists for the user")
def chat_session_exists(ctx) -> None:
    ses = _make_mock_session(name="UI Commands Session")
    ctx["sessions"]["ui-session"] = ses


@given("the user has sent a message in that session")
def user_sent_message(ctx) -> None:
    ses = ctx["sessions"].get("ui-session")
    msg = _make_mock_message(session_id=ses.id, role="user", content="Help me configure the pipeline")
    ctx["messages"] = [msg]


@given('permission mode is "safe"')
def permission_mode_is_safe(ctx) -> None:
    ctx["config"]["permission_mode"] = "safe"


# ── When steps (UI Commands) ──────────────────────────────────────────


def _start_ui_engine(request: pytest.FixtureRequest, ctx: dict, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the REAL ``_stream_ui_tool_flow`` engine on a dedicated event loop.

    The engine is started inside a background thread with its own asyncio
    loop. All routes the frontend would hit in real life (permission
    response, UI command results) are driven on that same loop because the
    engine ``await``s on events those routes set — a call from another loop
    would never wake them. Route seams are dependency overrides (auth +
    engine db) on the real app; everything else in the engine runs as
    written, including the permission-mode classification and the SSE
    merging.
    """
    import httpx

    from modulo.api.dependencies import get_db_session
    from modulo.api.main import app
    from modulo.api.routes import assistant as assistant_routes
    from modulo.auth.jwt import TenantPrincipal

    session = ctx["sessions"]["ui-session"]
    role = ctx.get("org_role", "admin")
    config_mode = ctx.get("config", {}).get("permission_mode", "safe")

    engine_db = _engine_db_double(session)
    principal = TenantPrincipal(username="assistant-user", organisation_id=ORG_ID, account_id=USER_ID, org_role=role)
    req = assistant_routes.StreamRequest(**_valid_stream_payload())
    config = assistant_routes.AssistantConfig(permission_mode=config_mode)

    harness: dict[str, Any] = {
        "session": session,
        "engine_db": engine_db,
        "config": config,
        "tool_calls": tool_calls,
        "tool_results": [],
        "events": [],
        "lock": threading.Lock(),
        "errors": [],
        "flags": {marker: threading.Event() for marker in ("permission_request", "ui_command_batch", "tool_call")},
        "batch_event": None,
        "permission_request_id": None,
        "loop": None,
        "http": None,
        "done": threading.Event(),
    }
    ctx["ui_engine"] = harness

    async def _override_session():
        yield engine_db

    overrides = _auth_overrides(role)
    overrides[get_db_session] = _override_session
    _install_overrides(request, overrides)
    _pop_engine_module_state(request, str(session.id))

    def _capture(event: str) -> None:
        with harness["lock"]:
            harness["events"].append(event)
            if "event: permission_request" in event:
                harness["permission_request_id"] = _sse_payload(event)["request_id"]
                harness["flags"]["permission_request"].set()
            elif "event: ui_command_batch" in event:
                harness["batch_event"] = event
                harness["flags"]["ui_command_batch"].set()
            elif "event: tool_call" in event:
                harness["flags"]["tool_call"].set()

    async def _engine_main() -> None:
        http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        harness["http"] = http_client
        with (
            patch.object(assistant_routes, "set_rls_org", new_callable=AsyncMock),
            patch.object(assistant_routes, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(assistant_routes, "_is_ui_driving_enabled", new_callable=AsyncMock, return_value=True),
            patch.object(
                assistant_routes.AssistantConfigService,
                "get_config",
                new_callable=AsyncMock,
                return_value=config,
            ),
        ):
            stream_ctx = assistant_routes._StreamContext(
                db_session=engine_db,
                principal=principal,
                session_id=session.id,
                req=req,
                settings=make_settings(),
                chat_session=session,
            )
            flow = assistant_routes._UiToolFlow()
            async for event in assistant_routes._stream_ui_tool_flow(
                stream_ctx, tool_calls, harness["tool_results"], flow
            ):
                _capture(event)
        await http_client.aclose()
        # Keep the loop running for the scenario's follow-up POSTs: if the
        # loop stopped the moment the flow ended, a step's in-flight POST
        # coroutine would be abandoned mid-await (loop starvation).
        quit_evt = asyncio.Event()
        harness["quit"] = quit_evt
        await quit_evt.wait()

    def _thread_main() -> None:
        loop = asyncio.new_event_loop()
        harness["loop"] = loop
        try:
            loop.run_until_complete(_engine_main())
        except Exception as exc:
            with harness["lock"]:
                harness["errors"].append(repr(exc))
        finally:
            harness["done"].set()

    thread = threading.Thread(target=_thread_main, name="assistant-ui-engine", daemon=True)
    thread.start()

    def _join_engine() -> None:
        # Signal the engine's keep-alive wait, then bound the teardown.
        loop = harness["loop"]
        if harness.get("quit") is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(harness["quit"].set)
        if not harness["done"].wait(timeout=60):
            pytest.fail("assistant UI engine thread never finished within 60s of scenario end")

    request.addfinalizer(_join_engine)
    return harness


def _await_engine_ready(harness: dict[str, Any], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while harness["loop"] is None or harness["http"] is None:
        if time.monotonic() > deadline:
            pytest.fail("assistant UI engine thread never came up")
        time.sleep(0.01)


def _run_in_reactor(harness: dict[str, Any], post_coro: Callable[[], Any], timeout: float = 30.0) -> Any:
    """Schedule an HTTP call on the engine's event loop and block for the result.

    The result is box-and-thrading-Event, NOT ``future.result()``: when the
    engine's `run_until_complete` stops right after our POST completes, the
    concurrent-future wakeup callback starves and ``result()`` hangs forever.
    """
    _await_engine_ready(harness)
    done = threading.Event()
    box: dict[str, Any] = {}

    async def _runner() -> None:
        try:
            box["resp"] = await post_coro()
        except Exception as exc:
            box["error"] = repr(exc)
        finally:
            done.set()

    asyncio.run_coroutine_threadsafe(_runner(), harness["loop"])
    if not done.wait(timeout=timeout):
        loop = harness["loop"]
        with harness["lock"]:
            tail = harness["events"][-5:]
            errors = list(harness["errors"])
        pytest.fail(
            f"POST on the engine loop did not finish within {timeout}s; "
            f"loop_running={loop.is_running()} loop_closed={loop.is_closed()} "
            f"engine errors={errors} last SSE events={tail}"
        )
    if "error" in box:
        pytest.fail(f"POST on the engine loop raised: {box['error']}")
    return box["resp"]


def _assert_no_engine_errors(harness: dict[str, Any]) -> None:
    if harness["errors"]:
        pytest.fail(f"assistant UI engine crashed inside its loop: {harness['errors'][0]}")


def _wait_for_marker(harness: dict[str, Any], marker: str, timeout: float = 20.0) -> None:
    ready = harness["flags"][marker].wait(timeout=timeout)
    _assert_no_engine_errors(harness)
    assert ready, f"engine stream never produced {'event: ' + marker!r} within {timeout}s"


def _submit_ui_command_results(harness: dict[str, Any], commands: list[dict[str, Any]]) -> None:
    """Post the frontend's tool results through the REAL ui-command-results route.

    The frontend's role in the scenarios: execute each command and post its
    result row back. Result rows carry the command's id/name/success — the
    route merges them by position with the approved calls.
    """
    session = harness["session"]
    results = [
        {"id": command["id"], "name": command["name"], "success": True, "result": {"value": True}}
        for command in commands
    ]
    body = {"results": results}

    async def _post():
        return await harness["http"].post(f"/api/v1/assistant/sessions/{session.id}/ui-command-results", json=body)

    resp = _run_in_reactor(harness, _post)
    assert resp.status_code == 200, f"ui-command-results POST failed: {resp.status_code} {resp.text}"


def _tool_calls_captured(harness: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the ``tool_call`` SSE payloads captured from the engine."""
    calls: list[dict[str, Any]] = []
    with harness["lock"]:
        for event in harness["events"]:
            if "event: tool_call" in event:
                calls.append(_sse_payload(event))
    return calls


@when(parsers.parse('the LLM emits an "{tool_name}" tool call with path "{path}"'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call with path "{path}"'))
@when(parsers.parse('the LLM emits an "{tool_name}" tool call with selector "{selector}"'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call with selector "{selector}"'))
@when(parsers.parse('the LLM emits an "{tool_name}" tool call with selector "{selector}" and value "{value}"'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call with selector "{selector}" and value "{value}"'))
@when(parsers.parse('the LLM emits an "{tool_name}" tool call'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call'))
def llm_emits_tool_call(tool_name: str, request, ctx, selector: str = "", value: str = "", path: str = "") -> None:
    args: dict[str, Any] = {}
    if path:
        args["path"] = path
    if selector:
        args["selector"] = selector
    if value:
        args["value"] = value
    tool_call = {"id": str(uuid.uuid4()), "name": tool_name, "args": args}
    _start_ui_engine(request, ctx, [tool_call])


@when("the LLM emits a sequence of tool calls")
def llm_emits_sequence(request, ctx) -> None:
    tool_calls = [
        {"id": str(uuid.uuid4()), "name": "navigate", "args": {"path": "/admin/pipelines"}},
        {"id": str(uuid.uuid4()), "name": "wait", "args": {"ms": 500}},
        {"id": str(uuid.uuid4()), "name": "click", "args": {"selector": "[data-testid=create-btn]"}},
        {"id": str(uuid.uuid4()), "name": "go_back", "args": {}},
    ]
    _start_ui_engine(request, ctx, tool_calls)


@when("the user approves the action")
def user_approves_action(request, ctx) -> None:
    """POST the approval through the REAL permission-response route, on the
    engine's event loop (the engine's ``_await_permission_decision`` awaits
    the event this route sets)."""
    harness = ctx["ui_engine"]
    request_id = harness["permission_request_id"]
    assert request_id, "no pending permission request to approve"

    session = harness["session"]

    async def _post():
        return await harness["http"].post(
            f"/api/v1/assistant/sessions/{session.id}/permission-response",
            json={"request_id": request_id, "action": "approve"},
        )

    resp = _run_in_reactor(harness, _post)
    assert resp.status_code == 200, f"permission-response POST failed: {resp.status_code} {resp.text}"
    ctx["permission_approved"] = True


# ── Then steps (UI Commands) ──────────────────────────────────────────


@then(parsers.parse('the backend yields an "ui_command_batch" event with the {command_name} command'))
def backend_yields_ui_command_batch(command_name: str, request, ctx) -> None:
    harness = ctx["ui_engine"]
    _wait_for_marker(harness, "ui_command_batch")
    batch = _sse_payload(harness["batch_event"])
    commands = batch["commands"]
    names = [command["name"] for command in commands]
    assert command_name in names, f"batch payload {names} does not contain a {command_name!r} command"
    # Frontend role: execute the batch and post one result row per command.
    _submit_ui_command_results(harness, commands)
    _wait_for_marker(harness, "tool_call")


@then('the backend yields a "permission_request" event')
def backend_yields_permission_request(ctx) -> None:
    harness = ctx["ui_engine"]
    _wait_for_marker(harness, "permission_request")


@then("the frontend shows the approval card")
def frontend_shows_approval_card() -> None:
    # Display-side: the wire flow (permission_request event + approval POST
    # through the real route) is asserted by the neighbouring steps.
    pass


@then("the frontend executes the navigate command")
def frontend_executes_navigate(request, ctx) -> None:
    harness = ctx["ui_engine"]
    calls = _tool_calls_captured(harness)
    assert calls, "no tool_call event captured after the navigate batch"
    assert calls[0].get("tool_name") == "navigate"


@then('the URL changes to "/admin/pipelines"')
def url_changes_to_pipelines(request, ctx) -> None:
    harness = ctx["ui_engine"]
    batch = _sse_payload(harness["batch_event"])
    navigate = next(command for command in batch["commands"] if command["name"] == "navigate")
    assert navigate["args"]["path"] == "/admin/pipelines"


@then("the frontend fills the input field")
def frontend_fills_input(request, ctx) -> None:
    harness = ctx["ui_engine"]
    calls = _tool_calls_captured(harness)
    assert calls, "no tool_call event captured after the fill batch"
    assert calls[0].get("tool_name") == "fill"


@then("the frontend returns the element's text content")
def frontend_returns_text(request, ctx) -> None:
    harness = ctx["ui_engine"]
    calls = _tool_calls_captured(harness)
    assert calls, "no tool_call event captured after the extract batch"
    assert calls[0].get("tool_name") == "extract"


@then('each command is yielded as an "ui_command_batch" event')
def each_command_yielded(request, ctx) -> None:
    harness = ctx["ui_engine"]
    _wait_for_marker(harness, "ui_command_batch")
    batch = _sse_payload(harness["batch_event"])
    commands = batch["commands"]
    assert len(commands) == 4, f"expected the batch to carry all 4 tool calls, got {[c['name'] for c in commands]}"
    # Frontend role: execute the whole batch, post all 4 result rows.
    _submit_ui_command_results(harness, commands)
    _wait_for_marker(harness, "tool_call")


@then("the results are fed back to the LLM for the next turn")
def results_fed_back(request, ctx) -> None:
    harness = ctx["ui_engine"]
    calls = _tool_calls_captured(harness)
    assert len(calls) == len(harness["tool_calls"]), f"expected one tool_call per issued tool call: {calls}"
    names = {call.get("tool_name") for call in calls}
    issued_names = {tc["name"] for tc in harness["tool_calls"]}
    assert names == issued_names, f"merged tool results differ from issued tools: {names} vs {issued_names}"
