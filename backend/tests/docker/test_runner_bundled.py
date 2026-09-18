"""Docker-marked Bundled Runner provider tests (FAR-590 D4, plan acceptance).

Run explicitly: ``uv run pytest tests/docker -m docker`` from ``backend/``
(with Docker Desktop / any engine up; ``MODULO_RUNNER_DIND_TESTS=1`` opts in
to the dind engine-kill strip which pulls ~560 MB).

Covers the D4 acceptance surface against a real engine:
  - workspace hardening at provision (non-root user, read-only rootfs +
    tmpfs, dropped caps + no-new-privileges, 1 CPU / 1 GiB, labels incl. the
    deployment-identity + creation marker, dedicated workspace network,
    per-profile ``none`` opt-in);
  - the streaming exec primitive end to end (chunks, exit code, kill handle,
    container-kill mid-exec classifying retryable — never a fabricated
    ``ExecResult(0)``);
  - destroy;
  - the reconciler's orphan destroy + active-run spare + the label-filtered
    listing through the filtered socket proxy;
  - the filtered socket-proxy shape (deny-403 outside the allowlist and the
    runtime completeness assertion: the exercise must not trigger ANY proxy
    rejection);
  - the dind engine-kill strip (engine death mid-exec is retryable, never
    ``ExecResult(exit_code=0)``).

The GHCR-published modulo-runner image is the GA item — locally the suite
tags ``alpine:3.20`` as ``modulo-runner:test-opencode`` (a numeric uid needs
no /etc/passwd entry, so the non-root stamping is still exercised).
"""

import asyncio
import contextlib
import os
import socket
import time
import uuid
from types import SimpleNamespace
from typing import Self

import aiodocker
import aiohttp
import pytest

from modulo.core.bundled_runner.runner_reconciler import reconcile_runner_workspaces
from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider

pytestmark = [pytest.mark.docker]

_PROXY_IMAGE = "lscr.io/linuxserver/socket-proxy:latest"
_PROXY_LABEL = "modulo.test=d4-proxy"
_PROXY_STATE: dict[str, str | None] = {"network": None, "name": None}


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


async def _ensure_image(docker: aiodocker.Docker, ref: str) -> None:
    try:
        await docker.images.inspect(ref)
    except aiodocker.exceptions.DockerError:
        await docker.images.pull(ref)


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
                await _ensure_image(docker, "alpine:3.20")
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


def _spec(network: str | None, **overrides: object) -> WorkspaceSpec:
    base: dict[str, object] = {
        "environment_profile_id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "image_ref": "modulo-runner:test-opencode",
        "capabilities": [],
        "timeout_seconds": 300,
        "resource_limits": {"memory_mb": 1024},
        "egress_policy": "outbound",
        "persistence_policy": "ephemeral",
        "labels": {},
        "workspace_metadata": {
            "modulo.run.id": "run-d4m-1",
            "modulo.org.id": "org-d4m-1",
            "modulo.node.id": "node-d4m-1",
        },
        "workspace_network": network,
    }
    base.update(overrides)
    return WorkspaceSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Hardening at provision
# ---------------------------------------------------------------------------


async def test_workspace_hardening_defaults_at_provision(runner_image: str, workspace_network: str) -> None:
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(workspace_network))
    try:
        client = await provider._get_client()
        container_id = provider._workspaces[ref]
        info = await (await client.containers.get(container_id)).show()
        host = info["HostConfig"]
        cfg = info["Config"]

        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert host["NanoCpus"] == 1_000_000_000  # 1.0 CPU
        assert host["Memory"] == 1024 * 1024 * 1024  # 1 GiB
        assert host["AutoRemove"] is True
        assert "/tmp" in host["Tmpfs"]
        assert cfg["User"] == "1001:1001"
        assert cfg["Cmd"] == ["sleep", "infinity"]
        assert cfg["Labels"]["modulo.run.id"] == "run-d4m-1"
        assert cfg["Labels"]["modulo.org.id"] == "org-d4m-1"
        assert cfg["Labels"]["modulo.node.id"] == "node-d4m-1"
        assert cfg["Labels"]["modulo.machine.id"]
        assert int(cfg["Labels"]["modulo.created_at"]) > 0
        networks = info["NetworkSettings"]["Networks"]
        assert workspace_network in networks
        # The workspace container never joins the compose/backend network.
        assert len(networks) == 1
    finally:
        await provider.close()


async def test_workspace_none_egress_opt_in(runner_image: str) -> None:
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(None, egress_policy="none"))
    try:
        client = await provider._get_client()
        container_id = provider._workspaces[ref]
        info = await (await client.containers.get(container_id)).show()
        assert info["HostConfig"]["NetworkMode"] == "none"
        # The workspace still executes (loopback-only).
        result = await provider.exec_command(ref, ["sh", "-c", "echo isolated"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "isolated"
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# Streaming exec primitive end to end
# ---------------------------------------------------------------------------


async def test_streaming_exec_chunks_and_exit_code(runner_image: str, workspace_network: str) -> None:
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(workspace_network))
    try:
        process = await provider.exec_command_stream(
            ref,
            ["sh", "-c", "echo stream-hello; echo stream-err >&2"],
            environment={"MODULO_TEST": "1"},
        )
        chunks = []
        async for chunk in process.chunks:
            chunks.append(chunk)
        await asyncio.wait_for(process.done.wait(), timeout=15)
        stdout = "".join(c.data for c in chunks if c.stream == "stdout")
        stderr = "".join(c.data for c in chunks if c.stream == "stderr")
        assert "stream-hello" in stdout
        assert "stream-err" in stderr
        assert process.exit_code == 0
        assert process.error is None
    finally:
        await provider.close()


async def test_streaming_exec_live_midrun_output(runner_image: str, workspace_network: str) -> None:
    """Live output arrives DURING the exec (not after completion) — the D4
    live-log drain contract on Docker."""
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(workspace_network))
    try:
        process = await provider.exec_command_stream(ref, ["sh", "-c", "echo first; sleep 3; echo second"])
        first_seen_at: float | None = None
        async for _chunk in process.chunks:
            if first_seen_at is None:
                first_seen_at = time.monotonic()
        await asyncio.wait_for(process.done.wait(), timeout=15)
        done_seen_at = time.monotonic()
        assert first_seen_at is not None
        assert done_seen_at - first_seen_at >= 2.0  # 'second' arrives ~3s after 'first'
        assert process.exit_code == 0
    finally:
        await provider.close()


async def test_container_kill_mid_exec_is_retryable_never_success(runner_image: str, workspace_network: str) -> None:
    """Killing the container mid-exec must surface as a stream error or a
    non-inspectable end — NEVER a fabricated ``exit_code == 0``."""
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(workspace_network))
    try:
        process = await provider.exec_command_stream(ref, ["sh", "-c", "echo started; sleep 30"])
        count = 0

        async def _consume() -> None:
            nonlocal count
            async for _chunk in process.chunks:
                count += 1

        consume_task = asyncio.create_task(_consume())
        await asyncio.sleep(2.0)  # let the exec start and emit 'started'
        client = await provider._get_client()
        container_id = provider._workspaces.get(ref)
        assert container_id is not None
        container = await client.containers.get(container_id)
        await container.kill()
        await asyncio.wait_for(consume_task, timeout=15)
        await asyncio.wait_for(process.done.wait(), timeout=15)
        assert count >= 1  # 'started' was streamed live
        # The kill NEVER fabricates success: a real non-zero exit (137 on
        # SIGKILL), a stream error, or a non-inspectable end (None) — the
        # engine-kill (dind) strip below asserts the pure no-exit-code case.
        assert process.exit_code != 0
        assert process.error is not None or process.exit_code is None or process.exit_code in (137, 143)
    finally:
        await provider.close()


async def test_exec_stream_kill_handle_stops_consumption(runner_image: str, workspace_network: str) -> None:
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(workspace_network))
    try:
        process = await provider.exec_command_stream(ref, ["sh", "-c", "echo alive; sleep 30"])
        chunks = []

        async def _consume() -> None:
            async for chunk in process.chunks:
                chunks.append(chunk)

        consume_task = asyncio.create_task(_consume())
        await asyncio.sleep(2.0)
        await process.kill()
        await asyncio.wait_for(consume_task, timeout=15)
        assert any("alive" in c.data for c in chunks)
    finally:
        await provider.close()


async def test_destroy_workspace_removes_container(runner_image: str, workspace_network: str) -> None:
    provider = DockerRuntimeProvider(default_image=runner_image)
    ref = await provider.create_workspace(_spec(workspace_network))
    container_id = provider._workspaces[ref]
    await provider.destroy_workspace(ref)
    assert ref not in provider._workspaces
    client = await provider._get_client()
    with pytest.raises(aiodocker.exceptions.DockerError):
        await (await client.containers.get(container_id)).show()
    await provider.close()


# ---------------------------------------------------------------------------
# Reconciler against the real engine
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, active_rows: list[list[str]]) -> None:
        self._active_ids = {row[0] for row in active_rows}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, *_a: object, **kwargs: object):
        params = kwargs.get("rid") or (_a[1].get("rid") if len(_a) > 1 and isinstance(_a[1], dict) else None)
        if params is not None:
            # Per-run destroy-path re-check: only the truly active run matches.
            rows: list[list[str]] = [[str(params)]] if str(params) in self._active_ids else []
        else:
            rows = [[rid] for rid in sorted(self._active_ids)]
        return SimpleNamespace(all=lambda: rows, fetchone=lambda: rows[0] if rows else None)


class _FakeEngine:
    def __init__(self, active_rows: list[list[str]]) -> None:
        self._active_rows = active_rows

    def connect(self) -> _FakeConn:
        return _FakeConn(self._active_rows)


class _FakeSettings:
    def __init__(self, destroy: bool) -> None:
        self.runner_reconciler_destroy_enabled = destroy


async def _labelled_container(name: str, run_id: str, age_s: int) -> str:
    """Create + start a reconciler-shaped labelled container (the sweep lists
    RUNNING containers only)."""
    async with aiodocker.Docker() as docker:
        created = await docker.containers.create(
            config={
                "Image": "alpine:3.20",
                "Cmd": ["sleep", "600"],
                "Labels": {
                    "modulo.run.id": run_id,
                    "modulo.machine.id": os.environ.get("MODULO_RUNNER_MACHINE_ID") or socket.gethostname(),
                    "modulo.created_at": str(int(time.time()) - age_s),
                    "modulo.test": "d4-reconciler",
                },
                "HostConfig": {"AutoRemove": False},
            },
            name=name,
        )
        container = await docker.containers.get(created.id)
        await container.start()
    return created.id


async def _delete_container(container_id: str) -> None:
    async with aiodocker.Docker() as docker:
        try:
            container = await docker.containers.get(container_id)
            await container.delete(force=True)
        except aiodocker.exceptions.DockerError:
            pass


async def test_reconciler_destroys_orphan_and_spares_active(monkeypatch: pytest.MonkeyPatch, runner_image: str) -> None:
    orphan_run = f"orphan-{uuid.uuid4().hex[:8]}"
    active_run = f"active-{uuid.uuid4().hex[:8]}"
    orphan_id = await _labelled_container(f"d4m-orphan-{uuid.uuid4().hex[:8]}", orphan_run, age_s=600)
    active_id = await _labelled_container(f"d4m-active-{uuid.uuid4().hex[:8]}", active_run, age_s=600)
    try:
        monkeypatch.setattr("modulo.settings.get_settings", lambda: _FakeSettings(destroy=True))

        engine = _FakeEngine(active_rows=[[active_run]])
        result = await reconcile_runner_workspaces(engine)

        assert result["scanned"] >= 2
        assert result["orphans_destroyed"] >= 1
        async with aiodocker.Docker() as docker:
            active_listings = await docker.containers.list(filters={"label": [f"modulo.run.id={active_run}"]})
            assert any(c.id == active_id for c in active_listings), "active-run container must be spared"
            orphan_listings = await docker.containers.list(filters={"label": [f"modulo.run.id={orphan_run}"]})
            assert not any(c.id == orphan_id for c in orphan_listings), "orphan must be destroyed"
    finally:
        await _delete_container(orphan_id)
        await _delete_container(active_id)


# ---------------------------------------------------------------------------
# Filtered socket proxy (phase-0 shape, spike-verified)
# ---------------------------------------------------------------------------


async def _start_socket_proxy() -> str | None:
    """Start a linuxserver/socket-proxy container and return its
    ``tcp://127.0.0.1:<host-port>`` endpoint.

    The OVERLAY wires the backend to the proxy over the compose network with
    NO host ports (CI-guarded); the test harness publishes a random
    localhost-only port because the Windows host cannot reach container
    bridge IPs directly. The allowlist path exercised is identical.
    Returns None when the image cannot be pulled/started (the caller skips).
    """
    async with aiodocker.Docker() as docker:
        try:
            await _ensure_image(docker, _PROXY_IMAGE)
        except Exception:
            return None

        network_name = f"modulo-proxy-test-{uuid.uuid4().hex[:10]}"
        try:
            await docker.networks.create({"Name": network_name, "Driver": "bridge"})
            _PROXY_STATE["network"] = network_name
            name = f"modulo-proxy-{uuid.uuid4().hex[:8]}"
            _PROXY_STATE["name"] = name
            created = await docker.containers.create(
                config={
                    "Image": _PROXY_IMAGE,
                    "Env": [
                        "CONTAINERS=1",
                        "EXEC=1",
                        "IMAGES=1",
                        "PING=1",
                        "VERSION=1",
                        "INFO=1",
                        "POST=1",
                        "ALLOW_ARCHIVE=0",
                        "ALLOW_EXPORT=0",
                        "ALLOW_LOGS=0",
                        "ALLOW_TOP=0",
                        "ALLOW_CHANGE=0",
                        "LOG_LEVEL=info",
                    ],
                    "HostConfig": {
                        "Binds": ["/var/run/docker.sock:/var/run/docker.sock:ro"],
                        "PortBindings": {"2375/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
                    },
                    "Labels": {"modulo.test": "d4-proxy"},
                },
                name=name,
            )
            proxy = await docker.containers.get(created.id)
            await proxy.start()
            network = await docker.networks.get(network_name)
            await network.connect({"Container": proxy.id})
            info = await proxy.show()
            assigned = (info["NetworkSettings"]["Ports"].get("2375/tcp") or [{}])[0]
            host_port = assigned.get("HostPort")
            if not host_port:
                await _stop_socket_proxy()
                return None
            for _ in range(40):
                with (
                    contextlib.suppress(Exception),
                    aiohttp.ClientSession() as session,
                    session.get(f"http://127.0.0.1:{host_port}/_ping", timeout=aiohttp.ClientTimeout(total=2)) as resp,
                ):
                    if resp.status == 200:
                        return f"tcp://127.0.0.1:{host_port}"
                await asyncio.sleep(0.5)
            await _stop_socket_proxy()
            return None
        except Exception:
            await _stop_socket_proxy()
            return None


async def _stop_socket_proxy() -> None:
    async with aiodocker.Docker() as docker:
        with contextlib.suppress(Exception):
            containers = await docker.containers.list(filters={"label": [_PROXY_LABEL]})
            for c in containers:
                with contextlib.suppress(Exception):
                    await c.delete(force=True)
        network_name = _PROXY_STATE.get("network")
        if network_name:
            with contextlib.suppress(Exception):
                await (await docker.networks.get(network_name)).delete()
        _PROXY_STATE["network"] = None
        _PROXY_STATE["name"] = None


async def _proxy_rejection_lines() -> list[str]:
    """Proxy per-request logs (direct engine access — the proxy's own logs
    endpoint is NOT in its allowlist by design)."""
    async with aiodocker.Docker() as docker:
        containers = await docker.containers.list(filters={"label": [_PROXY_LABEL]})
        if not containers:
            return []
        logs = await containers[-1].log(stdout=True, stderr=True)
    return [str(line) for line in logs if "PR--" in str(line) or " 403 " in str(line)]


async def test_proxy_allows_derived_surface_and_denies_outside_allowlist() -> None:
    """The overlay's endpoint->config mapping: allowed endpoints work through
    the proxy; a non-allowlisted endpoint (networks list) is denied 403."""
    endpoint = await _start_socket_proxy()
    if endpoint is None:
        pytest.skip("linuxserver/socket-proxy unavailable (pull or start failed)")
    host = endpoint.removeprefix("tcp://")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{host}/_ping") as resp:
                assert resp.status == 200
            async with session.get(f"http://{host}/containers/json") as resp:
                assert resp.status == 200
            async with session.get(f"http://{host}/networks") as resp:
                assert resp.status == 403
            async with session.get(f"http://{host}/volumes") as resp:
                assert resp.status == 403
    finally:
        await _stop_socket_proxy()


async def test_reconciler_label_filter_works_through_socket_proxy() -> None:
    """The reconciler's label-filtered listing works through the filtered
    socket proxy (GET /containers/json?filters=... rides the allowlist), and
    the runtime completeness assertion holds: the exercise triggers NO proxy
    rejection."""
    endpoint = await _start_socket_proxy()
    if endpoint is None:
        pytest.skip("linuxserver/socket-proxy unavailable (pull or start failed)")

    run_id = f"proxy-label-{uuid.uuid4().hex[:8]}"
    container_id = await _labelled_container(f"d4m-proxy-{uuid.uuid4().hex[:8]}", run_id, age_s=0)
    try:
        from modulo.core.bundled_runner import runner_reconciler

        source = runner_reconciler._DockerWorkspaceSource(endpoint)
        try:
            entries = await source.list_labelled_workspaces()
            assert any(entry.run_id == run_id for entry in entries)
        finally:
            await source.close()

        rejections = await _proxy_rejection_lines()
        assert not rejections, (
            "the proxy rejected at least one request during the reconciler exercise "
            f"(allowlist derivation incomplete): {rejections[:5]}"
        )
    finally:
        await _delete_container(container_id)
        await _stop_socket_proxy()


# ---------------------------------------------------------------------------
# dind engine-kill strip (opt-in: MODULO_RUNNER_DIND_TESTS=1)
# ---------------------------------------------------------------------------


async def test_engine_kill_mid_exec_is_retryable_never_success() -> None:
    """Killing the Docker ENGINE mid-exec: the client's in-flight exec fails
    immediately and never completes as ``exit_code == 0`` (the committed CI
    mechanism per the phase-0 spike; dind state is discarded with the
    engine)."""
    if os.environ.get("MODULO_RUNNER_DIND_TESTS") != "1":
        pytest.skip("opt-in dind strip: set MODULO_RUNNER_DIND_TESTS=1 (pulls ~560 MB)")
    async with aiodocker.Docker() as docker:
        try:
            await docker.images.inspect("docker:28-dind")
        except aiodocker.exceptions.DockerError:
            pytest.skip("docker:28-dind unavailable (pull it manually or in CI)")

        created = await docker.containers.create(
            config={
                "Image": "docker:28-dind",
                "Env": ["DOCKER_TLS_CERTDIR="],
                "Cmd": ["--host=unix:///var/run/docker.sock", "--host=tcp://0.0.0.0:2375"],
                "ExposedPorts": {"2375/tcp": {}},
                "HostConfig": {
                    "Privileged": True,
                    "AutoRemove": True,
                    "PortBindings": {"2375/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
                },
                "Labels": {"modulo.test": "d4-dind"},
            },
            name=f"modulo-dind-{uuid.uuid4().hex[:8]}",
        )
        dind = await docker.containers.get(created.id)
        await dind.start()
        info = await dind.show()
        ports = info["NetworkSettings"]["Ports"].get("2375/tcp") or []
        host_port = ports[0]["HostPort"] if ports else None
        assert host_port, "dind must publish 2375"
        dind_url = f"tcp://127.0.0.1:{host_port}"
        try:
            # Wait for the nested engine's readiness.
            ready = False
            for _ in range(60):
                try:
                    inner_probe = aiodocker.Docker(url=dind_url)
                    try:
                        await inner_probe.version()
                        ready = True
                    finally:
                        await inner_probe.close()
                    break
                except Exception:
                    await asyncio.sleep(1.0)
            assert ready, "dind engine never became ready"
            inner_client = aiodocker.Docker(url=dind_url)
            try:
                with contextlib.suppress(Exception):
                    await inner_client.images.pull("alpine:3.20")  # best-effort pre-pull into the nested store

                provider = DockerRuntimeProvider(docker_host=dind_url, default_image="alpine:3.20")
                spec = WorkspaceSpec(
                    environment_profile_id=uuid.uuid4(),
                    organisation_id=uuid.uuid4(),
                    image_ref="alpine:3.20",
                    workspace_network="bridge",
                    workspace_metadata={"modulo.run.id": "dind-run-1"},
                )
                # dind's dockerd occasionally drops a connection during its
                # internal setup — one retry on the workspace/exec setup.
                process = None
                for _attempt in range(2):
                    try:
                        ref = await provider.create_workspace(spec)
                        process = await provider.exec_command_stream(ref, ["sh", "-c", "echo nested; sleep 30"])
                        break
                    except aiodocker.exceptions.DockerError:
                        if process is not None:
                            break
                        await asyncio.sleep(2.0)
                assert process is not None, "workspace/exec could not be established inside dind"

                count = 0

                async def _consume() -> None:
                    nonlocal count
                    async for _chunk in process.chunks:
                        count += 1

                consume_task = asyncio.create_task(_consume())
                await asyncio.sleep(2.0)  # let 'nested' stream through
                # Kill the ENGINE (not just the container).
                await dind.kill()
                await asyncio.wait_for(consume_task, timeout=30)
                await asyncio.wait_for(process.done.wait(), timeout=30)
                assert count >= 1  # 'nested' was streamed live before the kill
                # Engine death is retryable — never a fabricated success.
                assert process.error is not None or process.exit_code is None
                assert process.exit_code != 0
                # And any follow-up command against the dead engine fails
                # loudly (never returns ExecResult(0)).
                with pytest.raises((aiodocker.exceptions.DockerError, OSError, RuntimeError, TimeoutError)):
                    await provider.exec_command(ref, ["echo", "after-death"], cmd_timeout=5)
            finally:
                with contextlib.suppress(Exception):
                    await inner_client.close()
        finally:
            with contextlib.suppress(Exception):
                await dind.kill()
