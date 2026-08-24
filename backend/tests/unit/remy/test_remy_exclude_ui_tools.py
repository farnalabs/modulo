"""Unit coverage for the remy-only stream mode (exclude_ui_tools flag, FAR-109).

Follows the direct-call pattern of ``test_stream_tool_credentials.py``:
``stream_chat`` is invoked directly with a patched ``AsyncSession``,
``_build_backend``, ``_is_ui_driving_enabled``, ``_get_all_tool_definitions``
and ``SkillLoader``. These tests assert on the real behaviour of the
``exclude_ui_tools`` flag:

* tool assembly is gated (``include_ui_tools=False``) when the flag is set;
* the text-mode fallback (``include_ui_tools_text``) is disabled when the flag
  is set on a backend without tool support;
* a model-emitted UI tool call under the flag yields a ``tool_call`` event with
  ``success=False`` and the distinct error, and emits NO ``ui_command_batch``
  or ``permission_request`` events;
* a ``get_manifest`` call under the flag is refused server-side and the
  manifest module is never invoked.
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

from modulo.api.routes.remy import StreamRequest, stream_chat
from modulo.auth.jwt import TenantPrincipal
from modulo.db.models.remy_message import ChatMessage

UI_DRIVING_UNAVAILABLE = "UI driving is not available in this view"


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


def _make_request(exclude_ui_tools: bool = False, mcp_api_key: str | None = None) -> StreamRequest:
    return StreamRequest(
        content="Help me",
        provider="stub",
        model="stub-model",
        api_key="provider-key",
        mcp_api_key=mcp_api_key,
        exclude_ui_tools=exclude_ui_tools,
    )


async def _run_stream(
    *,
    exclude_ui_tools: bool,
    ui_driving_enabled: bool,
    backend: _Backend,
    captured_tool_defs: dict[str, Any] | None = None,
    manifest_mock: MagicMock | None = None,
    loader_build_prompt: AsyncMock | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    session_id = uuid.uuid4()
    account_id = uuid.uuid4()
    organisation_id = uuid.uuid4()
    chat_session = SimpleNamespace(
        user_id=account_id,
        context_window_tokens=200_000,
        name="Existing session",
    )
    request_session = _FakeSession(chat_session)
    stream_session = _FakeSession()
    loader = MagicMock()
    loader.build_system_prompt = loader_build_prompt or AsyncMock(return_value="")
    call_mcp_tool = AsyncMock()

    principal = _make_principal(account_id, organisation_id)
    request = _make_request(exclude_ui_tools=exclude_ui_tools)
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
        patch("modulo.api.routes.remy.AsyncSession", return_value=stream_session),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy._build_backend", return_value=backend),
        patch("modulo.api.routes.remy.SkillLoader", return_value=loader),
        patch("modulo.api.routes.remy._reconstruct_messages", new_callable=AsyncMock, return_value=[]),
        patch("modulo.api.routes.remy._call_mcp_tool", call_mcp_tool),
        patch("modulo.api.routes.remy._get_all_tool_definitions", tool_defs_mock),
        patch(
            "modulo.api.routes.remy._is_ui_driving_enabled",
            new_callable=AsyncMock,
            return_value=ui_driving_enabled,
        ),
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


# ── (a) / (b) tool assembly gating ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_exclude_ui_tools_true_passes_include_ui_tools_false() -> None:
    captured: dict[str, Any] = {}
    backend = _Backend([])
    events = await _run_stream(
        exclude_ui_tools=True,
        ui_driving_enabled=True,
        backend=backend,
        captured_tool_defs=captured,
    )
    assert captured.get("include_ui_tools") is False
    assert backend.turns == 1
    assert events[-1][0] == "done"
    assert all(event != "error" for event, _data in events)


@pytest.mark.asyncio
async def test_exclude_ui_tools_false_keeps_include_ui_tools_true() -> None:
    captured: dict[str, Any] = {}
    backend = _Backend([])
    events = await _run_stream(
        exclude_ui_tools=False,
        ui_driving_enabled=True,
        backend=backend,
        captured_tool_defs=captured,
    )
    assert captured.get("include_ui_tools") is True
    assert backend.turns == 1
    assert events[-1][0] == "done"


# ── (c) text-mode backend fallback gating ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exclude_ui_tools", "expected_text_flag"),
    [(True, False), (False, True)],
    ids=["exclude-ui-tools-disables-text-mode", "panel-keeps-text-mode"],
)
async def test_text_mode_backend_gates_include_ui_tools_text(
    exclude_ui_tools: bool,
    expected_text_flag: bool,
) -> None:
    build_prompt = AsyncMock(return_value="")
    backend = _TextBackend([])
    events = await _run_stream(
        exclude_ui_tools=exclude_ui_tools,
        ui_driving_enabled=True,
        backend=backend,
        loader_build_prompt=build_prompt,
    )
    assert build_prompt.await_count == 1
    assert build_prompt.await_args is not None
    kwargs = build_prompt.await_args.kwargs
    assert kwargs["include_ui_tools_text"] is expected_text_flag
    assert backend.turns == 1
    assert events[-1][0] == "done"


# ── (d) model-emitted UI tool call refused under the flag ──────────────────


@pytest.mark.asyncio
async def test_ui_tool_call_under_exclude_ui_tools_returns_unavailable_error() -> None:
    backend = _Backend([_tool_call("click", "ui-1", 0)])
    events = await _run_stream(
        exclude_ui_tools=True,
        ui_driving_enabled=True,
        backend=backend,
    )

    tool_events = [data for event, data in events if event == "tool_call"]
    assert len(tool_events) == 1
    result = tool_events[0]
    assert result["tool_name"] == "click"
    assert result["success"] is False
    assert result["error"] == UI_DRIVING_UNAVAILABLE

    event_names = [event for event, _data in events]
    assert "ui_command_batch" not in event_names
    assert "permission_request" not in event_names
    assert events[-1][0] == "done"


# ── (e) get_manifest refused under the flag; manifest module untouched ─────


@pytest.mark.asyncio
async def test_get_manifest_under_exclude_ui_tools_refused_without_serving() -> None:
    manifest_mock = MagicMock(return_value={"routes": {}, "elements": {}, "sidebar_groups": {}})
    backend = _Backend([_tool_call("get_manifest", "ui-1", 0)])
    events = await _run_stream(
        exclude_ui_tools=True,
        ui_driving_enabled=True,
        backend=backend,
        manifest_mock=manifest_mock,
    )

    tool_events = [data for event, data in events if event == "tool_call"]
    assert len(tool_events) == 1
    result = tool_events[0]
    assert result["tool_name"] == "get_manifest"
    assert result["success"] is False
    assert result["error"] == UI_DRIVING_UNAVAILABLE

    # The manifest must never be served under the flag.
    manifest_mock.assert_not_called()

    event_names = [event for event, _data in events]
    assert "ui_command_batch" not in event_names
    assert "permission_request" not in event_names
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_get_manifest_without_flag_is_served() -> None:
    manifest_mock = MagicMock(return_value={"routes": {}, "elements": {}, "sidebar_groups": {}})
    backend = _Backend([_tool_call("get_manifest", "ui-1", 0)])
    events = await _run_stream(
        exclude_ui_tools=False,
        ui_driving_enabled=True,
        backend=backend,
        manifest_mock=manifest_mock,
    )

    tool_events = [data for event, data in events if event == "tool_call"]
    assert len(tool_events) == 1
    result = tool_events[0]
    assert result["tool_name"] == "get_manifest"
    assert result["success"] is True
    manifest_mock.assert_called_once()
