"""Docker-marked test fixtures (FAR-590 D4, docker marker).

Everything in ``tests/docker/`` requires a live Docker engine. The autouse
fixture pings the engine and skips the test when unreachable.

The dind engine-kill test additionally opts in via
``MODULO_RUNNER_DIND_TESTS=1`` — it pulls ~560 MB of dind image into the
local store, which must never be a silent default cost.

These tests are selected explicitly (``pytest -m docker``); they are NOT in
the default CI lanes yet (the compose CI job is a GA item, plan D4).
"""

import asyncio
import contextlib
import uuid

import aiodocker
import pytest

pytestmark = [pytest.mark.docker]


async def _engine_reachable() -> bool:
    try:
        docker = aiodocker.Docker()
    except Exception:
        return False
    try:
        await docker.version()
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await docker.close()
    return True


@pytest.fixture(autouse=True)
def _require_engine() -> None:
    if not asyncio.run(_engine_reachable()):
        pytest.skip("no reachable Docker engine (docker-marked tests)")


@pytest.fixture(scope="module")
def runner_image() -> str:
    """A local stand-in for the first-party modulo-runner image.

    The GHCR-published image is the GA item; locally we tag a small base
    image with the modulo-runner prefix so the provider stamps the non-root
    user (a numeric uid needs no /etc/passwd entry).
    """
    if not asyncio.run(_engine_reachable()):
        pytest.skip("no reachable Docker engine (docker-marked tests)")
    tag = "modulo-runner:test-opencode"

    async def _prepare() -> None:
        async with aiodocker.Docker() as docker:
            try:
                await docker.images.inspect(tag)
            except aiodocker.exceptions.DockerError:
                await docker.images.pull("alpine:3.20")
                await docker.images.tag("alpine:3.20", repo="modulo-runner", tag="test-opencode")

    asyncio.run(_prepare())
    return tag


@pytest.fixture(scope="module")
def workspace_network() -> str:
    """A dedicated bridge network (the overlay's modulo-runner-workspace role)."""
    if not asyncio.run(_engine_reachable()):
        pytest.skip("no reachable Docker engine (docker-marked tests)")
    name = f"modulo-runner-test-{uuid.uuid4().hex[:10]}"

    async def _manage(action: str) -> None:
        async with aiodocker.Docker() as docker:
            if action == "create":
                await docker.networks.create(
                    {"Name": name, "Driver": "bridge", "Labels": {"modulo.test": "d4-docker-marked"}}
                )
            else:
                with contextlib.suppress(Exception):
                    network = await docker.networks.get(name)
                    await network.delete()

    asyncio.run(_manage("create"))
    yield name
    asyncio.run(_manage("delete"))
