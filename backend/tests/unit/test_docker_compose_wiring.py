"""Verify docker-compose wiring for the alembic_version widening step.

`backend/entrypoint.sh` widens `alembic_version.version_num` to VARCHAR(64)
using `PGPASSWORD=${POSTGRES_PASSWORD:-modulo}`. That password must match the
one configured on the `db` service, otherwise the ALTER silently fails (the
step is swallowed by `|| true`) and a later `alembic upgrade heads` fails on
revision IDs longer than 32 chars.

Any service that builds from `./backend` (and therefore runs entrypoint.sh —
`backend`, `saq-runner`, `saq-system`) must declare `POSTGRES_PASSWORD` so it
sees the same value as the `db` service.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"

# Services built from ./backend run entrypoint.sh and must carry POSTGRES_PASSWORD.
_APP_SERVICES = ("backend", "saq-runner", "saq-system")


@pytest.mark.skipif(
    not _COMPOSE_PATH.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)
def test_compose_file_present():
    assert _COMPOSE_PATH.is_file()


def _load_compose() -> dict:
    with _COMPOSE_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.mark.skipif(
    not _COMPOSE_PATH.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)
def test_app_services_declare_postgres_password():
    compose = _load_compose()
    services = compose.get("services", {})

    db_env = services["db"].get("environment", {})
    db_password = db_env.get("POSTGRES_PASSWORD")
    assert db_password, "db service must declare POSTGRES_PASSWORD"

    for service in _APP_SERVICES:
        assert service in services, f"missing service {service} in compose file"
        env = services[service].get("environment", {})
        assert "POSTGRES_PASSWORD" in env, (
            f"{service} must declare POSTGRES_PASSWORD so entrypoint.sh uses the same password as the db service"
        )
        assert env["POSTGRES_PASSWORD"] == db_password, (
            f"{service} POSTGRES_PASSWORD ({env['POSTGRES_PASSWORD']}) must match the db service value ({db_password})"
        )
