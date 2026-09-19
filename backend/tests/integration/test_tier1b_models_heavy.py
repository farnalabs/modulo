"""Tier 1b (FAR-934) — local OpenAI-compatible model servers, NIGHTLY HEAVY set.

Covers: vllm, text-generation-inference (tgi).  Both images are multi-GB
and boot slowly on CPU-only runners, so they are exercised only by the
nightly ``tier1b-nightly`` CI job, never on PRs.
"""

from __future__ import annotations

import pytest

from tests.helpers.testcontainers_harness import (
    ContainerHandle,
    ContainerSpec,
    Tier1bFixtureError,
    probe_http,
    start_tier1b_container,
)
from tests.helpers.tier1b_backends import (
    invoke_once,
    make_backend,
    ssrf_loopback_consent,  # noqa: F401 — imported fixture, autouse via module namespace
)

pytestmark = pytest.mark.integration

QWEN_05B_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct"


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
    backend = make_backend("vllm", QWEN_05B_REPO_ID, f"{vllm_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"vllm health check failed against real container: {health}"
        text = await invoke_once(backend)
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
    backend = make_backend("tgi", QWEN_05B_REPO_ID, f"{tgi_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"tgi health check failed against real container: {health}"
        text = await invoke_once(backend)
        assert text.strip(), "tgi completion must be non-empty from the real container"
    finally:
        await backend.aclose()
