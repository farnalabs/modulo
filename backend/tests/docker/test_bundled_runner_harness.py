"""Bundled Runner CI-harness tests (FAR-773) — the compose-rig assertions.

These are the harness-level assertions that run against the
``deploy/compose/runner-ci.yml`` rig (socket-proxy + dind + workspace
network). They live in the docker-marked suite (``tests/docker/``), which is
a STANDALONE rig: no Postgres/testcontainers conftest chain is inherited
here — only a live Docker engine is required. The unit/docker provider tests
cover provider behaviour in isolation; THIS file verifies the topology the
CI job boots:

  1. the spike (a) allow/deny matrix through the proxy, PLUS the linuxserver
     deny-refinement probes: with the production config, archive/logs/top
     against a real running container must stay 403 (so widening
     ``ALLOW_LOGS=1`` etc. in production FAILS CI), and the ``PR--``
     proxy-reject flag must actually appear in the proxy log for them
     (spike criteria 1 + 3);
  2. the nested workspace round-trip THROUGH the proxy (spike (c)):
     create (workspace-net attach at create) -> start -> exec -> output;
  3. the proxy-completeness assertion: the exercise must not trigger ANY
     proxy rejection (log scan keyed on the ``PR--`` proxy-reject flag only
     — spike criterion 3). Transient first-pull 500s (spike surprise #5)
     are NOT ``PR--`` lines and cannot fail the scan, and the CI job
     pre-pulls ``alpine:3.20`` on the engine before the suite, so the
     cold-cache flake window is closed deterministically — no retry loop
     in the exercise itself (.github/workflows/runner-ci-harness.yml);
  4. the isolation probe: a provisioned workspace cannot reach the proxy
     endpoint (name-blocked + IP-blocked — spike (c) step 4);
  5. the engine-kill via dind: killing the engine with a nested container
     running must fail the client immediately, never ``ExecResult(0)``
     (spike (d) — the committed CI mechanism);
  6. the workspace-network rig guard: a missing compose workspace network
     must SKIP only on a plain local engine      (``MODULO_RUNNER_HARNESS_NETWORK`` unset)
     and FAIL LOUDLY when the env var is explicitly set (CI always sets it
     — a missing network there is a real rig failure, never a skip);
  7. the composition guards (pure file-text, no engine): the overlay holds
     the workspace network, never attaches the proxy to it, pins the proxy
     to the SAME digest as production's deploy/compose/runner.yml, and its
     13-key proxy allowlist env block matches production key-for-key
     (digest bumps must move the two files together).

All tests are ``@pytest.mark.docker``. Engine-dependent tests skip cleanly
(fixture level) when no Docker engine is reachable; the composition/guard
tests are engine-free. When the compose rig itself is absent (plain local
engine, no overlay), the topology-dependent tests skip with an explicit
reason rather than failing — the CI job boots the rig first.
"""

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiodocker
import aiohttp
import pytest

from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider

pytestmark = [pytest.mark.docker]

#: env vars the CI job (and local runs) set to point the harness at the rig.
_HARNESS_PROXY_ENV = "MODULO_RUNNER_HARNESS_PROXY_HOST"
_HARNESS_DIND_ENV = "MODULO_RUNNER_HARNESS_DIND_HOST"
#: the workspace-network env var the CI job sets to the overlay's compose
#: network name. EXPLICITLY SET in CI — so a missing network there is a
#: loud failure, not a skip (plain local engines leave it unset).
_HARNESS_NETWORK_ENV = "MODULO_RUNNER_HARNESS_NETWORK"
#: the compose overlay's workspace network name (workspace-network-holder
#: makes compose materialize it).
_HARNESS_WORKSPACE_NETWORK = os.environ.get(_HARNESS_NETWORK_ENV, "runner-ci-workspace")

_MATRIX_ALLOWED = ("/_ping", "/version", "/info", "/containers/json", "/images/json")
_MATRIX_DENIED = ("/networks", "/swarm", "/volumes", "/nodes")


_proactor_loop: asyncio.AbstractEventLoop | None = None
_proactor_ready = threading.Event()


def _proactor_main() -> None:
    global _proactor_loop
    _proactor_loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(_proactor_loop)
    _proactor_ready.set()
    _proactor_loop.run_forever()


def _run_on_local_engine_loop(factory: Callable[[], Any], timeout: float = 90.0) -> Any:
    """Run a LOCAL-engine coroutine on a dedicated Proactor event loop.

    Two Windows constraints meet here:
    - Docker Desktop's named pipe (aiodocker's local-engine default) only
      works on a ProactorEventLoop, and a conftest elsewhere in the suite
      (tests/integration) pins the ``WindowsSelectorEventLoopPolicy`` when
      it loads — this suite must work in both invocation shapes (standalone
      and alongside the integration suite), so the engine reads must not
      depend on which policy the pytest-asyncio session loop carries;
    - the harness tests run inside pytest-asyncio's session loop, so the
      local-engine reads cannot just ``run_until_complete`` a second loop in
      the same thread.

    A single background thread therefore drives a Proactor loop and this
    helper bridges to it via ``run_coroutine_threadsafe``. All results are
    plain data — no cross-loop object reuse. (On Linux the same bridge is a
    harmless plain loop.)
    """
    global _proactor_loop
    if _proactor_loop is None:
        threading.Thread(target=_proactor_main, name="harness-proactor", daemon=True).start()
        assert _proactor_ready.wait(timeout=10), "harness proactor loop thread never started"
    assert _proactor_loop is not None
    return asyncio.run_coroutine_threadsafe(factory(), _proactor_loop).result(timeout=timeout)


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


def _engine_probe() -> bool:
    """Sync engine reachability check (Proactor bridge: on win32 the named
    pipe needs a Proactor loop, and the session loop's policy must not
    constrain this probe)."""
    return bool(_run_on_local_engine_loop(_engine_reachable))


#: Engine-independent tests in this module: pure file-text assertions and
#: monkeypatched pure-logic guards that must run even with no Docker engine
#: reachable. This module-level autouse fixture OVERRIDES
#: tests/docker/conftest.py's same-named gate for that purpose (closer
#: fixture definition wins) without touching the shared conftest.
_ENGINE_INDEPENDENT_TESTS = frozenset(
    {
        "test_overlay_declares_workspace_network_holder",
        "test_overlay_matches_prod_proxy_config",
        "test_workspace_network_guard_fails_loud_when_env_is_set",
        "test_workspace_network_guard_skips_when_env_is_unset",
    }
)


@pytest.fixture(autouse=True)
def _require_engine(request: pytest.FixtureRequest) -> None:
    if request.node.name in _ENGINE_INDEPENDENT_TESTS:
        return
    if not _engine_probe():
        pytest.skip("no reachable Docker engine (docker-marked tests)")


def _proxy_host() -> str | None:
    return os.environ.get(_HARNESS_PROXY_ENV)


def _dind_host() -> str | None:
    return os.environ.get(_HARNESS_DIND_ENV)


def _require_rig() -> str:
    host = _proxy_host()
    if not host:
        pytest.skip(
            f"runner-ci compose rig not up (set {_HARNESS_PROXY_ENV} to the "
            "socket-proxy tcp:// endpoint; see deploy/compose/README-runner-ci.md)"
        )
    return host


async def _http_get(session: aiohttp.ClientSession, base: str, path: str) -> int:
    """GET one path through the proxy and return the status code."""
    try:
        async with session.get(f"http://{base}{path}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            # Drain the body so the connection is reusable.
            await resp.read()
            return resp.status
    except (aiohttp.ClientError, TimeoutError):
        return 0


async def _http_get_body(session: aiohttp.ClientSession, base: str, path: str) -> tuple[int, bytes]:
    """GET one path through the proxy and return (status, body)."""
    try:
        async with session.get(f"http://{base}{path}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.read()
            return resp.status, body
    except (aiohttp.ClientError, TimeoutError):
        return 0, b""


# ---------------------------------------------------------------------------
# 1. Allow/deny matrix smoke (spike (a))
# ---------------------------------------------------------------------------


async def test_proxy_allow_deny_matrix() -> None:
    """The production allowlist shape through the proxy: derived-surface
    endpoints pass; categories outside it (networks/swarm/volumes/nodes)
    are denied 403; and the linuxserver deny-refinements (ALLOW_ARCHIVE/
    EXPORT/LOGS/TOP/CHANGE=0) keep archive/logs/top 403 EVEN against a real
    running container even though the CONTAINERS category is open — the
    assertion that makes widening ``ALLOW_LOGS=1`` (etc.) in production
    FAIL CI. The denied requests must also carry the ``PR--``
    proxy-reject termination flag in the proxy log (spike criterion 3),
    which this test now asserts instead of only promising."""
    host = _require_rig()
    async with aiohttp.ClientSession() as session:
        for path in _MATRIX_ALLOWED:
            status = await _http_get(session, host, path)
            assert status == 200, f"GET {path} through the proxy must be allowed (got {status})"
        for path in _MATRIX_DENIED:
            status = await _http_get(session, host, path)
            assert status == 403, f"GET {path} through the proxy must be denied (got {status})"

        # Deny-refinement probes against a REAL running container: list the
        # engine's containers through the ALLOWED /containers/json and pick
        # the rig's proxy container (always present and running) as target.
        list_status, list_body = await _http_get_body(session, host, "/containers/json")
        assert list_status == 200, f"GET /containers/json through the proxy must be allowed (got {list_status})"
        containers = json.loads(list_body)
        assert containers, "the engine must report at least one running container"
        target = next(
            (c["Id"] for c in containers if c.get("Labels", {}).get("modulo.test") == "runner-ci-proxy"),
            containers[0]["Id"],
        )
        refine_paths = (
            f"/containers/{target}/archive?path=/etc/hostname",
            f"/containers/{target}/logs",
            f"/containers/{target}/top",
        )
        # Offset marker BEFORE the refinement probes: the earlier *_MATRIX_DENIED
        # 403s also carry PR--, so completeness of THIS promise must only judge
        # its own slice of the log.
        pre_refine = _proxy_log_lines()
        for path in refine_paths:
            status = await _http_get(session, host, path)
            assert status == 403, (
                f"GET {path} through the proxy must stay denied by the linuxserver "
                "deny-refinement (ALLOW_* = 0, even with CONTAINERS=1) - a non-403 "
                "here means the rig's proxy config drifted from production's, and "
                "widening ALLOW_LOGS in prod would silently pass CI"
            )
        # The PR-- flag must ACTUALLY appear for these denials (spike
        # criterion 3) - bounded poll for HAProxy's log flush.
        rejections: list[str] = []
        for _ in range(10):
            rejections = _rejection_lines(_proxy_log_lines()[len(pre_refine) :])
            if rejections:
                break
            await asyncio.sleep(0.5)
        assert rejections, (
            "the deny-refinement probes must carry the PR-- proxy-reject flag in "
            f"the proxy log (only 403-without-PR-- observed) for: {refine_paths}"
        )


# ---------------------------------------------------------------------------
# 2 + 3. Nested round-trip through the proxy + completeness log scan
# ---------------------------------------------------------------------------


async def _containers_by_label_op(label: str) -> list[dict[str, Any]]:
    """List local-engine containers by the rig label with their resolved
    harness-network IP (plain data — safe across the Proactor loop)."""
    async with aiodocker.Docker() as docker:
        containers = await docker.containers.list(filters={"label": [f"modulo.test={label}"]})
        out: list[dict[str, Any]] = []
        for container in containers:
            info = await container.show()
            nets = info.get("NetworkSettings", {}).get("Networks", {})
            ip = next(
                (cfg.get("IPAddress") for name, cfg in nets.items() if cfg.get("IPAddress")),
                None,
            )
            out.append({"id": container.id, "ip": ip})
        return out


def _containers_by_label(label: str) -> list[dict[str, Any]]:
    return list(_run_on_local_engine_loop(lambda: _containers_by_label_op(label)))


async def _proxy_log_lines_op() -> list[str]:
    """The proxy container's per-request log lines (direct engine access —
    the proxy's own /containers/{id}/logs endpoint is NOT in its allowlist
    by design, so we read via the local engine, not the proxy)."""
    containers = await _containers_by_label_op("runner-ci-proxy")
    if not containers:
        return []
    async with aiodocker.Docker() as docker:
        container = await docker.containers.get(containers[-1]["id"])
        logs = await container.log(stdout=True, stderr=True)
    return [str(line) for line in logs]


def _proxy_log_lines() -> list[str]:
    return list(_run_on_local_engine_loop(_proxy_log_lines_op))


async def _kill_container_by_label_op(label: str) -> None:
    containers = await _containers_by_label_op(label)
    async with aiodocker.Docker() as docker:
        for container in containers:
            with contextlib.suppress(Exception):
                handler = await docker.containers.get(container["id"])
                await handler.kill()


def _rejection_lines(lines: list[str]) -> list[str]:
    """Filter the proxy log to proxy-rejected requests.

    ``PR--`` is HAProxy's proxy-reject termination flag — EVERY deny shows
    it, and it cannot appear on a proxied success line (a bare " 403 "
    substring is ambiguous: the bytes-transferred field can legitimately
    contain the digits 403 on a 200 pull stream)."""
    return [line for line in lines if "PR--" in line]


async def _workspace_network_exists_op(network: str) -> bool:
    async with aiodocker.Docker() as docker:
        try:
            await docker.networks.get(network)
        except aiodocker.exceptions.DockerError:
            return False
        return True


def _workspace_network_exists(network: str) -> bool:
    return bool(_run_on_local_engine_loop(lambda: _workspace_network_exists_op(network)))


def _require_workspace_network_rig(network: str) -> None:
    """The compose workspace network must exist to run the round-trip.

    Fail/skip split (FAR-773 QA fix F3): ``MODULO_RUNNER_HARNESS_NETWORK``
    EXPLICITLY SET (the CI job always sets it) + missing network = a REAL rig
    failure -> ``pytest.fail`` naming the env var and the overlay file;
    never a silent skip. The skip branch applies only on a plain local
    engine with no rig (env var unset).
    """
    if _workspace_network_exists(network):
        return
    if os.environ.get(_HARNESS_NETWORK_ENV) is not None:
        pytest.fail(
            f"{_HARNESS_NETWORK_ENV} is explicitly set to '{network}' but the "
            "compose workspace network does not exist on the engine. In CI (which "
            "always sets it) this is a real rig failure, not a skip: boot "
            "deploy/compose/runner-ci.yml and verify the workspace-network-holder "
            "service materialised the network. Only a plain local engine without "
            "the rig (env var unset) may skip this topology-dependent test."
        )
    pytest.skip(
        f"compose workspace network '{network}' not found (plain local engine, "
        f"no runner-ci rig — {_HARNESS_NETWORK_ENV} is unset)"
    )


async def _control_probe_reachable_op(network: str, proxy_ip: str) -> bool:
    """Control experiment for the engine-dependent isolation premise: a
    scratch alpine container attached to the SAME workspace network probes
    the proxy's harness-network IP. Returns whether the cross-bridge route
    is reachable on this engine (plain data across the Proactor bridge)."""
    async with aiodocker.Docker() as docker:
        created = await docker.containers.create(
            config={
                "Image": "alpine:3.20",
                "Cmd": ["sleep", "120"],
                "HostConfig": {"NetworkMode": network, "AutoRemove": False},
            },
            name=f"runner-ci-iso-ctrl-{uuid.uuid4().hex[:8]}",
        )
        container = await docker.containers.get(created.id)
        try:
            await container.start()
            exec_instance = await container.exec(
                cmd=["sh", "-c", f"wget -q -T 3 -O - http://{proxy_ip}:2375/_ping 2>&1 || echo IP-BLOCKED"]
            )
            started = exec_instance.start(detach=False)
            if asyncio.iscoroutine(started):
                started = await started
            out = b""
            while True:
                frame = await started.read_out()
                if frame is None:
                    break
                data = getattr(frame, "data", frame)
                if data:
                    out += bytes(data)
            return b"IP-BLOCKED" not in out
        finally:
            with contextlib.suppress(Exception):
                await container.delete(force=True)


def _control_probe_reachable(network: str, proxy_ip: str) -> bool:
    return bool(_run_on_local_engine_loop(lambda: _control_probe_reachable_op(network, proxy_ip)))


def _spec(network: str | None) -> WorkspaceSpec:
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        image_ref="alpine:3.20",
        capabilities=[],
        timeout_seconds=300,
        resource_limits={"memory_mb": 256},
        egress_policy="outbound",
        persistence_policy="ephemeral",
        labels={},
        workspace_metadata={
            "modulo.run.id": "harness-run-1",
            "modulo.org.id": "harness-org-1",
        },
        workspace_network=network,
    )


async def test_nested_roundtrip_through_proxy_and_completeness() -> None:
    """The full harness exercise (spike (c) shape): via the PROXY ONLY —
    create a workspace attached to the compose workspace network at create
    (no /networks/* calls), start it, exec a command, capture output — then
    the proxy-completeness assertion: NO request in the exercise may have
    been REJECTED by the proxy. The scan is keyed on HAProxy's ``PR--``
    proxy-reject termination flag only: transient first-pull 500s (spike
    surprise #5, registry-side) are not PR-- lines and cannot fail it.
    The cold-cache flake is closed deterministically upstream — the CI job
    pre-pulls alpine:3.20 on the engine before the suite
    (.github/workflows/runner-ci-harness.yml) — so the exercise itself
    needs NO retry loop."""
    host = _require_rig()
    network = _HARNESS_WORKSPACE_NETWORK

    # The rig's workspace network must exist (the holder service created it):
    # env set (CI) + missing => FAIL loudly; env unset (plain engine) => skip.
    _require_workspace_network_rig(network)

    # Log offset marker (the matrix smoke test INTENTIONALLY triggers 403s
    # through the same proxy): completeness must only judge this exercise's
    # requests, so note the current log depth up-front and scan beyond it.
    pre_lines = _proxy_log_lines()

    provider = DockerRuntimeProvider(docker_host=f"tcp://{host}", default_image="alpine:3.20")
    ref: str | None = None
    try:
        ref = await provider.create_workspace(_spec(network))
        status = await provider.get_workspace_status(ref)
        assert status == "running"
        result = await provider.exec_command(ref, ["sh", "-c", "echo harness-roundtrip-through-proxy"])
        assert result.exit_code == 0, f"exec through the proxy failed: {result.stderr!r}"
        assert "harness-roundtrip-through-proxy" in result.stdout
    finally:
        if ref is not None:
            with contextlib.suppress(Exception):
                await provider.destroy_workspace(ref)
        with contextlib.suppress(Exception):
            await provider.close()

    # Completeness: scan ONLY this exercise's slice of the proxy's
    # per-request logs for rejections.
    post_lines = _proxy_log_lines()
    exercise_lines = post_lines[len(pre_lines) :]
    rejections = _rejection_lines(exercise_lines)
    assert not rejections, (
        "the proxy rejected at least one request during the harness exercise "
        f"(allowlist derivation incomplete): {rejections[:5]}"
    )


def test_workspace_network_guard_fails_loud_when_env_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3 branch 1 (pure logic — no engine): ``MODULO_RUNNER_HARNESS_NETWORK``
    EXPLICITLY SET (CI always sets it) + missing network must FAIL loudly
    naming the env var and the overlay file — never skip."""
    monkeypatch.setattr(sys.modules[__name__], "_workspace_network_exists", lambda _network: False)
    monkeypatch.setenv(_HARNESS_NETWORK_ENV, "runner-ci-workspace")
    with pytest.raises(pytest.fail.Exception, match="explicitly set"):
        _require_workspace_network_rig("runner-ci-workspace")


def test_workspace_network_guard_skips_when_env_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3 branch 2 (pure logic — no engine): the env var UNSET (plain local
    engine, no rig) + missing network must SKIP — the only vacuous path."""
    monkeypatch.setattr(sys.modules[__name__], "_workspace_network_exists", lambda _network: False)
    monkeypatch.delenv(_HARNESS_NETWORK_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="plain local engine"):
        _require_workspace_network_rig("runner-ci-workspace")


# ---------------------------------------------------------------------------
# 4. Isolation probe (spike (c) step 4)
# ---------------------------------------------------------------------------


async def test_provisioned_workspace_cannot_reach_proxy() -> None:
    """A workspace container attached ONLY to the compose workspace network
    is blocked from the proxy endpoint BOTH ways: name-blocked (the
    docker-socket-proxy alias exists only on the harness network) and
    IP-blocked (docker bridge-network isolation rules)."""
    host = _require_rig()
    network = _HARNESS_WORKSPACE_NETWORK

    provider = DockerRuntimeProvider(docker_host=f"tcp://{host}", default_image="alpine:3.20")
    ref: str | None = None
    try:
        ref = await provider.create_workspace(_spec(network))
        # Name-blocked: the alias does not resolve from the workspace net.
        name_result = await provider.exec_command(
            ref, ["sh", "-c", "wget -q -T 3 -O - http://docker-socket-proxy:2375/_ping 2>&1 || echo NAME-BLOCKED"]
        )
        assert "NAME-BLOCKED" in name_result.stdout or "bad address" in name_result.stdout, (
            f"workspace must not resolve the proxy alias; got exit={name_result.exit_code} out={name_result.stdout!r}"
        )
        # IP-blocked: even the proxy's harness-network IP is unreachable from
        # the workspace bridge — WHERE THE ENGINE ENFORCES bridge isolation.
        # The premise is engine-dependent (observed 2026-09-10: Docker
        # Desktop engine 29.7.2 no longer installs the DOCKER-ISOLATION
        # ruleset), so a control experiment on the SAME workspace network
        # validates it first; when the engine demonstrably permits
        # cross-bridge traffic we skip LOUDLY rather than weaken the
        # assertion or fabricate a pass.
        containers = _containers_by_label("runner-ci-proxy")
        assert containers, "proxy container must be running for the IP probe"
        proxy_ip = containers[-1]["ip"]
        assert proxy_ip, "proxy must have an IP on the harness (non-workspace) network"
        if not _control_probe_reachable(network, proxy_ip):
            # Control confirms isolation: the workspace must be blocked too.
            ip_result = await provider.exec_command(
                ref,
                ["sh", "-c", f"wget -q -T 3 -O - http://{proxy_ip}:2375/_ping 2>&1 || echo IP-BLOCKED"],
            )
            assert "IP-BLOCKED" in ip_result.stdout or ip_result.exit_code != 0, (
                f"workspace must not reach the proxy by IP; got exit={ip_result.exit_code} out={ip_result.stdout!r}"
            )
        else:
            pytest.skip(
                "engine does not enforce cross-bridge isolation (control experiment reached the proxy from the "
                "workspace network) - IP-leg of the isolation probe requires an engine-enforced isolation "
                "premise; operator mitigation: host firewall the proxy endpoint (see "
                "docs/security/bundled-runner-trust-boundary.md)"
            )
    finally:
        if ref is not None:
            with contextlib.suppress(Exception):
                await provider.destroy_workspace(ref)
        with contextlib.suppress(Exception):
            await provider.close()


# ---------------------------------------------------------------------------
# 5. Engine-kill via dind (spike (d) — the committed CI mechanism)
# ---------------------------------------------------------------------------


async def _dind_ready(url: str, attempts: int = 60) -> aiodocker.Docker:
    """Wait for the nested dind engine and return a client."""
    for _ in range(attempts):
        client = aiodocker.Docker(url=url)
        try:
            await client.version()
            return client
        except Exception:
            with contextlib.suppress(Exception):
                await client.close()
            await asyncio.sleep(1.0)
    raise TimeoutError(f"dind engine never became ready at {url}")


async def test_engine_kill_via_dind_fails_client_never_success() -> None:
    """Spike (d): kill the dind ENGINE with a nested container running —
    the client's in-flight exec fails immediately and any follow-up command
    fails loudly; it must NEVER complete as ``ExecResult(exit_code=0)``.
    The nested container's state is discarded with the engine."""
    dind_host = _dind_host()
    if not dind_host:
        pytest.skip(
            f"runner-ci dind not up (set {_HARNESS_DIND_ENV} to the dind tcp:// endpoint; "
            "see deploy/compose/README-runner-ci.md)"
        )

    dind_client = await _dind_ready(dind_host)
    try:
        # Pre-pull into the nested store (first-run cost lives here, not in
        # the assertion — spike surprise #6).
        with contextlib.suppress(Exception):
            await dind_client.images.pull("alpine:3.20")

        provider = DockerRuntimeProvider(docker_host=dind_host, default_image="alpine:3.20")
        # dind's dockerd occasionally drops a connection during internal
        # setup — one retry on workspace/exec setup (same tolerance as the
        # tests/docker dind strip).
        process = None
        ref: str | None = None
        for _attempt in range(2):
            try:
                spec = _spec(None)
                spec.workspace_network = "bridge"  # dind has no compose networks
                ref = await provider.create_workspace(spec)
                process = await provider.exec_command_stream(ref, ["sh", "-c", "echo nested; sleep 30"])
                break
            except aiodocker.exceptions.DockerError:
                await asyncio.sleep(2.0)
        assert ref is not None, "workspace could not be provisioned inside dind"
        assert process is not None, "exec stream could not be established inside dind"

        count = 0

        async def _consume() -> None:
            nonlocal count
            async for _chunk in process.chunks:
                count += 1

        consume_task = asyncio.create_task(_consume())
        await asyncio.sleep(2.0)  # let 'nested' stream through
        assert count >= 1, "the nested exec must stream output before the kill"

        # Find and kill the dind ENGINE container (the compose service).
        assert _containers_by_label("runner-ci-dind"), "runner-ci dind container must be running"
        _run_on_local_engine_loop(lambda: _kill_container_by_label_op("runner-ci-dind"))

        await asyncio.wait_for(consume_task, timeout=30)
        await asyncio.wait_for(process.done.wait(), timeout=30)
        # Engine death is retryable — never a fabricated success.
        assert process.error is not None or process.exit_code is None
        assert process.exit_code != 0
        # Follow-up commands against the dead engine fail loudly.
        with pytest.raises((aiodocker.exceptions.DockerError, OSError, RuntimeError, TimeoutError)):
            await provider.exec_command(ref, ["echo", "after-death"], cmd_timeout=5)
    finally:
        with contextlib.suppress(Exception):
            await dind_client.close()


# ---------------------------------------------------------------------------
# Composition sanity: the overlay file itself (pure file-text, no engine)
# ---------------------------------------------------------------------------


def _overlay_path() -> Path:
    return Path(__file__).resolve().parents[3] / "deploy" / "compose" / "runner-ci.yml"


def _prod_overlay_path() -> Path:
    return Path(__file__).resolve().parents[3] / "deploy" / "compose" / "runner.yml"


def test_overlay_declares_workspace_network_holder() -> None:
    """Spike surprise #1 guard: the overlay must reference-hold the
    workspace network (a service attached to it) or compose --profile never
    creates it — and the proxy must never attach to it."""
    overlay = _overlay_path()
    if not overlay.exists():
        pytest.skip("runner-ci overlay not present (running outside the repo checkout)")
    text = overlay.read_text(encoding="utf-8")
    assert "workspace-network-holder" in text, "the holder service must exist"
    # The proxy service block attaches to `harness` only.
    proxy_block = text.split("docker-socket-proxy:", 1)[1].split("\n  dind:", 1)[0]
    assert "- harness" in proxy_block
    assert "- workspace" not in proxy_block


_PROXY_IMAGE_RE = re.compile(r"image:\s*lscr\.io/linuxserver/socket-proxy@sha256:([0-9a-f]{64})")
_PROXY_ENV_RE = re.compile(r"^ {6}([A-Z][A-Z_]*): \"([^\"]*)\"$", re.MULTILINE)
#: the production proxy allowlist env block is exactly these 13 switches.
_EXPECTED_PROXY_ENV_KEYS = frozenset(
    {
        "CONTAINERS",
        "EXEC",
        "IMAGES",
        "PING",
        "VERSION",
        "INFO",
        "POST",
        "ALLOW_ARCHIVE",
        "ALLOW_EXPORT",
        "ALLOW_LOGS",
        "ALLOW_TOP",
        "ALLOW_CHANGE",
        "LOG_LEVEL",
    }
)


def _read_text(path: Path, why: str) -> str:
    if not path.exists():
        pytest.skip(f"{why} not present (running outside the repo checkout)")
    return path.read_text(encoding="utf-8")


def _proxy_digest(text: str, source: str) -> str:
    match = _PROXY_IMAGE_RE.search(text)
    assert match is not None, f"{source} must digest-pin the linuxserver/socket-proxy image; no image pin found"
    return match.group(1)


def _proxy_env_block(text: str, source: str) -> dict[str, str]:
    block_match = re.compile(r"^  docker-socket-proxy:\n(.*?)(?=^  \S|\Z)", re.DOTALL | re.MULTILINE).search(text)
    assert block_match is not None, f"{source} must define a docker-socket-proxy service"
    env = dict(_PROXY_ENV_RE.findall(block_match.group(1)))
    assert set(env) == _EXPECTED_PROXY_ENV_KEYS, (
        f"{source}'s proxy env block must be exactly the 13-key production allowlist; got {sorted(env)}"
    )
    return env


def test_overlay_matches_prod_proxy_config() -> None:
    """FAR-773 QA fixes F7 + R3 (pure file-text, no engine): the CI rig's
    proxy must validate EXACTLY what production ships — (1) the rig's proxy
    image digest equals deploy/compose/runner.yml's (the spike-verified
    build; digest bumps must move the two files together), and (2) the
    rig's 13-key proxy allowlist env block matches production key-for-key
    (drift guard)."""
    rig_text = _read_text(_overlay_path(), "runner-ci overlay")
    prod_text = _read_text(_prod_overlay_path(), "production runner overlay")

    rig_digest = _proxy_digest(rig_text, "deploy/compose/runner-ci.yml")
    prod_digest = _proxy_digest(prod_text, "deploy/compose/runner.yml")
    assert rig_digest == prod_digest, (
        "the CI rig's socket-proxy digest must match production's pin "
        "(digest bumps must move runner-ci.yml + runner.yml together): "
        f"rig={rig_digest} prod={prod_digest}"
    )

    rig_env = _proxy_env_block(rig_text, "deploy/compose/runner-ci.yml")
    prod_env = _proxy_env_block(prod_text, "deploy/compose/runner.yml")
    assert rig_env == prod_env, (
        f"the rig's proxy allowlist env block must match production key-for-key (rig={rig_env} prod={prod_env})"
    )
