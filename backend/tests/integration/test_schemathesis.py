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


pytestmark = pytest.mark.skipif(
    not _redis_reachable(),
    reason="REDIS_URL not set or Redis port unreachable (required by app lifespan)",
)


# E402: the import must come AFTER the skipif marker is evaluated — importing
# the app would trigger the full import chain (MCP server startup, DB engine)
# even when Redis is unreachable and the tests will skip anyway.
from modulo.api.main import app  # noqa: E402

schema = schemathesis.openapi.from_asgi("/openapi.json", app)

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
