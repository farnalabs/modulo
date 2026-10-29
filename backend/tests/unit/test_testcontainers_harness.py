"""Retry semantics of the Tier 1b testcontainers harness.

A transient registry failure (anonymous-pull rate limit, 5xx, dropped
connection) must not fail a per-PR job whose product code is untouched, so
``start_tier1b_container`` retries only the container start that pulls the
image. A permanent error still fails loudly on the first attempt.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import requests
import testcontainers.core.docker_client
import testcontainers.core.generic
from docker.errors import APIError, ImageNotFound

from tests.helpers import testcontainers_harness as harness


class _FakeDockerClient:
    def __init__(self, ping_error: Exception | None = None) -> None:
        self.client = self
        self._ping_error = ping_error

    def ping(self) -> bool:
        if self._ping_error is not None:
            raise self._ping_error
        return True


class _FakeContainer:
    def __init__(self, image: str, failures: list[Exception] | None = None) -> None:
        self.image = image
        self.failures = list(failures or [])
        self.start_calls = 0
        self.stop_calls = 0

    def with_env(self, _key: str, _value: str) -> _FakeContainer:
        return self

    def with_command(self, _command: Any) -> _FakeContainer:
        return self

    def with_volume_mapping(self, _host_dir: str, _mount_path: str, _mode: str) -> _FakeContainer:
        return self

    def with_exposed_ports(self, _port: int) -> _FakeContainer:
        return self

    def start(self) -> None:
        self.start_calls += 1
        if self.failures:
            raise self.failures.pop(0)

    def stop(self) -> None:
        self.stop_calls += 1


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: list[Exception] | None = None,
    ping_error: Exception | None = None,
) -> _FakeContainer:
    fake = _FakeContainer("example/image:1", failures)
    monkeypatch.setattr(testcontainers.core.generic, "DockerContainer", lambda _image: fake)
    monkeypatch.setattr(testcontainers.core.docker_client, "DockerClient", lambda: _FakeDockerClient(ping_error))
    monkeypatch.setattr(harness.time, "sleep", lambda _seconds: None)
    return fake


def test_start_retries_a_transient_image_pull_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fakes(
        monkeypatch,
        failures=[ImageNotFound("No such image: example/image:1"), requests.exceptions.ConnectionError("reset")],
    )

    handle = harness.start_tier1b_container(harness.ContainerSpec(image="example/image:1", container_port=None))

    assert fake.start_calls == harness.TIER1B_START_ATTEMPTS
    assert handle.host_port is None


def test_start_fails_on_the_first_permanent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fakes(monkeypatch, failures=[ValueError("bad volume mapping")])

    with pytest.raises(harness.Tier1bFixtureError, match="after 1 attempt"):
        harness.start_tier1b_container(harness.ContainerSpec(image="example/image:1", container_port=None))

    assert fake.start_calls == 1


def test_start_fails_loudly_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fakes(monkeypatch, failures=[ImageNotFound("gone") for _ in range(harness.TIER1B_START_ATTEMPTS)])

    with pytest.raises(harness.Tier1bFixtureError, match="after 3 attempt"):
        harness.start_tier1b_container(harness.ContainerSpec(image="example/image:1", container_port=None))

    assert fake.start_calls == harness.TIER1B_START_ATTEMPTS


def test_no_docker_daemon_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fakes(monkeypatch, ping_error=RuntimeError("no daemon"))

    with pytest.raises(harness.Tier1bFixtureError, match="no daemon"):
        harness.start_tier1b_container(harness.ContainerSpec(image="example/image:1", container_port=None))

    assert fake.start_calls == 0


def _api_error(status: int) -> APIError:
    response = requests.Response()
    response.status_code = status
    return APIError("api error", response=response)


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ImageNotFound("missing"), True),
        (requests.exceptions.ConnectionError("reset"), True),
        (requests.exceptions.Timeout("slow"), True),
        (_api_error(429), True),
        (_api_error(500), True),
        (_api_error(400), False),
        (ValueError("nope"), False),
    ],
)
def test_is_transient_start_error(exc: Exception, transient: bool) -> None:
    assert harness.is_transient_start_error(exc) is transient


class _ExecResult:
    def __init__(self, output: bytes, exit_code: int = 0) -> None:
        self.output = output
        self.exit_code = exit_code


class _FakeExecContainer:
    """Docker container double whose exec either returns, raises or blocks."""

    def __init__(
        self,
        *,
        result: _ExecResult | None = None,
        error: Exception | None = None,
        block_on: threading.Event | None = None,
    ) -> None:
        self.result = result or _ExecResult(b"")
        self.error = error
        self.block_on = block_on
        self.exec_calls = 0

    def exec(self, _config: Any) -> _ExecResult:
        self.exec_calls += 1
        if self.block_on is not None:
            self.block_on.wait()
        if self.error is not None:
            raise self.error
        return self.result


def _handle_with(container: _FakeExecContainer) -> harness.ContainerHandle:
    return harness.ContainerHandle(
        spec=harness.ContainerSpec(image="example/image:1", container_port=None),
        host_port=None,
        _docker_container=container,
    )


def test_exec_returns_output_within_the_deadline() -> None:
    handle = _handle_with(_FakeExecContainer(result=_ExecResult(b"qwen2:0.5b\n")))

    assert handle.exec(["ollama", "list"], timeout_seconds=5) == "qwen2:0.5b\n"


def test_exec_times_out_as_a_fixture_error_so_the_fixture_can_skip() -> None:
    release = threading.Event()
    handle = _handle_with(_FakeExecContainer(block_on=release))
    try:
        with pytest.raises(harness.Tier1bFixtureError, match="did not finish within"):
            handle.exec(["ollama", "pull", "qwen2:0.5b"], timeout_seconds=0.05)
    finally:
        release.set()


def test_exec_reraises_a_container_error_from_the_worker() -> None:
    handle = _handle_with(_FakeExecContainer(error=RuntimeError("daemon gone")))

    with pytest.raises(RuntimeError, match="daemon gone"):
        handle.exec(["ollama", "list"], timeout_seconds=5)


def test_exec_reports_a_non_zero_exit() -> None:
    handle = _handle_with(_FakeExecContainer(result=_ExecResult(b"403 Forbidden", exit_code=1)))

    with pytest.raises(harness.Tier1bFixtureError, match="exit 1"):
        handle.exec(["ollama", "pull", "qwen2:0.5b"], timeout_seconds=5)
