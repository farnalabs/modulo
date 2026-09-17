"""Tier 1b (FAR-934) shared testcontainers harness.

Builds real container-hosted fixtures for the 10 self-hostable connectors
(gitea, jenkins, n8n, youtrack, sonarqube, grafana, teamcity, trivy, codeclimate,
gitlab) and the 5 OpenAI-compatible local model servers (ollama, llamacpp,
localai, vllm, tgi) that the ``modulo.connectors`` and
``modulo.model_backends`` clients talk to.

Design rules (FAR-934):

- Every fixture waits on a REAL readiness probe before the test body runs —
  a bounded poll of an HTTP test probe, never a fixed sleep.
- A workstation without a Docker daemon must not fail the suite:
  ``tests/integration/conftest.py`` already skips every integration test
  loudly (with a stderr banner) when Docker is unreachable, and hard-fails
  CI in that case. This harness adds the same loud failure semantics to
  probe time-outs instead of letting a hung container look like a pass.
- Heavy fixtures (gitlab, vllm, tgi — multi-GB images, slow boot) only run
  in the nightly job (``ci.yml`` job ``tier1b-nightly``); the light set
  runs per-PR (``ci.yml`` job ``tier1b-light``).

Usage in a test module::

    from tests.helpers.testcontainers_harness import (
        ContainerHandle,
        ContainerSpec,
        probe_http,
        start_tier1b_container,
        stop_handle,
    )
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

SSRF_LOOPBACK_OPTIN = "127.0.0.0/8,::1/128"
EXEC_READINESS_TIMEOUT = 600


class Tier1bFixtureError(RuntimeError):
    """A Tier 1b fixture could not start or reach readiness before its deadline."""


def probe_http(
    path: str,
    *,
    ok_status_codes: tuple[int, ...] = (200,),
    timeout_seconds: float = 10.0,
) -> Callable[[int], str | None]:
    """Build an HTTP readiness probe: GET /{path} until an expected status.

    Returns ``None`` when the probe passes; a human-readable reason (for the
    loud fixture failure) otherwise.  Redirect responses are followed.
    """

    def _probe(port: int) -> str | None:
        url = f"http://127.0.0.1:{port}{path}"
        try:
            resp = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
        except httpx.HTTPError as exc:
            return f"GET {path} not reachable yet: {type(exc).__name__}"
        if resp.status_code in ok_status_codes:
            return None
        return f"GET {path} returned HTTP {resp.status_code} (expected {ok_status_codes})"

    return _probe


@dataclass
class ContainerSpec:
    """Declarative description of a Tier 1b container fixture."""

    image: str
    container_port: int
    env: dict[str, str] = field(default_factory=dict)
    command: str | list[str] | None = None
    ready_timeout_seconds: float = 600
    poll_interval_seconds: float = 2.0
    probe: Callable[[int], str | None] | None = None
    """Called with the mapped host port; None == ready, str == failure reason."""


@dataclass
class ContainerHandle:
    """A started container fixture with base_url and exec helpers."""

    spec: ContainerSpec
    host_port: int
    _docker_container: Any = field(default=None, repr=False, compare=False)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.host_port}"

    def exec(self, args: list[str], timeout_seconds: float = EXEC_READINESS_TIMEOUT) -> str:
        """Run args inside the container; return utf-8 stdout; raise on failure."""
        if self._docker_container is None:
            raise Tier1bFixtureError("container fixture already stopped")
        result = self._docker_container.exec(args)
        output = result.output
        out = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        if result.exit_code != 0:
            raise Tier1bFixtureError(f"container exec {args} failed (exit {result.exit_code}): {out[-2000:]}")
        return out

    def logs(self, tail: int = 100) -> str:
        """Return the last *tail* lines of the container log (diagnostics)."""
        if self._docker_container is None:
            raise Tier1bFixtureError("container fixture already stopped")
        out = self._docker_container.get_logs()
        logs = "\n".join(out) if isinstance(out, tuple) else str(out)
        return logs[-tail * 120 :]

    def stop(self) -> None:
        if self._docker_container is not None:
            # BestEffort teardown must never mask a test failure
            with contextlib.suppress(Exception):
                self._docker_container.stop()
            self._docker_container = None


def start_tier1b_container(spec: ContainerSpec) -> ContainerHandle:
    """Start *spec* via Testcontainers and wait on the real readiness probe.

    Raises ``Tier1bFixtureError`` upfront when no Docker daemon is reachable
    (the integration conftest then marks the suite skipped with the reason
    recorded), and at probe time-out with the last failure reason.

    The ``SSRF_ALLOW_PRIVATE_RANGES`` loopback opt-in must already be set by
    the caller — Tier 1b suites do it via a ``monkeypatch.setenv`` autouse
    fixture (the lint-clean mechanism); loopback consent is never assumed.
    """
    from testcontainers.core.docker_client import DockerClient
    from testcontainers.core.generic import DockerContainer

    DockerClient().client.ping()  # raises when there is no Docker daemon

    container = DockerContainer(spec.image)
    for key, value in spec.env.items():
        container.with_env(key, value)
    if spec.command:
        command = spec.command if isinstance(spec.command, list) else spec.command.split()
        container.with_command(command)
    container.with_exposed_ports(spec.container_port)
    container.start()

    host_port = int(container.get_exposed_port(spec.container_port))
    handle = ContainerHandle(spec=spec, host_port=host_port, _docker_container=container)

    deadline = time.monotonic() + spec.ready_timeout_seconds
    probe = spec.probe
    if probe is not None:
        last_reason = "probe did not run"
        while time.monotonic() < deadline:
            try:
                last_reason = probe(host_port)
            except Tier1bFixtureError:
                raise
            except Exception as exc:  # probe bugs must never kill the poll loop
                last_reason = f"probe raised {type(exc).__name__}: {exc}"
            if last_reason is None:
                break
            time.sleep(spec.poll_interval_seconds)
        else:
            handle.stop()
            raise Tier1bFixtureError(
                f"Fixture {spec.image!r} did not become ready within "
                f"{spec.ready_timeout_seconds:.0f}s. Last probe failure: {last_reason}"
            )
    return handle
