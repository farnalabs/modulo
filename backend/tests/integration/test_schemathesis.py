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
filtered = schema.include(
    method="GET", path_regex=r"^/api/v1/(pipelines|schemas|library|connectors|model-backends)(\?.*)?$"
)


@filtered.parametrize()
@settings(max_examples=3, suppress_health_check=[HealthCheck.too_slow])
# 3 examples per endpoint (not 10): each schemathesis example spins the full app
# lifespan (migrations + seeding) via from_asgi, so higher counts make the
# deploy gate impractically slow for marginal extra coverage on these simple
# auth-guarded GET endpoints.
def test_api_fuzz_get_endpoints(case):
    """All read-only GET endpoints must respond without 500 errors."""
    case.call_and_validate()
