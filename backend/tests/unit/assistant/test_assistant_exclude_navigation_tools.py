"""Unit coverage for the assistant-only stream mode (exclude_navigation_tools flag, FAR-109).

Follows the direct-call pattern of ``test_stream_tool_credentials.py``:
``stream_chat`` is invoked directly with a patched ``AsyncSession``,
``_build_backend``, ``_get_all_tool_definitions`` and ``SkillLoader``. These
tests assert on the real behaviour of the ``exclude_navigation_tools`` flag:

* tool assembly is gated (``include_navigation=False``) when the flag is set;
* the text-mode fallback (``include_ui_tools_text``) is disabled in every mode;
* navigation tool calls are resolved locally against the app manifest and
  reported as ``tool_call`` events with ``tool_name="get_manifest"``.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessageChunk

from modulo.api.routes.assistant import StreamRequest, stream_chat
from modulo.auth.jwt import TenantPrincipal
from modulo.db.models.assistant_message import ChatMessage


class _BeginContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeSession:
    def __init__(self, chat_session: SimpleNamespace | None = None) -> None:
        self.bind = object()
        self._chat_session = chat_session
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _BeginContext:
        return _BeginContext()

    async def get(self, _model: object, _identifier: uuid.UUID) -> SimpleNamespace | None:
        return self._chat_session

    def add(self, value: object) -> None:
        if isinstance(value, ChatMessage) and value.id is None:
            value.id = uuid.uuid4()
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _Backend:
    supports_tools = True

    def __init__(self, first_turn_calls: list[dict[str, Any]]) -> None:
        self._first_turn_calls = first_turn_calls
        self.turns = 0

    async def stream(self, _messages: list[object], **_kwargs: object) -> AsyncIterator[AIMessageChunk]:
        self.turns += 1
        if self.turns == 1 and self._first_turn_calls:
            yield AIMessageChunk(content="Working", tool_call_chunks=self._first_turn_calls)
        else:
            yield AIMessageChunk(content="Finished")


class _TextBackend(_Backend):
    supports_tools = False


def _tool_call(name: str, call_id: str, index: int) -> dict[str, Any]:
    return {"name": name, "args": "{}", "id": call_id, "index": index}


async def _read_sse(response: object) -> list[tuple[str, dict[str, Any]]]:
    chunks: list[str] = [
        chunk.decode() if isinstance(chunk, bytes) else chunk
        async for chunk in response.body_iterator  # type: ignore[attr-defined]
    ]

    events: list[tuple[str, dict[str, Any]]] = []
    for block in "".join(chunks).split("\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event, data))
    return events


def _make_principal(account_id: uuid.UUID, organisation_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="user@example.com",
        organisation_id=organisation_id,
        account_id=account_id,
        org_role="admin",
    )


def _make_request(exclude_navigation_tools: bool = False, mcp_api_key: str | None = None) -> StreamRequest:
    return StreamRequest(
        content="Help me",
        provider="stub",
        model="stub-model",
        api_key="provider-key",
        mcp_api_key=mcp_api_key,
        exclude_navigation_tools=exclude_navigation_tools,
    )


async def _run_stream(
    *,
    exclude_navigation_tools: bool,
    backend: _Backend,
    captured_tool_defs: dict[str, Any] | None = None,
    manifest_mock: MagicMock | None = None,
    loader_build_prompt: AsyncMock | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    session_id = uuid.uuid4()
    account_id = uuid.uuid4()
    organisation_id = uuid.uuid4()
    chat_session = SimpleNamespace(
        account_id=account_id,
        context_window_tokens=200_000,
        name="Existing session",
    )
    request_session = _FakeSession(chat_session)
    stream_session = _FakeSession()
    loader = MagicMock()
    loader.build_system_prompt = loader_build_prompt or AsyncMock(return_value="")
    call_mcp_tool = AsyncMock()

    principal = _make_principal(account_id, organisation_id)
    request = _make_request(exclude_navigation_tools=exclude_navigation_tools)
    settings = SimpleNamespace(modulo_public_url="http://test", fernet_key="unused")

    tool_defs_mock = MagicMock(side_effect=None)
    if captured_tool_defs is not None:

        def _capture(**kwargs: Any) -> list[dict[str, Any]]:
            captured_tool_defs.update(kwargs)
            return []

        tool_defs_mock.side_effect = _capture
    else:
        tool_defs_mock.return_value = []

    patches = [
        patch("modulo.api.routes.assistant.AsyncSession", return_value=stream_session),
        patch("modulo.api.routes.assistant.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.assistant._build_backend", return_value=backend),
        patch("modulo.api.routes.assistant.SkillLoader", return_value=loader),
        patch("modulo.api.routes.assistant._reconstruct_messages", new_callable=AsyncMock, return_value=[]),
        patch("modulo.api.routes.assistant._call_mcp_tool", call_mcp_tool),
        patch("modulo.api.routes.assistant._get_all_tool_definitions", tool_defs_mock),
    ]
    if manifest_mock is not None:
        patches.append(patch("modulo.core.manifest.get_manifest", manifest_mock))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        response = await stream_chat(
            session_id=session_id,
            req=request,
            request=_Request(),  # type: ignore[arg-type]
            session=request_session,  # type: ignore[arg-type]
            principal=principal,
            settings=settings,  # type: ignore[arg-type]
        )
        return await _read_sse(response)


# ── tool assembly gating ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exclude_navigation_tools_true_passes_include_navigation_false() -> None:
    captured: dict[str, Any] = {}
    backend = _Backend([])
    events = await _run_stream(
        exclude_navigation_tools=True,
        backend=backend,
        captured_tool_defs=captured,
    )
    assert captured.get("include_navigation") is False
    assert backend.turns == 1
    assert events[-1][0] == "done"
    assert all(event != "error" for event, _data in events)


@pytest.mark.asyncio
async def test_exclude_navigation_tools_false_keeps_include_navigation_true() -> None:
    captured: dict[str, Any] = {}
    backend = _Backend([])
    events = await _run_stream(
        exclude_navigation_tools=False,
        backend=backend,
        captured_tool_defs=captured,
    )
    assert captured.get("include_navigation") is True
    assert backend.turns == 1
    assert events[-1][0] == "done"


# ── text-mode backend fallback ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exclude_navigation_tools", [True, False])
async def test_text_mode_backend_never_includes_browser_tools_text(exclude_navigation_tools: bool) -> None:
    build_prompt = AsyncMock(return_value="")
    backend = _TextBackend([])
    events = await _run_stream(
        exclude_navigation_tools=exclude_navigation_tools,
        backend=backend,
        loader_build_prompt=build_prompt,
    )
    assert build_prompt.await_count == 1
    assert build_prompt.await_args is not None
    kwargs = build_prompt.await_args.kwargs
    assert kwargs["include_ui_tools_text"] is False
    assert backend.turns == 1
    assert events[-1][0] == "done"


# ── navigation calls resolved locally ────────────────────────────────────


@pytest.mark.asyncio
async def test_navigate_call_is_resolved_locally_as_manifest_result() -> None:
    manifest_mock = MagicMock(
        return_value={
            "routes": {"/admin/pipelines": {"name": "Pipelines", "testid": "pipelines-view", "type": "page"}},
            "elements": {"/admin/pipelines": [{"testid": "create-btn"}]},
            "sidebar_groups": {},
        }
    )
    backend = _Backend([_tool_call("navigate", "nav-1", 0)])
    events = await _run_stream(
        exclude_navigation_tools=False,
        backend=backend,
        manifest_mock=manifest_mock,
    )

    tool_events = [data for event, data in events if event == "tool_call"]
    assert len(tool_events) == 1
    result = tool_events[0]
    assert result["tool_name"] == "get_manifest"
    assert result["success"] is True
    assert result["result"]["routes"]["/admin/pipelines"]["name"] == "Pipelines"
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_get_manifest_call_with_path_returns_single_route() -> None:
    manifest_mock = MagicMock(
        return_value={
            "routes": {"/admin/pipelines": {"name": "Pipelines"}},
            "elements": {"/admin/pipelines": [{"testid": "create-btn"}]},
            "sidebar_groups": {},
        }
    )
    backend = _Backend(
        [
            {
                "name": "get_manifest",
                "args": '{"path": "/admin/pipelines"}',
                "id": "manifest-1",
                "index": 0,
            }
        ]
    )
    events = await _run_stream(
        exclude_navigation_tools=True,
        backend=backend,
        manifest_mock=manifest_mock,
    )

    tool_events = [data for event, data in events if event == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["result"] == {
        "route": {"name": "Pipelines"},
        "elements": [{"testid": "create-btn"}],
    }
    assert events[-1][0] == "done"
