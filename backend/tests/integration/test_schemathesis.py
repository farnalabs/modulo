"""Schemathesis-based API fuzzing from the OpenAPI spec.

Runs hypothesis-generated API calls against the FastAPI app statelessly
via the ASGI protocol. Only tests read-only GET endpoints to avoid side
effects.

Run with::

    pytest tests/integration/test_schemathesis.py -x --timeout=120

Requires env vars (minimally SECRET_KEY, FERNET_KEY).  The app's
lifespan is NOT triggered by ``from_asgi``, so DB connectivity is
not required for the schema load — individual endpoints may still
fail with 5xx if they depend on a database, and that is expected.
"""

import schemathesis
from hypothesis import HealthCheck, settings

from modulo.api.main import app

schema = schemathesis.from_asgi("/openapi.json", app)


@schema.parametrize(endpoint=r"^/api/v1/(pipelines|schemas|library|connectors|model-backends)(\?.*)?$")
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_api_fuzz_get_endpoints(case):
    """All read-only GET endpoints must respond without 500 errors."""
    case.call_and_validate()
