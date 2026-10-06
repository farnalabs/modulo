"""Architecture test: the all-in-one image runs BOTH SAQ queue workers.

FAR-1509. ``deploy/compose/docker-compose.prod.yml`` runs a single image, and
that image's supervisord used to start only ``backend`` (uvicorn) and
``nginx``. The consequence was not cosmetic: the deployment's own readiness
probe reported

    saq_workers: unavailable - no live saq workers on queue(s) ['runs', 'system']

and therefore **no pipeline run, cron trigger or polling trigger could ever
execute** in the production compose deployment - the product's core function
was inert while the API reported healthy. The root ``docker-compose.yml``
carried the two worker services all along; the all-in-one artifact did not.

This test pins the fix structurally. It fails on the pre-fix file three ways:

1. ``supervisord.conf`` declares no SAQ program at all;
2. the declared commands differ from the root compose's ``saq-runner`` /
   ``saq-system`` commands, so the two deployments would drift;
3. the programs are declared but the Dockerfile never copies
   ``supervisord.conf`` into the image, so they could not ship.

Runtime proof (stack up, ``saq_workers: ok``, a trigger producing a run) is
outside what a unit test can do; this pins the shape, the e2e run pins the
behaviour.

Run from ``backend/``:
``uv run pytest tests/architecture/test_all_in_one_supervisor_workers.py``.
"""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path

import pytest
import yaml

# tests/architecture/ -> tests/ -> backend/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SUPERVISORD_CONF = _REPO_ROOT / "deploy" / "supervisor" / "supervisord.conf"
_ROOT_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_ALL_IN_ONE_DOCKERFILE = _REPO_ROOT / "deploy" / "docker" / "Dockerfile.all-in-one"

#: supervisord program section -> the root compose service whose command it
#: must reproduce verbatim. The root compose is the reference deployment for
#: "how Modulo's workers run"; the all-in-one image must run the same thing.
_WORKER_PROGRAMS: dict[str, str] = {
    "program:saq-runs": "saq-runner",
    "program:saq-system": "saq-system",
}

#: Where every program's output must land - the same directory the existing
#: backend / nginx programs use, so an operator reads worker logs in the
#: place they already read the others.
_LOG_DIR = "/var/log/supervisor"

#: The single application service in the production compose - the one the
#: supervisord programs run inside.
_APP_SERVICE = "modulo"


def _supervisord() -> configparser.ConfigParser:
    if not _SUPERVISORD_CONF.exists():
        pytest.skip(f"supervisord config not present: {_SUPERVISORD_CONF.relative_to(_REPO_ROOT)}")
    parser = configparser.ConfigParser(interpolation=None)
    # supervisord accepts `%(ENV_PATH)s` style expansions in `environment=`,
    # which ConfigParser would otherwise try to resolve itself.
    parser.read(_SUPERVISORD_CONF, encoding="utf-8")
    return parser


def _root_compose_service_command(service: str) -> list[str]:
    compose = yaml.safe_load(_ROOT_COMPOSE.read_text(encoding="utf-8"))
    command = compose["services"][service]["command"]
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def test_each_worker_program_matches_the_root_compose_command():
    """The image must run exactly what the reference deployment runs."""
    parser = _supervisord()
    for section, service in _WORKER_PROGRAMS.items():
        assert parser.has_section(section), (
            f"{_SUPERVISORD_CONF.relative_to(_REPO_ROOT)} has no [{section}] - without it the "
            f"all-in-one image never runs the `{service}` worker, so nothing executes in the "
            "production compose deployment: no pipeline run, no cron trigger, no polling trigger "
            "(FAR-1509). /healthz/ready reports saq_workers unavailable on that queue."
        )
        actual = shlex.split(parser.get(section, "command"))
        expected = _root_compose_service_command(service)
        assert actual == expected, (
            f"[{section}].command drifted from docker-compose.yml's `{service}` command:\n"
            f"  supervisord: {actual}\n  compose:     {expected}\n"
            "The two deployments must run the same worker, or one of them is wrong."
        )


def test_each_worker_program_autostarts_and_logs_where_an_operator_can_read_them():
    """Workers must start with the container and write logs like the others."""
    parser = _supervisord()
    for section in _WORKER_PROGRAMS:
        assert parser.has_section(section)
        assert parser.get(section, "autostart").strip().lower() == "true", (
            f"[{section}] must autostart - a worker an operator has to remember to start is a "
            "worker the deployment will not have"
        )
        assert parser.get(section, "directory").strip() == "/app", (
            f"[{section}] must run in /app (the venv, alembic.ini and the source tree live there, "
            "exactly as for [program:backend])"
        )
        for key in ("stdout_logfile", "stderr_logfile"):
            value = parser.get(section, key).strip()
            assert value.startswith(f"{_LOG_DIR}/"), (
                f"[{section}].{key} = {value!r} - worker output must land in {_LOG_DIR} beside the "
                "backend and nginx logs, or an operator has nothing to read when a run never starts"
            )
        # PATH must reach the venv, or `python` resolves to the system
        # interpreter and the worker dies on import - the same reason
        # [program:backend] declares it.
        environment = parser.get(section, "environment")
        assert "/app/.venv/bin" in environment, (
            f"[{section}].environment must put /app/.venv/bin on PATH (got {environment!r})"
        )


def test_supervisord_conf_is_actually_shipped_by_the_all_in_one_dockerfile():
    """A program nobody copies into the image cannot run (FAR-1509)."""
    dockerfile = _ALL_IN_ONE_DOCKERFILE.read_text(encoding="utf-8")
    copied = [
        line
        for line in dockerfile.splitlines()
        if line.startswith("COPY") and "deploy/supervisor/supervisord.conf" in line
    ]
    assert copied, (
        f"{_ALL_IN_ONE_DOCKERFILE.relative_to(_REPO_ROOT)} never copies deploy/supervisor/"
        "supervisord.conf, so whatever it declares would not ship in the image"
    )


def test_the_workers_are_not_a_compose_side_service():
    """The all-in-one artifact stays all-in-one: workers live in the image.

    NOT a product rule - separate worker services are a legitimate way to run
    Modulo (the root compose does it, for independent scaling). It is a
    documentation-of-intent check for THIS artifact: if the workers move to
    compose services, ``deploy/compose/docker-compose.prod.yml`` must gain the
    full application environment block for them, and the drift risk that
    produced FAR-1506 returns. The change must be deliberate, not accidental.
    """
    compose_path = _REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml"
    if not compose_path.exists():
        pytest.skip("prod compose not present (running outside repo checkout)")
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose.get("services", {})
    worker_services = [name for name in services if "saq" in name.lower() or "worker" in name.lower()]

    assert not worker_services, (
        f"deploy/compose/docker-compose.prod.yml defines worker service(s) {worker_services} while "
        "the image's supervisord also runs them - that would double-run every queue. Pick one "
        "placement: if the workers move OUT of the image, remove the supervisord programs and "
        "update docs/deployment.md + this test deliberately."
    )


def test_the_compose_wires_the_system_database_url_the_system_worker_requires():
    """``dispatcher_reconcile`` - and therefore readiness - needs it (FAR-1509).

    The system worker's ``dispatcher_reconcile`` builds its session factory
    from ``MODULO_SYSTEM_DATABASE_URL`` and FAILS CLOSED when it is unset
    (``core/cron_helpers.py::_get_system_engine`` raises ``RuntimeError``), so
    the cron never runs. ``/healthz/ready`` reports that cron ``unavailable``
    and **gates** on it (FAR-199): the backend never becomes ready, even
    though the API and both workers are up.

    The prod compose never needed this before FAR-1509 because it never ran
    the system worker at all - observed on a live bring-up, not inferred: the
    worker logged ``MODULO_SYSTEM_DATABASE_URL is not set: cross-org system
    crons require the modulo_system role`` on every tick and readiness stayed
    503 with ``dispatcher_reconcile has never run``. The Helm chart wires the
    same URL for the same reason (``deploy/helm/.../_helpers.tpl``).
    """
    compose_path = _REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml"
    if not compose_path.exists():
        pytest.skip("prod compose not present (running outside repo checkout)")
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    environment = compose.get("services", {}).get(_APP_SERVICE, {}).get("environment", {})
    assert isinstance(environment, dict), f"services.{_APP_SERVICE}.environment must be a mapping"

    value = environment.get("MODULO_SYSTEM_DATABASE_URL")
    assert value, (
        f"deploy/compose/docker-compose.prod.yml does not pass MODULO_SYSTEM_DATABASE_URL to "
        f"{_APP_SERVICE}: the system worker's dispatcher_reconcile cron fails closed without it, "
        "so /healthz/ready never reports ready (the API + workers can all be up and the deployment is "
        "still permanently 503) - see core/cron_helpers.py::_get_system_engine"
    )
    assert value.startswith("postgresql+asyncpg://"), (
        f"MODULO_SYSTEM_DATABASE_URL must use the asyncpg dialect the app's engine builder expects, got {value!r}"
    )
    assert "modulo_system:" in value, (
        f"MODULO_SYSTEM_DATABASE_URL must authenticate as the modulo_system (BYPASSRLS) role, got {value!r} - "
        "the app role is NOBYPASSRLS and the cron refuses to fall back to it"
    )
    # Same password as the app role: bootstrap_role parses the password out of
    # this URL when it creates/alters the role, so both sides must reference the
    # ONE operator secret (MODULO_DB_PASSWORD) rather than two literals drifting.
    assert "${MODULO_DB_PASSWORD" in value, (
        f"MODULO_SYSTEM_DATABASE_URL must interpolate ${{MODULO_DB_PASSWORD}} so the role password and the "
        f"URL agree, got {value!r}"
    )
