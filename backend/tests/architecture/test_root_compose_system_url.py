"""FAR-1519 item 1: the root docker-compose.yml must wire the system DB URL.

``dispatcher_reconcile`` builds its session factory from
``MODULO_SYSTEM_DATABASE_URL`` and FAILS CLOSED without it
(``core/cron_helpers.py::_get_system_engine`` raises BEFORE the try block that
persists the heartbeat) — so no stats are ever written, ``/healthz/ready``
reports ``dispatcher_reconcile has never run`` as ``unavailable``, and that
tier GATES readiness with HTTP 503 (FAR-199): the API answers while the
deployment never becomes ready.

* ``saq-system`` runs the cron and needs the URL to authenticate.
* ``backend`` needs it too: the entrypoint's ``bootstrap_role`` reconciles
  the ``modulo_system`` password from this URL when it creates/alters the
  role, so a backend that creates the role without it would leave a random
  placeholder the worker's URL could never authenticate against (FAR-1519: an
  unconfigured URL no longer re-randomises an existing credential - it only
  creates a *missing* role with a placeholder); the backend's own cross-org
  reads (pre-auth SSO provider lookup, Fernet key rotation) use the same URL.

Structural guard: deleting the variable from either service fails here,
instead of surfacing as a permanently-red readiness endpoint on the next
``docker compose up``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE = _REPO_ROOT / "docker-compose.yml"
#: saq-runner deliberately absent — the runs-queue functions never touch the
#: system engine (only the system-queue crons and the backend do).
_SERVICES_REQUIRING_SYSTEM_URL = ("backend", "saq-system")


def _service_environment(service: str) -> dict[str, object]:
    if not _COMPOSE.exists():
        pytest.skip(f"Compose file not present: {_COMPOSE.relative_to(_REPO_ROOT)}")
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    environment = compose.get("services", {}).get(service, {}).get("environment", {})
    assert isinstance(environment, dict), f"services.{service}.environment must be a mapping"
    return environment


@pytest.mark.parametrize("service", _SERVICES_REQUIRING_SYSTEM_URL)
def test_service_wires_the_system_database_url(service: str) -> None:
    environment = _service_environment(service)
    value = environment.get("MODULO_SYSTEM_DATABASE_URL")
    assert value, (
        f"services.{service} in docker-compose.yml does not pass MODULO_SYSTEM_DATABASE_URL: "
        "the system worker's dispatcher_reconcile cron fails closed without it, so /healthz/ready "
        "never reports ready (the API + workers can all be up and readiness stays 503) - see "
        "core/cron_helpers.py::_get_system_engine"
    )
    assert isinstance(value, str)
    assert value.startswith("postgresql+asyncpg://"), (
        f"MODULO_SYSTEM_DATABASE_URL must use the asyncpg dialect the app's engine builder expects, got {value!r}"
    )
    assert "modulo_system:" in value, (
        f"MODULO_SYSTEM_DATABASE_URL must authenticate as the modulo_system (BYPASSRLS) role, got {value!r} - "
        "the app role is NOBYPASSRLS and the cron refuses to fall back to it"
    )
    assert "${POSTGRES_PASSWORD" in value, (
        "MODULO_SYSTEM_DATABASE_URL must interpolate the same POSTGRES_PASSWORD the other URLs use so "
        f"the role password and the URL can never disagree, got {value!r}"
    )
