"""Tier 1b harness: bounded retry of transient container-registry failures.

On 2026-10-24 quay.io answered ``503 Service Unavailable`` for the Keycloak
manifest and the whole auth tier errored on main (run 40076695793), even
though the registry recovered within minutes. A pull failure that the
registry itself declares transient must be retried; every other start
failure (unknown tag, unsupported manifest, disk full) must NOT be, and a
registry that stays down must still fail the job loudly.

The retry mechanics live in ``start_tier1b_container`` (shared with the
sibling suite ``test_testcontainers_harness.py``). This module covers the
registry-specific cases: 5xx status handling, transport errors, a transient
cause reachable only through the exception chain, permanent 4xx failures,
and the end-to-end wiring that must still fail loudly after the budget.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
import requests
import testcontainers.core.docker_client as docker_client_module
import testcontainers.core.generic as generic_module
from docker.errors import APIError

from tests.helpers import testcontainers_harness as harness

_IMAGE = "quay.io/keycloak/keycloak:26.1.0"


def _api_error(status_code: int, message: str = "Server Error") -> APIError:
    """An ``APIError`` shaped like the Docker SDK's (it reads ``response.status_code``)."""
    response = Mock(
        status_code=status_code,
        url="http+docker://localhost/v1.48/images/create?fromImage=quay.io%2Fkeycloak%2Fkeycloak",
        reason=message,
    )
    return APIError(f"{status_code} {message}", response=response)


def _wrapped_connection_error() -> Exception:
    """A pull failure that only reveals its transient cause through ``__cause__``."""
    wrapped = RuntimeError("pull failed")
    wrapped.__cause__ = requests.exceptions.ConnectionError("connection reset")
    return wrapped


class _FakeDockerClient:
    def __init__(self) -> None:
        self.client = Mock()


class _FakeContainer:
    """Records ``start()`` calls and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = outcomes
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        outcome = self._outcomes.pop(0)
        if outcome is not None:
            raise outcome

    def with_env(self, _key: str, _value: str) -> None:
        pass

    def with_command(self, _command: Any) -> None:
        pass

    def with_exposed_ports(self, _port: int) -> None:
        pass

    def stop(self) -> None:
        self.stop_calls += 1


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(harness.time, "sleep", recorded.append)
    return recorded


def _install_fakes(monkeypatch: pytest.MonkeyPatch, container: _FakeContainer) -> None:
    monkeypatch.setattr(docker_client_module, "DockerClient", _FakeDockerClient)
    monkeypatch.setattr(generic_module, "DockerContainer", lambda _image: container)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_api_error(503, "Service Unavailable"), id="registry-503"),
        pytest.param(_api_error(500, "Internal Server Error"), id="registry-500"),
        pytest.param(requests.exceptions.ConnectionError("connection reset"), id="connection-reset"),
        pytest.param(requests.exceptions.ReadTimeout("read timed out"), id="read-timeout"),
        pytest.param(_wrapped_connection_error(), id="wrapped"),
    ],
)
def test_is_transient_start_error_true(exc: Exception) -> None:
    assert harness.is_transient_start_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_api_error(404, "Not Found"), id="unknown-tag"),
        pytest.param(_api_error(400, "Bad Request"), id="bad-manifest"),
        pytest.param(ValueError("no space left on device"), id="unrelated"),
    ],
)
def test_is_transient_start_error_false(exc: Exception) -> None:
    assert harness.is_transient_start_error(exc) is False


def test_retries_a_transient_failure_then_succeeds(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    container = _FakeContainer([_api_error(503), _api_error(503), None])
    _install_fakes(monkeypatch, container)

    harness.start_tier1b_container(harness.ContainerSpec(image=_IMAGE, container_port=None))

    assert container.start_calls == 3
    assert sleeps == [harness.TIER1B_START_BACKOFF_SECONDS * 1, harness.TIER1B_START_BACKOFF_SECONDS * 2]


def test_non_transient_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    container = _FakeContainer([_api_error(404, "Not Found")])
    _install_fakes(monkeypatch, container)

    with pytest.raises(harness.Tier1bFixtureError, match="after 1 attempt"):
        harness.start_tier1b_container(harness.ContainerSpec(image=_IMAGE, container_port=None))

    assert container.start_calls == 1
    assert sleeps == []


def test_exhausted_budget_reports_the_last_error(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    failures = [_api_error(503) for _ in range(harness.TIER1B_START_ATTEMPTS - 1)]
    failures.append(_api_error(503, "Still Unavailable"))
    container = _FakeContainer(list(failures))
    _install_fakes(monkeypatch, container)

    with pytest.raises(harness.Tier1bFixtureError, match="Still Unavailable"):
        harness.start_tier1b_container(harness.ContainerSpec(image=_IMAGE, container_port=None))

    assert container.start_calls == harness.TIER1B_START_ATTEMPTS
    assert sleeps == [
        harness.TIER1B_START_BACKOFF_SECONDS * attempt for attempt in range(1, harness.TIER1B_START_ATTEMPTS)
    ]


def test_start_tier1b_container_retries_and_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """End-to-end wiring: the retry runs, then the fixture failure is loud."""
    container = _FakeContainer([_api_error(503) for _ in range(harness.TIER1B_START_ATTEMPTS)])
    _install_fakes(monkeypatch, container)

    with pytest.raises(harness.Tier1bFixtureError) as excinfo:
        harness.start_tier1b_container(harness.ContainerSpec(image=_IMAGE, container_port=8443))

    assert _IMAGE in str(excinfo.value)
    assert container.start_calls == harness.TIER1B_START_ATTEMPTS
    assert sleeps == [
        harness.TIER1B_START_BACKOFF_SECONDS * attempt for attempt in range(1, harness.TIER1B_START_ATTEMPTS)
    ]
    assert container.stop_calls == harness.TIER1B_START_ATTEMPTS
