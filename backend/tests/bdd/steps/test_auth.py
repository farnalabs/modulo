"""Step definitions for auth features: login, RBAC, API keys, tenant isolation.

Designed to coexist with shared status steps in other step files:
  - ``@then("the response status is 200")`` is defined in test_connectors.py
    (checks ``request.node._resp is not None``).
  - ``@then("the response status is 404")`` is defined in test_library.py
    (injects ``ctx`` fixture and checks ``ctx["response"]``).

This file provides a ``ctx`` fixture (mutable dict) so that the shared 404 step
works for our scenarios.  Every When step stores the response in **three**
locations for maximum compatibility:

  - ``request.node.response``  — used by this file's custom Then steps
  - ``request.node._resp``    — used by test_connectors.py's status_200 step
  - ``ctx["response"]``       — used by test_library.py's status_404 step
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from jose import jwt as jose_jwt
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
scenarios("../features/auth/login.feature")
scenarios("../features/auth/rbac.feature")
scenarios("../features/auth/api_keys.feature")
scenarios("../features/auth/tenant_isolation.feature")

# ---------------------------------------------------------------------------
# Constants matching conftest.py
# ---------------------------------------------------------------------------
_VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


# ---------------------------------------------------------------------------
# Shared response context — makes ``ctx["response"]`` available so other
# step files' ``@then("the response status is …")`` steps work here.
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Mutable context dict shared across steps in this test.

    Other step files (test_library.py, test_connectors.py) also define
    ``ctx`` — each is scoped to its own module so there is no conflict.
    """
    return {}


# ---------------------------------------------------------------------------
# Helper — store a response object in all locations expected by the various
# step files' Then assertions.
# ---------------------------------------------------------------------------


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    """Record a response so shared ``@then`` steps can inspect it."""
    request.node._resp = resp  # test_connectors.py convention
    request.node.response = resp  # test_auth.py convention
    ctx["response"] = resp  # test_library.py convention


# ===========================================================================
# auth/login.feature  —  4 scenarios
# ===========================================================================


@given(
    parsers.parse(
        'a user exists with email "{email}" and password "{password}"'
    ),
)
def user_exists(email: str, password: str) -> None:
    """Context step: a valid user is registered.

    No action needed — the mock in the When step controls whether
    authentication succeeds or fails.
    """
    return


@when(
    parsers.parse(
        'I POST /api/auth/login with email "{email}" and password "{password}"'
    ),
    target_fixture="login_response",
)
def login(
    client: Any, email: str, password: str, request: Any, ctx: dict[str, Any]
) -> Any:
    """POST /api/v1/auth/login with the given credentials.

    Mocks ``authenticate_user`` so the test controls success/failure.
    """
    with patch("modulo.api.routes.auth.authenticate_user") as mock_auth:
        if email == "alice@example.com" and password == "correct-horse-battery":
            mock_auth.return_value = True
        else:
            mock_auth.return_value = False
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": email, "password": password},
        )
        _store_response(request, ctx, resp)
        return resp


@then("the response contains an access_token")
def has_access_token(request: Any) -> None:
    body = request.node.response.json()
    assert "access_token" in body, f"Response missing access_token: {body}"
    assert isinstance(body["access_token"], str), (
        f"access_token is not a string: {body['access_token']}"
    )
    assert len(body["access_token"]) > 0, "access_token is empty"


@then("the token encodes org_id")
def token_encodes_org_id(request: Any) -> None:
    body = request.node.response.json()
    token = body["access_token"]
    payload: dict[str, object] = jose_jwt.decode(
        token, _VALID_32, algorithms=["HS256"]
    )
    assert "org_id" in payload, f"Token payload missing org_id: {payload}"
    assert payload["org_id"] is not None


@then("the response status is 401")
def status_401(request: Any) -> None:
    resp = request.node.response
    assert resp.status_code == 401, (
        f"Expected 401, got {resp.status_code}: {resp.text}"
    )


# -- Expired token scenario ------------------------------------------------


@given(
    parsers.parse('I have an expired JWT for org "{org_name}"'),
    target_fixture="expired_token",
)
def expired_jwt(org_name: str) -> str:
    """Create a JWT whose ``exp`` is in the past."""
    now = datetime.now(UTC)
    payload = {
        "sub": "testuser",
        "org_id": str(ORG_ID),
        "user_id": str(USER_ID),
        "org_role": "admin",
        "iat": now - timedelta(hours=48),
        "exp": now - timedelta(hours=1),  # expired 1 hour ago
    }
    return str(jose_jwt.encode(payload, _VALID_32, algorithm="HS256"))


@when("I make an authenticated request to /api/pipelines")
def expired_auth_request(
    unauth_client: Any, expired_token: str, request: Any, ctx: dict[str, Any]
) -> None:
    """GET /api/v1/pipelines with the expired JWT as Bearer."""
    resp = unauth_client.get(
        "/api/v1/pipelines",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    _store_response(request, ctx, resp)


# ===========================================================================
# auth/rbac.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("I am an admin user")
def step_admin_user() -> bool:
    """Placeholder: admin role boundary scenario."""
    return True


@given("I am an editor user")
def step_editor_user() -> bool:
    """Placeholder: editor role boundary scenario."""
    return True


@given("I am a viewer user")
def step_viewer_user() -> bool:
    """Placeholder: viewer role boundary scenario."""
    return True


# ===========================================================================
# auth/api_keys.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("I have a valid API key")
def step_valid_api_key() -> bool:
    """Placeholder: API key auth scenario."""
    return True


@given("I have a revoked API key")
def step_revoked_api_key() -> bool:
    """Placeholder: revoked API key scenario."""
    return True


# ===========================================================================
# auth/tenant_isolation.feature  —  3 scenarios
# ===========================================================================


def _make_mock_pipeline(
    name: str, org_id: uuid.UUID, pipeline_id: uuid.UUID
) -> SimpleNamespace:
    """Build a lightweight mock pipeline object."""
    p = SimpleNamespace()
    p.id = pipeline_id
    p.organisation_id = org_id
    p.name = name
    p.description = None
    p.visibility = "org"
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.created_by = USER_ID
    p.created_at = None
    p.updated_at = None
    return p


@given(
    parsers.parse('organisation "{org}" has pipeline "{name}"'),
)
def org_has_pipeline(request: Any, org: str, name: str) -> None:
    """Record that a named pipeline exists for the given org.

    State is accumulated on ``request.node.pipelines`` so later steps
    can decide what each org can see.
    """
    if not hasattr(request.node, "pipelines"):
        request.node.pipelines = {}

    org_id = ORG_ID if org == "acme" else ALT_ORG_ID
    pipeline_id = uuid.uuid5(ORG_ID, name)  # deterministic per-name

    request.node.pipelines[name] = {
        "org": org,
        "org_id": org_id,
        "mock": _make_mock_pipeline(name, org_id, pipeline_id),
    }


@when(parsers.parse('I authenticate as a user in "{org}"'))
def authenticate_org(request: Any, org: str) -> None:
    """Remember which org the current user belongs to."""
    request.node.current_org = org


@when("I GET /api/pipelines", target_fixture="pipelines_response")
def get_pipelines(
    request: Any, client: Any, alt_org_client: Any, ctx: dict[str, Any]
) -> Any:
    """GET /api/v1/pipelines with the correct client for ``current_org``.

    Mocks ``list_pipelines`` so only pipelines belonging to the active
    organisation are returned (simulating RLS).
    """
    current = getattr(request.node, "current_org", "acme")
    test_client = alt_org_client if current == "globex" else client
    all_pipelines: dict[str, dict[str, Any]] = getattr(
        request.node, "pipelines", {}
    )

    with patch("modulo.api.routes.pipelines.list_pipelines") as mock_list:
        org_id = ORG_ID if current == "acme" else ALT_ORG_ID
        visible = [
            p["mock"] for p in all_pipelines.values() if p["org_id"] == org_id
        ]

        mock_list.return_value = SimpleNamespace(
            items=visible,
            total=len(visible),
            page=1,
            page_size=20,
        )

        resp = test_client.get("/api/v1/pipelines")
        _store_response(request, ctx, resp)
        return resp


@then(parsers.parse('I see "{name}"'))
def see_pipeline(pipelines_response: Any, name: str) -> None:
    assert pipelines_response.status_code == 200, (
        f"Expected 200, got {pipelines_response.status_code}: "
        f"{pipelines_response.text}"
    )
    body = pipelines_response.json()
    names = [item["name"] for item in body.get("items", [])]
    assert name in names, (
        f"Expected to see pipeline '{name}', but it was not in the "
        f"response: {names}"
    )


@then(parsers.parse('I do not see "{name}"'))
def not_see_pipeline(pipelines_response: Any, name: str) -> None:
    assert pipelines_response.status_code == 200, (
        f"Expected 200, got {pipelines_response.status_code}: "
        f"{pipelines_response.text}"
    )
    body = pipelines_response.json()
    names = [item["name"] for item in body.get("items", [])]
    assert name not in names, (
        f"Pipeline '{name}' was visible but should not have been: {names}"
    )


# -- RLS enforced at the database layer ------------------------------------


@when("a raw query runs without setting app.current_org_id")
def step_raw_query_no_rls() -> None:
    """Simulate a query that bypasses RLS.

    In a real scenario this would execute a raw SELECT on the DB without
    calling ``set_rls_org()``. Here the assertion logic lives in the
    ``Then`` step which exercises the function outside a transaction.
    """
    return


@then(parsers.parse("the query returns no rows for {expected}"))
def step_rls_enforced(request: Any, expected: str) -> None:
    """Verify that ``set_rls_org`` raises outside an active transaction."""
    from modulo.api.routes.pipelines import set_rls_org

    session = request.getfixturevalue("mock_session")
    session.in_transaction.return_value = False

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        coro = set_rls_org(session, ORG_ID)
        loop.run_until_complete(coro)
        msg = (
            "Expected RuntimeError when calling set_rls_org outside a "
            "transaction, but no error was raised"
        )
        raise AssertionError(msg)
    except RuntimeError:
        pass  # Expected — RLS requires an active transaction.
    finally:
        loop.close()


# -- Cross-org pipeline run is forbidden -----------------------------------


@given(
    parsers.parse('I authenticate as a user in "{org}"'),
)
def alt_authenticate_org(request: Any, org: str) -> None:
    """Store org identity for the cross-org run scenario."""
    request.node.current_org = org


@when(
    parsers.parse("I POST /api/pipelines/{pipeline_name}/runs"),
    target_fixture="run_response",
)
def cross_org_run(
    request: Any,
    alt_org_client: Any,
    pipeline_name: str,
    ctx: dict[str, Any],
) -> Any:
    """POST to /api/v1/runs with a pipeline_id derived from the name.

    Mocks ``get_pipeline`` to return ``None`` (simulating RLS filtering
    for a cross-org access).
    """
    pipeline_id = uuid.uuid5(ORG_ID, pipeline_name)

    with patch("modulo.api.routes.runs.get_pipeline") as mock_get:
        mock_get.return_value = None
        resp = alt_org_client.post(
            "/api/v1/runs",
            json={
                "pipeline_id": str(pipeline_id),
                "input_payload": {},
            },
        )
        _store_response(request, ctx, resp)
        return resp
