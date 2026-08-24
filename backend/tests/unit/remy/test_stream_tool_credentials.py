"""Stream-level coverage for optional Remy MCP credentials."""

from __future__ import annotations

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calls", "expected_results", "expected_turns"),
    [
        ([_tool_call("get_manifest", "ui-1", 0)], [("get_manifest", True)], 2),
        ([_tool_call("list_pipelines", "mcp-1", 0)], [("list_pipelines", False)], 2),
        (
            [
                _tool_call("get_manifest", "ui-1", 0),
                _tool_call("list_pipelines", "mcp-1", 1),
            ],
            [("list_pipelines", False), ("get_manifest", True)],
            2,
        ),
        ([], [], 1),
    ],
    ids=["ui-only", "mcp-only", "mixed", "no-tool-calls"],
)
async def test_stream_without_api_keys_preserves_tool_control_flow(
    calls: list[dict[str, Any]],
    expected_results: list[tuple[str, bool]],
    expected_turns: int,
) -> None:
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
    backend = _Backend(calls)
    loader = MagicMock()
    loader.build_system_prompt = AsyncMock(return_value="")
    call_mcp_tool = AsyncMock()

    principal = TenantPrincipal(
        username="user@example.com",
        organisation_id=organisation_id,
        account_id=account_id,
        org_role="admin",
    )
    request = StreamRequest(
        content="Help me",
        provider="stub",
        model="stub-model",
        api_key=None,
        mcp_api_key=None,
    )
    settings = SimpleNamespace(modulo_public_url="http://test", fernet_key="unused")

    with (
        patch("modulo.api.routes.remy.AsyncSession", return_value=stream_session),
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.remy._resolve_api_key", new_callable=AsyncMock, return_value="provider-key"),
        patch("modulo.api.routes.remy._build_backend", return_value=backend),
        patch("modulo.api.routes.remy.SkillLoader", return_value=loader),
        patch("modulo.api.routes.remy._reconstruct_messages", new_callable=AsyncMock, return_value=[]),
        patch("modulo.api.routes.remy.build_tool_registry", new_callable=AsyncMock),
        patch("modulo.api.routes.remy._is_ui_driving_enabled", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.remy._get_all_tool_definitions", return_value=[]),
        patch("modulo.api.routes.remy._call_mcp_tool", call_mcp_tool),
        patch(
            "modulo.core.manifest.get_manifest",
            return_value={"routes": {}, "elements": {}, "sidebar_groups": {}},
        ),
    ):
        response = await stream_chat(
            session_id=session_id,
            req=request,
            request=_Request(),  # type: ignore[arg-type]
            session=request_session,  # type: ignore[arg-type]
            principal=principal,
            settings=settings,  # type: ignore[arg-type]
        )
        events = await _read_sse(response)

    result_events = [data for event, data in events if event == "tool_call"]
    assert [(result["tool_name"], result["success"]) for result in result_events] == expected_results
    assert events[-1][0] == "done"
    assert all(event != "error" for event, _data in events)
    assert backend.turns == expected_turns
    call_mcp_tool.assert_not_awaited()

    missing_key_results = [result for result in result_events if result["tool_name"] == "list_pipelines"]
    if missing_key_results:
        assert missing_key_results[0]["error"] == "Tool execution requires an MCP API key"
