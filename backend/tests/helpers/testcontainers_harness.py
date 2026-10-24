"""Tier 1b (FAR-934) shared testcontainers harness.

Builds real container-hosted fixtures for the 9 self-hostable connectors
(gitea, jenkins, n8n, youtrack, sonarqube, grafana, teamcity, codeclimate,
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
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

SSRF_LOOPBACK_OPTIN = "127.0.0.0/8,::1/128"

# A container-registry outage is transient, and it aborts a pull that would
# succeed moments later: on 2026-10-24 quay.io returned 503 for the Keycloak
# manifest and the auth tier errored on main (run 40076695793). The pull is
# therefore retried with a bounded backoff. These are DELAYS rather than a
# count so the worst case (~100s) stays inside the CI job's pytest budget;
# they are deliberately not exponential-unbounded — a registry that is still
# down after this window is an outage CI must report, not paper over.
TRANSIENT_PULL_RETRY_DELAYS_SECONDS: tuple[float, ...] = (10.0, 30.0, 60.0)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield *exc* and every exception it wraps (``__cause__`` / ``__context__``)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient_registry_error(exc: BaseException) -> bool:
    """True when *exc* is a registry/daemon failure a retry can plausibly clear.

    Deliberately narrow so a genuine failure is never retried: a registry
    outage surfaces through the Docker SDK as an ``APIError`` whose
    ``response.status_code`` is a 5xx (the daemon wraps the registry's own 503
    in a 500), and a connection reset mid-pull as a ``requests`` transport
    error. A 404 for a renamed tag, an unsupported manifest format or a
    disk-full abort is a real failure and is raised on the first attempt.
    """
    for err in _exception_chain(exc):
        status = getattr(getattr(err, "response", None), "status_code", None)
        if isinstance(status, int) and status >= 500:
            return True
        if type(err).__module__.startswith("requests.exceptions"):
            return True
    return False


def start_container_with_transient_pull_retry(container: Any, image: str) -> None:
    """``container.start()``, retrying transient registry/connection failures.

    Each retry is announced on stderr so a flaky registry leaves a visible
    trace in the job log instead of silently masking an outage. The final
    attempt's error propagates unchanged, so the caller still turns a
    genuinely broken fixture into a loud ``Tier1bFixtureError`` — a registry
    that stays down keeps failing CI rather than degrading to a skip.
    """
    delays: tuple[float | None, ...] = (*TRANSIENT_PULL_RETRY_DELAYS_SECONDS, None)
    attempts = len(delays)
    for attempt, delay in enumerate(delays, start=1):
        try:
            container.start()
            return
        except Exception as exc:
            if delay is None or not is_transient_registry_error(exc):
                raise
            sys.stderr.write(
                f"Tier 1b fixture {image!r}: transient registry failure on attempt "
                f"{attempt}/{attempts} ({type(exc).__name__}: {exc}); retrying in {delay:.0f}s\n"
            )
            sys.stderr.flush()
            time.sleep(delay)


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
    container_port: int | None
    """None for port-less containers (CLI-only images like ``codeclimate``) — no port mapping is set up."""
    env: dict[str, str] = field(default_factory=dict)
    command: str | list[str] | None = None
    volumes: list[tuple[str, str]] | None = None
    """Optional [(host_dir, container_path)] bind mounts for seeded fixtures (LocalAI model seeds)."""
    volume_mode: str = "rw"
    """Docker bind-mount mode for ``volumes`` (testcontainers defaults to read-only; seeding needs rw)."""
    ready_timeout_seconds: float = 600
    poll_interval_seconds: float = 2.0
    probe: Callable[[int], str | None] | None = None
    """Called with the mapped host port; None == ready, str == failure reason."""


@dataclass
class ContainerHandle:
    """A started container fixture with base_url and exec helpers."""

    spec: ContainerSpec
    host_port: int | None
    _docker_container: Any = field(default=None, repr=False, compare=False)
    creds: dict[str, str] = field(default_factory=dict)
    """Fixture-minted credentials (tokens/passwords) keyed by a stable name.

    Session-scoped fixtures publish credentials here for dependent fixtures to
    read, instead of dynamically assigning ``handle.<attr>`` (FAR-934 review).
    """

    @property
    def base_url(self) -> str:
        if self.host_port is None:
            raise Tier1bFixtureError(
                f"container {self.spec.image} was started without a port mapping "
                "(container_port=None); base_url is not addressable"
            )
        return f"http://127.0.0.1:{self.host_port}"

    def exec(self, args: list[str], user: str | None = None) -> str:
        """Run args inside the container; return utf-8 stdout; raise on failure.

        ``user`` is optional (docker exec user spec, e.g. ``"git"`` or
        ``"1000:1000"``) — needed for images whose CLI tools refuse to run
        as the container's default root user (gitea, notably).

        There is deliberately no timeout parameter: testcontainers 4.15's
        ``ExecConfig`` exposes no timeout, so accepting one here would be
        dead code (FAR-934 review). A hung exec is bounded by the suite-level
        ``--timeout`` in CI.
        """
        if self._docker_container is None:
            raise Tier1bFixtureError("container fixture already stopped")
        from testcontainers.core.container import ExecConfig as _ExecConfig

        result = self._docker_container.exec(_ExecConfig(command=args, user=user))
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

    container = DockerContainer(spec.image)
    try:
        DockerClient().client.ping()  # raises when there is no Docker daemon

        for key, value in spec.env.items():
            container.with_env(key, value)
        if spec.command:
            command = spec.command if isinstance(spec.command, list) else spec.command.split()
            container.with_command(command)
        for host_dir, mount_path in spec.volumes or []:
            container.with_volume_mapping(host_dir, mount_path, spec.volume_mode)
        if spec.container_port is not None:
            container.with_exposed_ports(spec.container_port)
        # Retries ONLY a transient registry/connection failure (borderline
        # outages, e.g. quay.io 503); every other start error is raised on the
        # first attempt and becomes the loud Tier1bFixtureError below.
        start_container_with_transient_pull_retry(container, spec.image)
    except Exception as exc:
        # Docker-level unavailability (no daemon, image pull/start failure — e.g.
        # a multi-GB pull exhausting the runner's disk) is a FIXTURE problem, not
        # a test failure: every Tier 1b fixture translates Tier1bFixtureError into
        # a loud, recorded pytest.skip. Without this wrap the raw docker error
        # (docker.errors.ImageNotFound / requests.HTTPError) escapes and ERRORs
        # every dependent test instead of skipping it (observed on PR #773).
        with contextlib.suppress(Exception):
            container.stop()
        raise Tier1bFixtureError(
            f"Fixture {spec.image!r} could not be started on this Docker host: {type(exc).__name__}: {exc}"
        ) from exc
    # From this point on the container EXISTS — any failure (port-mapping
    # read, probe poll/timeout) must stop it before the exception propagates,
    # or a red test leaks a live container on the host.
    try:
        host_port = None if spec.container_port is None else int(container.get_exposed_port(spec.container_port))
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
                # Include the container's own log tail in the failure: a fixture
                # that starts but whose process exits immediately (e.g. Keycloak
                # refusing an unreadable cert and exiting 2) is otherwise
                # indistinguishable from a hung/slow boot — the probe only ever
                # reports ConnectError. Best-effort: log capture must never turn
                # one failure into a different one.
                logs_tail = ""
                with contextlib.suppress(Exception):
                    logs_tail = f"\n--- {spec.image!r} container logs (tail) ---\n{handle.logs(tail=40)}"
                raise Tier1bFixtureError(
                    f"Fixture {spec.image!r} did not become ready within "
                    f"{spec.ready_timeout_seconds:.0f}s. Last probe failure: {last_reason}{logs_tail}"
                )
        return handle
    except Exception:  # teardown must fire on ANY failure above (pytest.skip's Skipped is an Exception subclass)
        with contextlib.suppress(Exception):
            container.stop()
        raise
