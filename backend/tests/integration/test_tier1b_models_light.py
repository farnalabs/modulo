"""Tier 1b (FAR-934) — local OpenAI-compatible model servers, LIGHT set.

Covers: ollama, llama.cpp (server mode), localai.  The heavy pair (vllm,
tgi — multi-GB images, very slow CPU boot) lives in
``test_tier1b_models_heavy.py`` and runs only in the nightly job.

Each test boots a real local model server with Testcontainers, then drives
the real Modulo ``OpenAICompatibleBackend`` (pinned transport; SSRF loopback
consent is granted by the integration conftest) and asserts on REAL model
completions — never canned bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers.testcontainers_harness import (
    SSRF_LOOPBACK_OPTIN,
    ContainerHandle,
    ContainerSpec,
    Tier1bFixtureError,
    probe_http,
    start_tier1b_container,
)

if TYPE_CHECKING:
    from modulo.model_backends.module import OpenAICompatibleBackend

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def ssrf_loopback_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRF guard loopback opt-in (see the light connector module for rationale)."""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", SSRF_LOOPBACK_OPTIN)


QWEN_05B_GGUF = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"


def _backend(provider: str, model_id: str, base_url: str) -> OpenAICompatibleBackend:
    from modulo.model_backends.module import OpenAICompatibleBackend

    return OpenAICompatibleBackend(
        api_key="not-needed",
        model_id=model_id,
        base_url=base_url,
        provider=provider,
    )


async def _invoke_once(backend: OpenAICompatibleBackend) -> str:
    from langchain_core.messages import HumanMessage

    result = await backend.invoke([HumanMessage(content="Say the single word: modulo")])
    return str(result.content)


# ── ollama ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def ollama_service() -> ContainerHandle:
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="ollama/ollama:latest",
                container_port=11434,
                command=["serve"],
                probe=probe_http("/api/version"),
                ready_timeout_seconds=300,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b ollama fixture unavailable on this Docker host (recorded skip): {exc}")
    # Pull a tiny real model inside the container (bounded by the harness's
    # exec handling); then start serving via the OpenAI-compatible route.
    handle.exec(["ollama", "pull", "qwen2:0.5b"])
    yield handle
    handle.stop()


async def test_ollama_health_and_completion(ollama_service: ContainerHandle) -> None:
    backend = _backend("ollama", "qwen2:0.5b", f"{ollama_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"ollama health check failed against real container: {health}"
        text = await _invoke_once(backend)
        assert text.strip(), "ollama completion must be non-empty from the real container"
    finally:
        await backend.aclose()


def test_ollama_model_listed(ollama_service: ContainerHandle) -> None:
    listing = ollama_service.exec(["ollama", "list"])
    assert "qwen2:0.5b" in listing, f"expected pulled model in real ollama list: {listing!r}"


# ── llama.cpp server ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def llamacpp_service() -> ContainerHandle:
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="ghcr.io/ggerganov/llama.cpp:server",
                container_port=8080,
                command=[
                    "-m",
                    QWEN_05B_GGUF,
                    "--host",
                    "0.0.0.0",  # noqa: S104 - container server must bind all interfaces inside the container
                    "--port",
                    "8080",
                    "--threads",
                    "2",
                ],
                probe=probe_http("/health"),
                ready_timeout_seconds=900,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b llamacpp fixture unavailable on this Docker host (recorded skip): {exc}")
    yield handle
    handle.stop()


async def test_llamacpp_health_and_completion(llamacpp_service: ContainerHandle) -> None:
    backend = _backend("llamacpp", "qwen2.5-0.5b-instruct", f"{llamacpp_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"llamacpp health check failed against real container: {health}"
        text = await _invoke_once(backend)
        assert text.strip(), "llamacpp completion must be non-empty from the real container"
    finally:
        await backend.aclose()


# ── localai ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def localai_service() -> ContainerHandle:
    """LocalAI CPU image; the ``qwen2`` catalog boots a bundled Qwen2.5 model."""
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="localai/localai:latest-cpu",
                container_port=8080,
                command=["qwen2"],
                probe=probe_http("/readyz"),
                ready_timeout_seconds=900,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b localai fixture unavailable on this Docker host (recorded skip): {exc}")
    yield handle
    handle.stop()


async def test_localai_health_and_completion(localai_service: ContainerHandle) -> None:
    backend = _backend("localai", "qwen2", f"{localai_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"localai health check failed against real container: {health}"
        text = await _invoke_once(backend)
        assert text.strip(), "localai completion must be non-empty from the real container"
    finally:
        await backend.aclose()
