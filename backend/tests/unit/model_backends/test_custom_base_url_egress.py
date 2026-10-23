"""FAR-2046 — a custom model-backend endpoint is actually used end to end.

These tests drive the REAL client-construction path (hub ``_build_backend`` →
``OpenAICompatibleBackend`` → ``pinned_async_client_sync``) and capture the
outbound request at the transport boundary, so they fail on the pre-fix
behaviour (``openai`` ignored ``creds.base_url`` entirely) rather than passing
against a hand-shaped fake.

The last test exercises the model-backed typed judge path
(``build_org_model_backend_hub`` → ``build_suite_run_llm_judge_callable``) which
is the air-gapped use case ADR 066 depends on.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import HumanMessage

import modulo.model_backends.module as openai_module
from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.eval_engine.suite_run_judge import (
    build_org_model_backend_hub,
    build_suite_run_llm_judge_callable,
)
from modulo.core.model_backend_hub import _build_backend

CUSTOM_BASE_URL = "https://gateway.internal.example/v1"
_CHAT_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": '{"passed": true, "score": 1.0, "detail": "ok"}'},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _CapturedTransport:
    """Record the pinned-client URL and every request the SDK sends."""

    def __init__(self) -> None:
        self.pinned_urls: list[str] = []
        self.trust_env: list[bool | None] = []
        self.request_urls: list[str] = []

    def factory(self, url: str, **kwargs: Any) -> httpx.AsyncClient:
        self.pinned_urls.append(url)
        self.trust_env.append(kwargs.get("trust_env"))

        def handler(request: httpx.Request) -> httpx.Response:
            self.request_urls.append(str(request.url))
            return httpx.Response(200, json=_CHAT_COMPLETION)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _CapturedTransport:
    captured = _CapturedTransport()
    monkeypatch.setattr(openai_module, "pinned_async_client_sync", captured.factory)
    return captured


async def test_hub_built_openai_backend_sends_request_to_custom_base_url(
    transport: _CapturedTransport,
) -> None:
    """The request goes to the operator's endpoint, through the pinned transport."""
    backend = _build_backend(
        "openai",
        "test-model",
        {"api_key": "test-key", "base_url": CUSTOM_BASE_URL},
        {},
    )
    try:
        reply = await backend.invoke([HumanMessage(content="ping")])
    finally:
        await backend.aclose()

    assert transport.pinned_urls == [CUSTOM_BASE_URL]
    assert transport.trust_env == [False]
    assert transport.request_urls
    assert all(url.startswith(f"{CUSTOM_BASE_URL}/chat/completions") for url in transport.request_urls)
    assert "ok" in str(reply.content)


async def test_hub_built_local_provider_overrides_default_with_custom_base_url(
    transport: _CapturedTransport,
) -> None:
    """A named local provider falls back to its default only when none is configured."""
    backend = _build_backend(
        "ollama",
        "test-model",
        {"api_key": "test-key", "base_url": CUSTOM_BASE_URL},
        {},
    )
    try:
        await backend.invoke([HumanMessage(content="ping")])
    finally:
        await backend.aclose()

    assert transport.pinned_urls == [CUSTOM_BASE_URL]


class _FakeSecretsBackend:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    async def get_secret(self, key: str) -> str:
        return json.dumps(self._payload)


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, _stmt: object) -> _RowsResult:
        return _RowsResult(self._rows)


class _FakeRow:
    def __init__(self, backend_id: uuid.UUID) -> None:
        self.id = backend_id
        self.provider = "openai"
        self.model_id = "test-model"
        self.default_params: dict[str, Any] = {}
        self.fallback_backend_ids = None
        self.status = "active"
        self.credentials_ciphertext = b""


async def test_model_backed_typed_judge_uses_the_custom_base_url(
    transport: _CapturedTransport,
) -> None:
    """An llm_judge eval resolves its backend and calls the custom endpoint."""
    backend_id = uuid.uuid4()
    secrets_backend = _FakeSecretsBackend({"api_key": "test-key", "base_url": CUSTOM_BASE_URL})
    settings = type("Settings", (), {"fernet_key": "unused-in-this-test"})()

    with (
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=secrets_backend),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        hub = await build_org_model_backend_hub(_FakeSession([_FakeRow(backend_id)]), uuid.uuid4())

    assert hub is not None
    try:
        judge = build_suite_run_llm_judge_callable(hub)
        eval_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            node_id="judge-node",
            name="judge-quality",
            eval_type=EvalType.LLM_JUDGE,
            config={"field": "content", "model_backend_id": str(backend_id)},
        )
        result = judge({"content": "hello"}, eval_def)
    finally:
        await hub.__aexit__(None, None, None)

    assert result["passed"] is True
    assert transport.pinned_urls == [CUSTOM_BASE_URL]
    assert transport.trust_env == [False]
    assert transport.request_urls
    assert all(url.startswith(f"{CUSTOM_BASE_URL}/chat/completions") for url in transport.request_urls)
