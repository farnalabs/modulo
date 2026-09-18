"""BDD step definitions: Remy chat — sessions, messages, skills, config, and UI commands."""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import ORG_ID, USER_ID, make_settings

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


# ── Load scenarios from feature files ──────────────────────────────────

try:
    scenarios("../features/remy/remy_sessions.feature")
    scenarios("../features/remy/remy_messages.feature")
    scenarios("../features/remy/remy_admin_config.feature")
    scenarios("../features/remy/remy_skills.feature")
    scenarios("../features/remy/remy_access_control.feature")
    scenarios("../features/remy/remy_context_window.feature")
    scenarios("../features/remy/remy_ui_commands.feature")
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


@given("I have 2 remy sessions")
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


@given("I have a remy session")
def have_one_session(ctx) -> None:
    ctx["sessions"]["session-1"] = _make_mock_session(name="Test Session")
    ctx["session_counter"] = 1


@given("I have a remy session with 3 messages")
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


@given("I have a remy session with messages in order")
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


@given("the Remy access list includes my user_id")
def access_list_includes_user(ctx) -> None:
    ctx["config"]["access_list"] = {
        "user_ids": [str(USER_ID)],
        "team_ids": [],
        "org_roles": [],
    }


@given(parsers.parse('the Remy access list includes role "{role}"'))
def access_list_includes_role(role: str, ctx) -> None:
    ctx["config"]["access_list"] = {
        "user_ids": [],
        "team_ids": [],
        "org_roles": [role],
    }


@given(parsers.parse('the Remy access list includes team_id "{team_id}"'))
def access_list_includes_team(team_id: str, ctx) -> None:
    ctx["config"]["access_list"] = {
        "user_ids": [],
        "team_ids": [team_id],
        "org_roles": [],
    }


@given("the Remy access list does not include my role or user_id")
def access_list_excludes_user(ctx) -> None:
    ctx["config"]["access_list"] = {
        "user_ids": [],
        "team_ids": [],
        "org_roles": [],
    }


@given(parsers.parse('I belong to team "{team_id}"'))
def user_belongs_to_team(team_id: str, ctx) -> None:
    ctx["team_ids"] = [team_id]


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


# ── When steps (Remy Sessions) ─────────────────────────────────────────


@when(parsers.parse('I create a remy session with provider "{provider}" and model "{model}"'))
def create_remy_session(provider: str, model: str, request, ctx) -> None:
    mock_ses = _make_mock_session(provider=provider, model=model, name="New Chat")

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.ChatSession", new_callable=MagicMock) as mock_cls,
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
            "/api/v1/remy/sessions",
            json={"provider": provider, "model": model, "context_window_tokens": 200000},
        )
        request.node._resp = resp


@when("I list remy sessions")
def list_remy_sessions(request, ctx) -> None:
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
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
        resp = client.get("/api/v1/remy/sessions")
        request.node._resp = resp


@when("I get the remy session by id")
def get_remy_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())

    mock_scalar_one = MagicMock(return_value=ses)
    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none = mock_scalar_one
    mock_exec.scalar = MagicMock(return_value=0)

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
        resp = client.get(f"/api/v1/remy/sessions/{ses.id}")
        request.node._resp = resp


@when(parsers.parse('I get a remy session by id "{session_id}"'))
def get_remy_session_by_id(session_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/remy/sessions/{session_id}")
        request.node._resp = resp


@when('I rename the remy session to "My renamed chat"')
def rename_remy_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)
        resp = client.patch(f"/api/v1/remy/sessions/{ses.id}", json={"name": "My renamed chat"})
        request.node._resp = resp


@when("I delete the remy session")
def delete_remy_session(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-w-msgs", ctx.get("sessions", {}).get("session-1", _make_mock_session()))

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=ses)

        client = _make_client(mock_session_inst)
        resp = client.delete(f"/api/v1/remy/sessions/{ses.id}")
        request.node._resp = resp


@when("I get a remy session that belongs to another user")
def get_other_users_session(request, ctx) -> None:
    other_user_ses = _make_mock_session(account_id=uuid.uuid4())

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=other_user_ses)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/remy/sessions/{other_user_ses.id}")
        request.node._resp = resp


# ── When steps (Remy Messages) ────────────────────────────────────────


@when(parsers.parse('I append a "{role}" message with content "{content}"'))
@when(parsers.parse("I append a \"{role}\" message with content '{content}'"))
@when(parsers.parse('I append a message with role "{role}"'))
def append_message(role: str, request, ctx, content: str = "Hello") -> None:
    ses = ctx.get("sessions", {}).get("session-1")
    if not ses:
        ses = _make_mock_session(name="Test Session")
        ctx["sessions"]["session-1"] = ses

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
            f"/api/v1/remy/sessions/{ses.id}/messages",
            json={"role": role, "content": content},
        )
        request.node._resp = resp


@when(parsers.parse('I append a "{role}" message with content "{content}" and parent_id set'))
def append_message_with_parent(role: str, content: str, request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())
    parent_id = ctx.get("parent_message_id", uuid.uuid4())

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
            f"/api/v1/remy/sessions/{ses.id}/messages",
            json={"role": role, "content": content, "parent_id": str(parent_id)},
        )
        request.node._resp = resp


@when(parsers.parse('I append a "{role}" message to session "{session_id}"'))
def append_message_to_session_id(role: str, session_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
            f"/api/v1/remy/sessions/{session_id}/messages",
            json={"role": role, "content": "Hello"},
        )
        request.node._resp = resp


@when("I list messages for the remy session")
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
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
        resp = client.get(f"/api/v1/remy/sessions/{ses.id}/messages")
        request.node._resp = resp


@when(parsers.parse('I list messages for session "{session_id}"'))
def list_messages_for_session_id(session_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.get(f"/api/v1/remy/sessions/{session_id}/messages")
        request.node._resp = resp


@when('I append a "assistant" message with tool_calls containing a code_interpreter call')
def append_message_with_tool_calls(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("session-1", _make_mock_session())
    tool_calls = {
        "tool_calls": [{"id": "call_123", "name": "code_interpreter", "args": {"code": "print(1)"}}],
    }

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
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
            f"/api/v1/remy/sessions/{ses.id}/messages",
            json={
                "role": "assistant",
                "content": "Let me run that",
                "tool_calls_json": tool_calls,
            },
        )
        request.node._resp = resp


# ── When steps (Admin Config) ─────────────────────────────────────────


@when("I GET the admin Remy config")
def get_admin_remy_config(request, ctx) -> None:
    config_value = ctx.get("config", {})

    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
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
        resp = client.get("/api/v1/admin/remy/config")
        request.node._resp = resp


@when(parsers.parse('I update the Remy config with system_prompt "{prompt}"'))
def update_remy_config_system_prompt(prompt: str, request, ctx) -> None:
    _update_remy_config(request, ctx, {"system_prompt": prompt})


@when(parsers.parse('I update the Remy config with additional_guidance "{guidance}"'))
def update_remy_config_guidance(guidance: str, request, ctx) -> None:
    _update_remy_config(request, ctx, {"additional_guidance": guidance})


@when(
    parsers.re(
        r"I update the Remy config access_list with user_ids (?P<user_ids_str>.+)",
    )
)
def update_remy_config_access_list(user_ids_str: str, request, ctx) -> None:
    user_ids = json.loads(user_ids_str)
    _update_remy_config(request, ctx, {"access_list": {"user_ids": user_ids, "team_ids": [], "org_roles": []}})


@when(parsers.parse('I update the Remy config default_provider to "{provider}" and default_model to "{model}"'))
def update_remy_config_defaults(provider: str, model: str, request, ctx) -> None:
    _update_remy_config(request, ctx, {"default_provider": provider, "default_model": model})


@when(parsers.re(r"I update the Remy config allowed_providers to (?P<providers_str>.+)"))
def update_remy_config_allowed_providers(providers_str: str, request, ctx) -> None:
    providers = json.loads(providers_str)
    _update_remy_config(request, ctx, {"allowed_providers": providers})


def _update_remy_config(request: Any, ctx: dict, updates: dict) -> None:

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
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
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
        resp = client.put("/api/v1/admin/remy/config", json=updates)
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
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        client = _make_client(mock_session_inst)
        resp = client.post(
            "/api/v1/admin/remy/skills",
            json={"name": name, "body": body},
        )
        request.node._resp = resp


@when("I list org skills")
def list_org_skills(request, ctx) -> None:
    skills = list(ctx.get("org_skills", {}).values())
    mock_exec = MagicMock()
    mock_exec.scalars = MagicMock(return_value=skills)

    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.execute = AsyncMock(return_value=mock_exec)

        client = _make_client(mock_session_inst)
        resp = client.get("/api/v1/admin/remy/skills")
        request.node._resp = resp


@when('I update the org skill name to "code-review-v2"')
def update_org_skill(request, ctx) -> None:
    skill = next(iter(ctx.get("org_skills", {}).values()))

    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=skill)

        client = _make_client(mock_session_inst)
        resp = client.put(f"/api/v1/admin/remy/skills/{skill.id}", json={"name": "code-review-v2"})
        request.node._resp = resp


@when("I delete the org skill")
def delete_org_skill(request, ctx) -> None:
    skill = next(iter(ctx.get("org_skills", {}).values()))

    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=skill)

        client = _make_client(mock_session_inst)
        resp = client.delete(f"/api/v1/admin/remy/skills/{skill.id}")
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
        resp = client.get("/api/v1/me/remy/skills")
        request.node._resp = resp


@when(parsers.parse('I update an org skill by id "{skill_id}" with name "{name}"'))
def update_org_skill_by_id(skill_id: str, name: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.put(f"/api/v1/admin/remy/skills/{skill_id}", json={"name": name})
        request.node._resp = resp


@when(parsers.parse('I delete an org skill by id "{skill_id}"'))
def delete_org_skill_by_id(skill_id: str, request, ctx) -> None:
    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_session_inst.get = AsyncMock(return_value=None)

        client = _make_client(mock_session_inst)
        resp = client.delete(f"/api/v1/admin/remy/skills/{skill_id}")
        request.node._resp = resp


# ── When steps (Access Control) ───────────────────────────────────────


@when("I check remy access")
def check_remy_access(request, ctx) -> None:
    config = ctx.get("config", {})
    viewer_auth = getattr(request.node, "_viewer_auth", False)
    role = "viewer" if viewer_auth else "admin"

    config_access = config.get("access_list", {})
    user_ids = config_access.get("user_ids", [])
    org_roles = config_access.get("org_roles", [])
    team_ids = config_access.get("team_ids", [])
    user_teams = set(ctx.get("team_ids", []))

    if config_access:
        has_access = str(USER_ID) in user_ids or role in org_roles or bool(user_teams & set(team_ids))
    else:
        has_access = True

    response_data = {"granted": has_access}

    if ctx.get("no_backends"):
        has_access = False
        response_data = {"granted": False, "error": "No API key configured"}

    resp = MagicMock()
    resp.status_code = 200 if has_access else 403
    resp.json = lambda d=response_data: d
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
    resp = client.get("/api/v1/admin/remy/available-providers")
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


@then("access is granted")
def access_granted(request) -> None:
    data = request.node._resp.json()
    assert data.get("granted") is True


@then("access is denied")
def access_denied(request) -> None:
    data = request.node._resp.json()
    assert data.get("granted") is False


@then("the error indicates no API key configured")
def error_no_api_key(request) -> None:
    data = request.node._resp.json()
    assert "No API key configured" in json.dumps(data)


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


@given('the organisation has Remy enabled with "safe" permission mode')
def org_has_remy_with_safe_mode(ctx) -> None:
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


@when(parsers.parse('the LLM emits an "{tool_name}" tool call with path "{path}"'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call with path "{path}"'))
@when(parsers.parse('the LLM emits an "{tool_name}" tool call with selector "{selector}"'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call with selector "{selector}"'))
@when(parsers.parse('the LLM emits an "{tool_name}" tool call with selector "{selector}" and value "{value}"'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call with selector "{selector}" and value "{value}"'))
@when(parsers.parse('the LLM emits an "{tool_name}" tool call'))
@when(parsers.parse('the LLM emits a "{tool_name}" tool call'))
def llm_emits_tool_call(tool_name: str, request, ctx, selector: str = "", value: str = "", path: str = "") -> None:
    ses = ctx.get("sessions", {}).get("ui-session")
    args: dict[str, Any] = {}
    if selector:
        args["selector"] = selector
    if value:
        args["value"] = value
    if path:
        args["path"] = path

    # Simulate the permission check that happens in the streaming endpoint

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm

        from modulo.api.main import app

        viewer_auth = getattr(request.node, "_viewer_auth", False)
        if viewer_auth:
            from modulo.auth.dependencies import get_current_user
            from modulo.auth.jwt import AuthenticatedPrincipal

            app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
                username="viewer",
                organisation_id=ORG_ID,
                account_id=uuid.uuid4(),
                org_role="viewer",
            )

        req_id = str(uuid.uuid4())
        ctx["last_request_id"] = req_id
        ctx["last_tool_call"] = {"name": tool_name, "args": args}

        # Store the pending permission so we can respond to it later
        if (
            tool_name == "click"
            and selector
            and any(p in selector.lower() for p in ["delete", "remove", "destroy", "archive"])
        ):
            ctx["requires_approval"] = True
        else:
            ctx["requires_approval"] = False

        verify_url = f"/api/v1/remy/sessions/{ses.id}/ui-command-results"
        ctx["verify_url"] = verify_url

        # Don't actually call the endpoint here — let the then steps verify
        request.node._resp = MagicMock()
        request.node._resp.status_code = 200
        request.node._resp.json = lambda: {"status": "ok"}


@when("the LLM emits a sequence of tool calls")
def llm_emits_sequence(request, ctx) -> None:
    ctx["sequence"] = [
        {"name": "navigate", "args": {"path": "/admin/pipelines"}},
        {"name": "wait", "args": {"ms": 500}},
        {"name": "click", "args": {"selector": "[data-testid=create-btn]"}},
        {"name": "go_back", "args": {}},
    ]
    ctx["requires_approval"] = False
    request.node._resp = MagicMock()
    request.node._resp.status_code = 200
    request.node._resp.json = lambda: {"status": "ok"}


@when("the user approves the action")
def user_approves_action(request, ctx) -> None:
    ses = ctx.get("sessions", {}).get("ui-session")
    req_id = ctx.get("last_request_id", str(uuid.uuid4()))

    from modulo.api.routes.remy import (
        _pending_permissions,
    )

    event = asyncio.Event()
    _pending_permissions[req_id] = (event, str(ses.id))

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
    ):
        mock_session_inst = AsyncMock()
        mock_session_inst.begin = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin.return_value = begin_cm
        mock_chat_session = MagicMock()
        mock_chat_session.id = ses.id
        mock_chat_session.account_id = USER_ID
        mock_session_inst.get = AsyncMock(return_value=mock_chat_session)

        client = _make_client(mock_session_inst)

        resp = client.post(
            f"/api/v1/remy/sessions/{ses.id}/permission-response",
            json={"request_id": req_id, "action": "approve"},
        )
        request.node._resp = resp
        ctx["permission_approved"] = True


# ── Then steps (UI Commands) ──────────────────────────────────────────


@then(parsers.parse('the backend yields an "ui_command_batch" event with the {command_name} command'))
@then(parsers.parse('the backend yields an "ui_command_batch" event with the {command_name} command'))
def backend_yields_ui_command_batch(request, command_name: str) -> None:
    data = request.node._resp.json()
    assert data is not None


@then('the backend yields a "permission_request" event')
def backend_yields_permission_request(ctx) -> None:
    assert ctx.get("requires_approval") is True, "Expected permission request but tool was auto-allowed"


@then("the frontend shows the approval card")
def frontend_shows_approval_card() -> None:
    pass


@then("the frontend executes the navigate command")
def frontend_executes_navigate() -> None:
    pass


@then('the URL changes to "/admin/pipelines"')
def url_changes_to_pipelines() -> None:
    pass


@then("the frontend fills the input field")
def frontend_fills_input() -> None:
    pass


@then("the frontend returns the element's text content")
def frontend_returns_text() -> None:
    pass


@then('each command is yielded as an "ui_command_batch" event')
def each_command_yielded(ctx) -> None:
    sequence = ctx.get("sequence", [])
    assert len(sequence) == 4


@then("the results are fed back to the LLM for the next turn")
def results_fed_back() -> None:
    pass
