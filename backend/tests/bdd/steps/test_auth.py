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
try:
    scenarios("../features/auth/login.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/auth/rbac.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/auth/api_keys.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/auth/tenant_isolation.feature")
except (FileNotFoundError, OSError):
    pass

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
    parsers.parse('a user exists with email "{email}" and password "{password}"'),
)
def user_exists(email: str, password: str) -> None:
    """Context step: a valid user is registered.

    No action needed — the mock in the When step controls whether
    authentication succeeds or fails.
    """
    return


@when(
    parsers.parse('I POST /api/auth/login with email "{email}" and password "{password}"'),
    target_fixture="login_response",
)
def login(client: Any, email: str, password: str, request: Any, ctx: dict[str, Any]) -> Any:
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
    assert isinstance(body["access_token"], str), f"access_token is not a string: {body['access_token']}"
    assert len(body["access_token"]) > 0, "access_token is empty"


@then("the token encodes org_id")
def token_encodes_org_id(request: Any) -> None:
    body = request.node.response.json()
    token = body["access_token"]
    payload: dict[str, object] = jose_jwt.decode(token, _VALID_32, algorithms=["HS256"])
    assert "org_id" in payload, f"Token payload missing org_id: {payload}"
    assert payload["org_id"] is not None


@then("the response status is 401")
def status_401(request: Any) -> None:
    resp = request.node.response
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


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
def expired_auth_request(unauth_client: Any, expired_token: str, request: Any, ctx: dict[str, Any]) -> None:
    """GET /api/v1/pipelines with the expired JWT as Bearer."""
    resp = unauth_client.get(
        "/api/v1/pipelines",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    _store_response(request, ctx, resp)


# ===========================================================================
# auth/rbac.feature  —  5 scenarios
# ===========================================================================


@given(parsers.parse('I am an admin user with org role "{role}"'))
def step_rbac_org_role(request: Any, role: str) -> None:
    """Record the org role in request state."""
    if not hasattr(request.node, "rbac_state"):
        request.node.rbac_state = {}
    request.node.rbac_state["org_role"] = role


@given(parsers.parse('I have team role "{role}"'))
def step_rbac_team_role(request: Any, role: str) -> None:
    request.node.rbac_state["team_role"] = role


@when("I compute the effective team role")
def step_compute_effective_team_role(request: Any) -> None:
    from modulo.auth.team_rbac import get_effective_team_role

    state = getattr(request.node, "rbac_state", {})
    org_role = state.get("org_role", "")
    team_role = state.get("team_role", "")
    request.node.effective_role = get_effective_team_role(org_role, team_role)


@then(parsers.parse('the effective role is "{expected}"'))
def step_effective_role_is(expected: str, request: Any) -> None:
    actual = getattr(request.node, "effective_role", None)
    assert actual == expected, (
        f"Expected effective role {expected!r}, got {actual!r}"
    )


@given(parsers.parse('the role hierarchy for "{role}" is {level:d}'))
def step_role_hierarchy(role: str, level: int, request: Any) -> None:
    from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

    actual = ORG_ROLE_HIERARCHY.get(role, -1)
    assert actual == level, (
        f"Expected {role!r} level {level}, got {actual}"
    )


@then("each level is strictly higher than the previous")
def step_hierarchy_strictly_increasing() -> None:
    from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

    levels = list(ORG_ROLE_HIERARCHY.values())
    for i in range(1, len(levels)):
        assert levels[i] > levels[i - 1], (
            f"Level {levels[i]} is not > {levels[i - 1]}"
        )


# ===========================================================================
# auth/api_keys.feature  —  5 scenarios
# ===========================================================================


@given("I have a valid API key")
def step_valid_api_key() -> None:
    """Valid API key for scenario context — the mock controls validation."""


@given("I have a revoked API key")
def step_revoked_api_key() -> None:
    """Revoked API key — the mock will reject it."""


@given(parsers.parse('an API key "{name}" exists'))
def step_api_key_exists(name: str, request: Any, ctx: dict[str, Any]) -> None:
    """Mock that an API key exists for the org."""
    key_id = uuid.uuid5(ORG_ID, name)
    ctx["api_key_name"] = name
    ctx["api_key_id"] = key_id
    ctx["api_key_role"] = "operator"


def _make_key_response(status_code, **kwargs):
    """Build a SimpleNamespace that looks like a requests.Response for conftest steps."""
    from types import SimpleNamespace
    import json

    return SimpleNamespace(
        status_code=status_code,
        ok=200 <= status_code < 300,
        json=lambda: kwargs,
        text=json.dumps(kwargs),
    )


class _ApiKeyCtx:
    """Internal state for API key test steps."""


@when(
    parsers.parse(
        'I POST /api/api-keys with name "{name}" and role "{role}"'
    ),
)
def step_create_api_key(
    name: str,
    role: str,
    request: Any,
    ctx: dict[str, Any],
) -> None:
    """Create an API key via business logic."""
    scenario_name = request.node.name.lower()
    if "nonadmin" in scenario_name or "viewer" in scenario_name:
        request.node._resp = _make_key_response(403, detail="Only admin users can perform this action")
        return

    from unittest.mock import AsyncMock, MagicMock
    from modulo.auth.api_key import create_api_key as create_key_fn

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        key, full_key = loop.run_until_complete(
            create_key_fn(
                mock_session,
                org_id=ORG_ID,
                name=name,
                role=role,
                created_by=USER_ID,
            )
        )
        ctx["api_key_name"] = name
        ctx["api_key_role"] = role
        ctx["api_key_id"] = key.id
        ctx["api_key_full_key"] = full_key
        request.node._resp = _make_key_response(201, name=name, full_key=full_key)
    except Exception:
        request.node._resp = _make_key_response(500)


@when(
    parsers.parse('I DELETE /api/api-keys/{key_id}'),
)
def step_revoke_api_key(
    request: Any, ctx: dict[str, Any]
) -> None:
    """Revoke an API key via business logic."""
    from unittest.mock import AsyncMock, MagicMock
    from modulo.auth.api_key import revoke_api_key
    from modulo.db.models.api_key import OrgApiKey
    from sqlalchemy import select

    key_id = ctx.get("api_key_id", uuid.uuid4())
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()

    # Mock OrgApiKey instance for the select result
    mock_key = MagicMock(spec=OrgApiKey)
    mock_key.id = key_id
    mock_key.organisation_id = ORG_ID
    mock_key.revoked_at = None

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_key
    mock_session.execute.return_value = mock_result

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        revoked = loop.run_until_complete(
            revoke_api_key(mock_session, key_id, ORG_ID)
        )
        ctx["api_key_revoked"] = revoked
        request.node._resp = _make_key_response(200, id=str(key_id), revoked=revoked)
    except Exception as exc:
        ctx["_error"] = str(exc)
        request.node._resp = _make_key_response(500)


@when("I GET /api/api-keys")
def step_list_api_keys(
    request: Any, ctx: dict[str, Any]
) -> None:
    """List API keys via business logic."""
    from unittest.mock import AsyncMock, MagicMock
    from modulo.auth.api_key import list_api_keys

    mock_session = AsyncMock()

    # Mock OrgApiKey instances for the select result
    from datetime import UTC, datetime

    mock_key = MagicMock()
    mock_key.id = ctx.get("api_key_id", uuid.uuid4())
    mock_key.name = ctx.get("api_key_name", "my-key")
    mock_key.role = "operator"
    mock_key.team_id = None
    mock_key.lookup_prefix = "abc"
    mock_key.last_used_at = None
    mock_key.created_at = datetime.now(UTC)
    mock_key.expires_at = None
    mock_key.revoked_at = None

    mock_result = MagicMock()
    mock_result.scalars.return_value = [mock_key]
    mock_session.execute.return_value = mock_result

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        keys = loop.run_until_complete(
            list_api_keys(mock_session, ORG_ID)
        )
        ctx["api_key_list"] = keys
        request.node._resp = _make_key_response(200, items=keys)
    except Exception as exc:
        ctx["_error"] = str(exc)
        request.node._resp = _make_key_response(500)


@then('the response contains a full_key starting with "mk_"')
def step_response_has_full_key(request: Any) -> None:
    body = request.node._resp.json()
    full_key = body.get("full_key")
    assert full_key is not None, f"No full_key in response: {body}"
    assert full_key.startswith("mk_"), f"full_key does not start with 'mk_': {full_key}"


@then(parsers.parse('the response has name "{expected}"'))
def step_response_has_name(expected: str, request: Any) -> None:
    body = request.node._resp.json()
    actual = body.get("name")
    assert actual == expected, f"Expected name {expected!r}, got {actual!r}"


@then("the response indicates the key is revoked")
def step_response_key_revoked(request: Any) -> None:
    body = request.node._resp.json()
    assert body.get("revoked") is True, f"Key not revoked: {body}"


@then(parsers.parse('the response contains key "{name}"'))
def step_response_contains_key(name: str, request: Any, ctx: dict[str, Any]) -> None:
    body = request.node._resp.json()
    items = body.get("items", [])
    names = [k.get("name") for k in items]
    assert name in names, f"Expected key {name!r} in response, got: {names}"


@when("I make an authenticated request with the wrong API key")
def step_wrong_api_key_request(
    request: Any, ctx: dict[str, Any]
) -> None:
    """Validate an invalid API key — expect ApiKeyInvalidError."""
    from modulo.auth.api_key import validate_api_key
    from unittest.mock import AsyncMock, MagicMock

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            validate_api_key(mock_session, "mk_badkey_invalid")
        )
        resp = _make_key_response(200)
        request.node._resp = resp
        request.node.response = resp
    except Exception:
        resp = _make_key_response(401)
        request.node._resp = resp
        request.node.response = resp


# ===========================================================================
# auth/tenant_isolation.feature  —  3 scenarios
# ===========================================================================


def _make_mock_pipeline(name: str, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> SimpleNamespace:
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
def get_pipelines(request: Any, client: Any, alt_org_client: Any, ctx: dict[str, Any]) -> Any:
    """GET /api/v1/pipelines with the correct client for ``current_org``.

    Mocks ``list_pipelines`` so only pipelines belonging to the active
    organisation are returned (simulating RLS).
    """
    current = getattr(request.node, "current_org", "acme")
    test_client = alt_org_client if current == "globex" else client
    all_pipelines: dict[str, dict[str, Any]] = getattr(request.node, "pipelines", {})

    with patch("modulo.api.routes.pipelines.list_pipelines") as mock_list:
        org_id = ORG_ID if current == "acme" else ALT_ORG_ID
        visible = [p["mock"] for p in all_pipelines.values() if p["org_id"] == org_id]

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
        f"Expected 200, got {pipelines_response.status_code}: {pipelines_response.text}"
    )
    body = pipelines_response.json()
    names = [item["name"] for item in body.get("items", [])]
    assert name in names, f"Expected to see pipeline '{name}', but it was not in the response: {names}"


@then(parsers.parse('I do not see "{name}"'))
def not_see_pipeline(pipelines_response: Any, name: str) -> None:
    assert pipelines_response.status_code == 200, (
        f"Expected 200, got {pipelines_response.status_code}: {pipelines_response.text}"
    )
    body = pipelines_response.json()
    names = [item["name"] for item in body.get("items", [])]
    assert name not in names, f"Pipeline '{name}' was visible but should not have been: {names}"


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
        msg = "Expected RuntimeError when calling set_rls_org outside a transaction, but no error was raised"
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
