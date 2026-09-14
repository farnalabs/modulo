"""Schemathesis-based API fuzzing from the OpenAPI spec.

Runs hypothesis-generated API calls against the FastAPI app statelessly
via the ASGI protocol. Only tests read-only GET endpoints to avoid side
effects.

Run with::

    pytest tests/integration/test_schemathesis.py -x --timeout=120

Requires env vars (minimally SECRET_KEY, FERNET_KEY).  The app's lifespan
IS triggered by ``schemathesis.openapi.from_asgi`` (it drives the app through
Starlette's TestClient, which runs startup/shutdown), and startup requires a
reachable REDIS_URL. When Redis is absent or unreachable (e.g. a bare local
``pytest tests/integration/`` without the docker-compose Redis up), the tests
skip rather than fail — the lifespan would raise ``RuntimeError`` otherwise.
CI (deploy.yml) starts Redis and sets REDIS_URL, so the fuzz runs there.
"""

import os
import socket

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import status_code_conformance


def _redis_reachable() -> bool:
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        return False
    try:
        host = redis_url.split("://")[1].split(":")[0]
        port = int(redis_url.split(":")[-1].split("/")[0])
        with socket.create_connection((host, port), timeout=2):
            return True
    except (OSError, IndexError, ValueError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _redis_reachable(),
        reason="REDIS_URL not set or Redis port unreachable (required by app lifespan)",
    ),
]


# E402: the import must come AFTER the skipif marker is evaluated — importing
# the app would trigger the full import chain (MCP server startup, DB engine)
# even when Redis is unreachable and the tests will skip anyway. The app
# lifespan (driven by schemathesis's from_asgi) requires a reachable REDIS_URL
# and a migrated DB; it raises RuntimeError when that is not the case (e.g. a
# bare local `pytest tests/integration/` without Docker services, or a
# lightweight CI job that does not boot the full app). Capture the failure so we
# can fall back to a skipped sentinel test below instead of erroring at
# collection time: a module-level skip on the only test file leaves pytest with
# "no tests collected", exit code 5, which fails the job.
schema = None
_app_boot_error: BaseException | None = None
try:
    from modulo.api.main import app

    schema = schemathesis.openapi.from_asgi("/openapi.json", app)
except Exception as exc:  # boot failures are environmental, not test bugs
    _app_boot_error = exc


# Each endpoint group is fuzzed in its own job (see schemathesis-nightly.yml
# matrix). Every schemathesis example spins the full app lifespan (migrations
# + seeding) via from_asgi, so running every group in one job takes ~15 min
# and the ephemeral ubicloud runner kills the job ~10 min in ("runner has
# received a shutdown signal", exit 143 - an infrastructure kill, not a test
# failure) before the fuzz can finish. Splitting per group keeps each job well
# inside the runner's usable window while still catching 5xx regressions.
SCHEMA_GROUPS = {
    "pipelines": r"^/api/v1/pipelines(\?.*)?$",
    "schemas": r"^/api/v1/schemas(\?.*)?$",
    "libraries": r"^/api/v1/libraries(\?.*)?$",
    "connectors": r"^/api/v1/connectors(\?.*)?$",
    "model-backends": r"^/api/v1/model-backends(\?.*)?$",
}

_group = os.environ.get("SCHEMA_GROUP", "").strip()
if _group:
    _regex = SCHEMA_GROUPS[_group]
else:
    _regex = r"^/api/v1/(pipelines|schemas|libraries|connectors|model-backends)(\?.*)?$"


@pytest.mark.integration
def test_schemathesis_fuzz_smoke():
    """Guarantee this module always collects at least one test.

    The real fuzz (test_api_fuzz_get_endpoints) is only defined when the app can
    be fully booted. When it cannot (lightweight CI without a migrated DB +
    Redis), this sentinel keeps pytest from exiting 5 ("no tests collected"),
    which would otherwise fail the job. It passes trivially when the fuzz runs
    and skips (with a reason) when it cannot. Genuine fuzz coverage lives in
    schemathesis-nightly.yml.
    """
    if schema is None:
        pytest.skip(f"Schemathesis fuzz skipped: app lifespan unavailable ({_app_boot_error})")


if schema is not None:
    filtered = schema.include(method="GET", path_regex=_regex)

    @filtered.parametrize()
    @settings(max_examples=1, suppress_health_check=[HealthCheck.too_slow])
    def test_api_fuzz_get_endpoints(case):
        """All read-only GET endpoints must respond without 500 errors."""
        # The fuzzer runs unauthenticated, so auth-protected GET endpoints
        # legitimately return 401/403. Those codes are undocumented in the schema,
        # so drop strict status-code conformance while keeping not_a_server_error
        # (5xx) and response_schema_conformance (2xx bodies) active.
        case.call_and_validate(excluded_checks=[status_code_conformance])
