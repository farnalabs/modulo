"""Tier 1b harness: bounded retry of transient container-registry failures.

On 2026-10-24 quay.io answered ``503 Service Unavailable`` for the Keycloak
manifest and the whole auth tier errored on main (run 40076695793), even
though the registry recovered within minutes. A pull failure that the
registry itself declares transient must be retried; every other start
failure (unknown tag, unsupported manifest, disk full) must NOT be, and a
registry that stays down must still fail the job loudly.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests
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


def _wrapped_connection_error() -> BaseException:
    """A pull failure that only reveals its transient cause through ``__cause__``."""
    wrapped = RuntimeError("pull failed")
    wrapped.__cause__ = requests.exceptions.ConnectionError("connection reset")
    return wrapped


class _FakeContainer:
    """Records ``start()`` calls and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = outcomes
        self.start_calls = 0
        self.stopped = False

    def start(self) -> None:
        self.start_calls += 1
        outcome = self._outcomes.pop(0)
        if outcome is not None:
            raise outcome

    def with_env(self, key: str, value: str) -> None:
        pass

    def with_command(self, command: list[str]) -> None:
        pass

    def with_exposed_ports(self, port: int) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(harness.time, "sleep", recorded.append)
    return recorded


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
def test_is_transient_registry_error_true(exc: BaseException) -> None:
    assert harness.is_transient_registry_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_api_error(404, "Not Found"), id="unknown-tag"),
        pytest.param(_api_error(400, "Bad Request"), id="bad-manifest"),
        pytest.param(ValueError("no space left on device"), id="unrelated"),
    ],
)
def test_is_transient_registry_error_false(exc: BaseException) -> None:
    assert harness.is_transient_registry_error(exc) is False


def test_retries_a_transient_failure_then_succeeds(sleeps: list[float]) -> None:
    container = _FakeContainer([_api_error(503), _api_error(503), None])

    harness.start_container_with_transient_pull_retry(container, _IMAGE)

    assert container.start_calls == 3
    assert sleeps == list(harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS[:2])


def test_non_transient_failure_is_not_retried(sleeps: list[float]) -> None:
    container = _FakeContainer([_api_error(404, "Not Found")])

    with pytest.raises(APIError):
        harness.start_container_with_transient_pull_retry(container, _IMAGE)

    assert container.start_calls == 1
    assert sleeps == []


def test_exhausted_budget_reraises_the_last_error(sleeps: list[float]) -> None:
    failures = [_api_error(503) for _ in harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS]
    failures.append(_api_error(503, "Still Unavailable"))
    container = _FakeContainer(list(failures))

    with pytest.raises(APIError, match="Still Unavailable"):
        harness.start_container_with_transient_pull_retry(container, _IMAGE)

    assert container.start_calls == len(harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS)


def test_start_tier1b_container_retries_and_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """End-to-end wiring: the retry runs, then the fixture failure is loud."""
    import testcontainers.core.docker_client as docker_client_module
    import testcontainers.core.generic as generic_module

    container = _FakeContainer([_api_error(503) for _ in harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS])

    class _FakeClient:
        def __init__(self) -> None:
            self.client = Mock()

    monkeypatch.setattr(docker_client_module, "DockerClient", _FakeClient)
    monkeypatch.setattr(generic_module, "DockerContainer", lambda image: container)

    with pytest.raises(harness.Tier1bFixtureError) as excinfo:
        harness.start_tier1b_container(harness.ContainerSpec(image=_IMAGE, container_port=8443))

    assert _IMAGE in str(excinfo.value)
    assert container.start_calls == len(harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(harness.TRANSIENT_PULL_RETRY_DELAYS_SECONDS)
    assert container.stopped is True
