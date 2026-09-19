"""Tier 1b (FAR-934) — local OpenAI-compatible model servers, LIGHT set.

Covers: ollama, llama.cpp (server mode), localai.  The heavy pair (vllm,
tgi — multi-GB images, very slow CPU boot) lives in
``test_tier1b_models_heavy.py`` and runs only in the nightly job.

Each test boots a real local model server with Testcontainers, then drives
the real Modulo ``OpenAICompatibleBackend`` (pinned transport; the shared
``ssrf_loopback_consent`` fixture grants loopback consent) and asserts on REAL
model completions — never canned bodies.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.request import urlretrieve

import pytest

from tests.helpers.testcontainers_harness import (
    ContainerHandle,
    ContainerSpec,
    Tier1bFixtureError,
    probe_http,
    start_tier1b_container,
)
from tests.helpers.tier1b_backends import (
    QWEN_05B_GGUF,
    invoke_once,
    make_backend,
    ssrf_loopback_consent,  # noqa: F401 — imported fixture, autouse via module namespace
)

pytestmark = pytest.mark.integration


# ── ollama ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def ollama_service() -> Iterator[ContainerHandle]:
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
    try:
        handle.exec(["ollama", "pull", "qwen2:0.5b"])
        yield handle
    finally:
        handle.stop()


async def test_ollama_health_and_completion(ollama_service: ContainerHandle) -> None:
    backend = make_backend("ollama", "qwen2:0.5b", f"{ollama_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"ollama health check failed against real container: {health}"
        text = await invoke_once(backend)
        assert text.strip(), "ollama completion must be non-empty from the real container"
    finally:
        await backend.aclose()


def test_ollama_model_listed(ollama_service: ContainerHandle) -> None:
    listing = ollama_service.exec(["ollama", "list"])
    assert "qwen2:0.5b" in listing, f"expected pulled model in real ollama list: {listing!r}"


# ── llama.cpp server ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def llamacpp_service() -> Iterator[ContainerHandle]:
    """llama.cpp server over a host-downloaded GGUF bind mount."""
    model_dir = Path(tempfile.mkdtemp(prefix="llamacpp-tier1b-"))
    try:
        urlretrieve(QWEN_05B_GGUF, str(model_dir / "qwen2.5-0.5b.gguf"))  # noqa: S310 - pinned https GGUF URL
    except Exception as exc:
        shutil.rmtree(model_dir, ignore_errors=True)
        pytest.skip(f"Tier 1b llamacpp model seed unavailable (recorded skip): {exc}")
    try:
        try:
            handle = start_tier1b_container(
                ContainerSpec(
                    # 2025+: llama.cpp containers moved to the org namespace
                    # ghcr.io/ggml-org/llama.cpp (ghcr.io/ggerganov no longer ships a :server tag).
                    image="ghcr.io/ggml-org/llama.cpp:server",
                    container_port=8080,
                    # Recorded live contract (verified against ggml-org/llama.cpp:server):
                    # this build cannot fetch a model over HTTP (-m <URL> fails with
                    # gguf_init_from_file "No such file or directory"), so the GGUF is
                    # downloaded on the host and bind-mounted read-write into /models.
                    command=[
                        "-m",
                        "/models/qwen2.5-0.5b.gguf",
                        "--host",
                        "0.0.0.0",  # noqa: S104 - container server must bind all interfaces inside the container
                        "--port",
                        "8080",
                        "--threads",
                        "2",
                    ],
                    probe=probe_http("/health"),
                    ready_timeout_seconds=900,
                    volumes=[(str(model_dir), "/models")],
                )
            )
        except Tier1bFixtureError as exc:
            shutil.rmtree(model_dir, ignore_errors=True)
            pytest.skip(f"Tier 1b llamacpp fixture unavailable on this Docker host (recorded skip): {exc}")
        try:
            yield handle
        finally:
            handle.stop()
    finally:
        shutil.rmtree(model_dir, ignore_errors=True)


async def test_llamacpp_health_and_completion(llamacpp_service: ContainerHandle) -> None:
    backend = make_backend("llamacpp", "qwen2.5-0.5b-instruct", f"{llamacpp_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"llamacpp health check failed against real container: {health}"
        text = await invoke_once(backend)
        assert text.strip(), "llamacpp completion must be non-empty from the real container"
    finally:
        await backend.aclose()


# ── localai ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def localai_service() -> ContainerHandle:
    """LocalAI CPU image serving a Qwen2.5 GGUF seeded through a bind mount."""
    model_dir = Path(tempfile.mkdtemp(prefix="localai-tier1b-"))
    try:
        urlretrieve(QWEN_05B_GGUF, str(model_dir / "qwen2.5-0.5b.gguf"))  # noqa: S310 - pinned https GGUF URL
        (model_dir / "qwen2.yml").write_text("name: qwen2\nbackend: llama\nparameters:\n  model: qwen2.5-0.5b.gguf\n")
    except Exception as exc:
        shutil.rmtree(model_dir, ignore_errors=True)
        pytest.skip(f"Tier 1b localai model seed unavailable (recorded skip): {exc}")
    try:
        try:
            handle = start_tier1b_container(
                ContainerSpec(
                    image="localai/localai:latest-cpu",
                    container_port=8080,
                    command=["run", "qwen2"],
                    env={
                        # The image entrypoint rebuilds local-ai from source unless
                        # REBUILD=false — a multi-minute make cycle we must never hit.
                        "REBUILD": "false",
                        "MODELS_PATH": "/build/models",
                        "THREADS": "2",
                    },
                    volumes=[(str(model_dir), "/build/models")],
                    probe=probe_http("/readyz"),
                    ready_timeout_seconds=900,
                )
            )
        except Tier1bFixtureError as exc:
            shutil.rmtree(model_dir, ignore_errors=True)
            pytest.skip(f"Tier 1b localai fixture unavailable on this Docker host (recorded skip): {exc}")
        try:
            yield handle
        finally:
            handle.stop()
    finally:
        shutil.rmtree(model_dir, ignore_errors=True)


async def test_localai_health_and_completion(localai_service: ContainerHandle) -> None:
    backend = make_backend("localai", "qwen2", f"{localai_service.base_url}/v1")
    try:
        health = await backend.health_check()
        assert health.ok, f"localai health check failed against real container: {health}"
        text = await invoke_once(backend)
        assert text.strip(), "localai completion must be non-empty from the real container"
    finally:
        await backend.aclose()
