"""Tier 1b (FAR-934) — local OpenAI-compatible model servers, NIGHTLY HEAVY set.

Covers: vllm, text-generation-inference (tgi).  Both images are multi-GB
and boot slowly on CPU-only runners, so they are exercised only by the
nightly ``tier1b-nightly`` CI job, never on PRs.
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
QWEN_05B_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct"


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


# ── vllm (CPU nightly) ──────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def vllm_service() -> ContainerHandle:
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="vllm/vllm-openai:latest",
                container_port=8000,
                env={"VLLM_WORKER_MULTIPROC_METHOD": "spawn"},
                command=[
                    "--model",
                    QWEN_05B_REPO_ID,
                    "--device",
                    "cpu",
                    "--dtype",
                    "float16",
                ],
                probe=probe_http("/health"),
                ready_timeout_seconds=1800,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b vllm nightly fixture unavailable on this Docker host (recorded skip): {exc}")
    yield handle
    handle.stop()


async def test_vllm_health_and_completion(vllm_service: ContainerHandle) -> None:
    backend = _backend("vllm", QWEN_05B_REPO_ID, f"{vllm_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"vllm health check failed against real container: {health}"
        text = await _invoke_once(backend)
        assert text.strip(), "vllm completion must be non-empty from the real container"
    finally:
        await backend.aclose()


# ── text-generation-inference ───────────────────────────────────────────────


@pytest.fixture(scope="session")
def tgi_service() -> ContainerHandle:
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="ghcr.io/huggingface/text-generation-inference:latest",
                container_port=80,
                command=[
                    "--model-id",
                    QWEN_05B_REPO_ID,
                    "--disable-custom-kernels",
                    "--dtype",
                    "float16",
                ],
                probe=probe_http("/health"),
                ready_timeout_seconds=1800,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b tgi nightly fixture unavailable on this Docker host (recorded skip): {exc}")
    yield handle
    handle.stop()


async def test_tgi_health_and_completion(tgi_service: ContainerHandle) -> None:
    backend = _backend("tgi", QWEN_05B_REPO_ID, f"{tgi_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"tgi health check failed against real container: {health}"
        text = await _invoke_once(backend)
        assert text.strip(), "tgi completion must be non-empty from the real container"
    finally:
        await backend.aclose()
