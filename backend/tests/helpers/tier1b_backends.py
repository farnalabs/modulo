"""Tier 1b (FAR-934) shared helpers for the local model-server suites.

The light (ollama, llama.cpp, localai) and heavy (vllm, tgi) model suites
drive the same ``OpenAICompatibleBackend`` against a real container, so the
SSRF loopback opt-in, the GGUF seed URL and the backend factory/invocation
helpers live here once instead of being copy-pasted per module (FAR-934 review).

The ``ssrf_loopback_consent`` fixture is defined once here and imported by each
Tier 1b module (an imported fixture is discoverable in the importing module's
namespace; ``autouse`` then applies it). It is deliberately NOT placed in
``tests/integration/conftest.py`` — an autouse conftest fixture would grant the
loopback opt-in to every unrelated integration test.

Usage::

    from tests.helpers.tier1b_backends import (
        QWEN_05B_GGUF,
        invoke_once,
        make_backend,
        ssrf_loopback_consent,
    )
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from tests.helpers.testcontainers_harness import SSRF_LOOPBACK_OPTIN

if TYPE_CHECKING:
    from modulo.model_backends.module import OpenAICompatibleBackend

QWEN_05B_GGUF = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"


@pytest.fixture(scope="session", autouse=True)
def ssrf_loopback_consent() -> Iterator[None]:
    """Let the pinned-transport SSRF guard reach loopback containers.

    The same documented operator opt-in the Tier 1a fixtures use, applied
    lint-clean via a session-scoped MonkeyPatch (``monkeypatch`` itself is
    function-scoped and therefore illegal for a session fixture — pytest
    raises ScopeMismatch if requested here).
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("SSRF_ALLOW_PRIVATE_RANGES", SSRF_LOOPBACK_OPTIN)
    yield
    mp.undo()


def make_backend(provider: str, model_id: str, base_url: str) -> OpenAICompatibleBackend:
    """Build a real ``OpenAICompatibleBackend`` pointed at a live container."""
    from modulo.model_backends.module import OpenAICompatibleBackend

    return OpenAICompatibleBackend(
        api_key="not-needed",
        model_id=model_id,
        base_url=base_url,
        provider=provider,
    )


async def invoke_once(backend: OpenAICompatibleBackend) -> str:
    """Invoke the backend once with a real prompt; return its text output."""
    from langchain_core.messages import HumanMessage

    result = await backend.invoke([HumanMessage(content="Say the single word: modulo")])
    return str(result.content)
