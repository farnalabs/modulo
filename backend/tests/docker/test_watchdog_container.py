"""Container-level proof for the compose health watchdog (docker-marked).

Starts the real image built from ``deploy/watchdog/`` with the same environment
``docker-compose.yml`` passes (the six SMTP variables, empty by default) and
asserts the two claims that only a running container can prove:

* the service **starts and keeps running** with no SMTP credentials at all - the
  "quiet degradation" requirement; and
* it announces that state once at startup, so the default is observable rather
  than silent.

The enabled state is exercised the same way, so a regression in the wiring that
would silently turn alerting off (or on) is caught here rather than by an
operator discovering an undelivered alert.

Selected explicitly, like the rest of ``tests/docker/``: ``uv run pytest
tests/docker/test_watchdog_container.py`` from ``backend/`` (requires a live
Docker engine; the shared autouse fixture skips when there is none).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest
from docker.models.containers import Container

pytestmark = [pytest.mark.docker]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WATCHDOG_DIR = _REPO_ROOT / "deploy" / "watchdog"
_IMAGE_TAG = "modulo-watchdog:test"

# The app's SMTP variables - docker-compose.yml passes all six through, empty
# when the operator has set nothing (`${SMTP_HOST:-}` and friends).
_SMTP_VARS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "EMAIL_FROM",
    "ALERT_EMAIL_TO",
)

_NO_CREDENTIALS = dict.fromkeys(_SMTP_VARS, "")
_CREDENTIALS = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "alerts@example.com",
    "SMTP_PASSWORD": "hunter2",
    "EMAIL_FROM": "alerts@example.com",
    "ALERT_EMAIL_TO": "ops@example.com",
}


@pytest.fixture(scope="module")
def client() -> Iterator[docker.DockerClient]:
    c = docker.from_env()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="module")
def watchdog_image(client: docker.DockerClient) -> str:
    image, _logs = client.images.build(path=str(_WATCHDOG_DIR), tag=_IMAGE_TAG, rm=True)
    return image.id


def _wait_for_log(container: Container, needle: bytes, timeout: float = 90.0) -> bytes:
    """Poll the container's logs for ``needle``, bounded by ``timeout``."""
    deadline = time.monotonic() + timeout
    logs = b""
    while time.monotonic() < deadline:
        container.reload()
        logs = container.logs()
        if needle in logs:
            return logs
        if container.status != "running":
            break
        time.sleep(0.5)
    state = container.attrs.get("State", {})
    raise AssertionError(
        f"did not find {needle!r} (status={container.status} exit={state.get('ExitCode')})\n"
        f"logs:\n{logs.decode(errors='replace')}"
    )


def test_watchdog_starts_and_monitors_without_smtp_credentials(watchdog_image: str):
    """The default, credential-free state must monitor - never crash or exit."""
    client = docker.from_env()
    container = client.containers.run(watchdog_image, detach=True, environment=dict(_NO_CREDENTIALS))
    try:
        # The operator-facing statement that the default state is deliberate.
        _wait_for_log(container, b"watchdog: email alerting DISABLED")
        # Gatus's own confirmation that the email provider was left unconfigured.
        _wait_for_log(container, b"Ignoring provider=email")
        # ...and the reason it is still useful: it probed the backend endpoint.
        _wait_for_log(container, b"[watchdog.executeEndpoint] Monitored")

        container.reload()
        assert container.status == "running", f"watchdog is not running: {container.attrs['State']}"
    finally:
        container.remove(force=True)
        client.close()


def test_watchdog_reports_alerting_enabled_when_smtp_credentials_are_set(watchdog_image: str):
    """Wiring the app's SMTP variables turns alerting on end to end."""
    client = docker.from_env()
    container = client.containers.run(watchdog_image, detach=True, environment=dict(_CREDENTIALS))
    try:
        _wait_for_log(container, b"watchdog: email alerting ENABLED")
        logs = _wait_for_log(container, b"[config.ValidateAlertingConfig] configuredProviders=")
        assert b"configuredProviders=[email]" in logs

        container.reload()
        assert container.status == "running", f"watchdog is not running: {container.attrs['State']}"
    finally:
        container.remove(force=True)
        client.close()
