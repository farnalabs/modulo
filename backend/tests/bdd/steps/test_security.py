"""Step definitions for security features: credential store, input sanitization, RLS."""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.db.rls import set_rls_org, set_rls_user_context

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/security/credential_store.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/security/input_sanitization.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/security/rls_enforcement.feature")


@pytest.fixture
def patches():
    """Collect ``unittest.mock.patch`` instances for automatic cleanup.

    Every ``given`` / ``when`` step that starts a patch should append the
    patcher to this list.  The fixture stops all patches (in reverse order)
    when the scenario finishes.
    """
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        with contextlib.suppress(RuntimeError):
            p.stop()


# ===========================================================================
# security/credential_store.feature  —  3 scenarios
# ===========================================================================


@given(
    parsers.parse('a connector with API key "{api_key}"'),
    target_fixture="plaintext_key",
)
def connector_with_api_key(api_key: str) -> str:
    """Store a plaintext API key that will later be encrypted."""
    return api_key


@given("a connector with encrypted credential", target_fixture="encrypted_credential")
def connector_encrypted() -> dict[str, Any]:
    """Pre-encrypt a credential using Fernet for the read scenario."""
    key = Fernet.generate_key()
    f = Fernet(key)
    plaintext = "my-secret-api-key"
    encrypted = f.encrypt(plaintext.encode())
    return {
        "key": key,
        "encrypted": encrypted,
        "plaintext": plaintext,
    }


@given(
    parsers.parse("a credential encrypted with key A"),
    target_fixture="credential_key_a",
)
def credential_encrypted_key_a() -> dict[str, Any]:
    """Encrypt with one Fernet key so we can try to decrypt with another."""
    key_a = Fernet.generate_key()
    f_a = Fernet(key_a)
    encrypted = f_a.encrypt(b"my-secret-value")
    return {
        "key_a": key_a,
        "encrypted": encrypted,
        "plaintext": "my-secret-value",
    }


# -- Encrypt on write -------------------------------------------------------


@when(
    "I save the connector",
    target_fixture="saved_credential",
)
def save_connector(plaintext_key: str) -> dict[str, Any]:
    """Simulate encrypting a credential with Fernet on write."""
    key = Fernet.generate_key()
    f = Fernet(key)
    encrypted = f.encrypt(plaintext_key.encode())
    return {
        "fernet_key": key,
        "encrypted": encrypted,
        "plaintext": plaintext_key,
    }


@then("the stored credential value is a Fernet token")
def check_fernet_token(saved_credential: dict[str, Any]) -> None:
    """Verify the encrypted value looks like a Fernet token."""
    token = saved_credential["encrypted"]
    assert isinstance(token, bytes), f"Fernet token should be bytes, got {type(token)}"
    # All Fernet tokens start with the base64-encoded version byte 0x80
    # which decodes to "gAAAAA".
    assert token.startswith(b"gAAAAA"), f"Token does not start with Fernet magic bytes: {token[:20]!r}"
    # A valid Fernet token can be decoded without error.
    f = Fernet(saved_credential["fernet_key"])
    decrypted = f.decrypt(token)
    assert decrypted == saved_credential["plaintext"].encode(), (
        f"Round-trip mismatch: got {decrypted!r}, expected {saved_credential['plaintext']!r}"
    )


@then(
    parsers.parse('decrypting with FERNET_KEY yields "{expected}"'),
)
def decrypt_with_key(saved_credential: dict[str, Any], expected: str) -> None:
    """Confirm that decrypting with the same key recovers the plaintext."""
    f = Fernet(saved_credential["fernet_key"])
    decrypted = f.decrypt(saved_credential["encrypted"]).decode()
    assert decrypted == expected, f"Decrypted value '{decrypted}' does not match expected '{expected}'"


# -- Decrypt on read within a pipeline node --------------------------------


@when("a pipeline node calls connector.query()", target_fixture="decrypted_value")
def decrypt_on_read(encrypted_credential: dict[str, Any]) -> str:
    """Simulate the decrypt-on-read that happens before a connector call."""
    f = Fernet(encrypted_credential["key"])
    return f.decrypt(encrypted_credential["encrypted"]).decode()


@then("the node receives the plaintext credential")
def check_plaintext_received(decrypted_value: str) -> None:
    assert decrypted_value == "my-secret-api-key", f"Expected 'my-secret-api-key', got '{decrypted_value}'"


# -- Wrong FERNET_KEY cannot decrypt ---------------------------------------


@when(
    "the service restarts with key B",
    target_fixture="wrong_key",
)
def restart_with_key_b() -> bytes:
    """Generate a different Fernet key (key B) to simulate a key rotation."""
    return Fernet.generate_key()


@when("attempting to decrypt", target_fixture="decrypt_error")
def attempt_decrypt(credential_key_a: dict[str, Any], wrong_key: bytes) -> Exception | None:
    """Try to decrypt key-A-encrypted data with key B."""
    try:
        f = Fernet(wrong_key)
        f.decrypt(credential_key_a["encrypted"])
        return None  # No error — unexpected
    except InvalidToken as exc:
        return exc


@then("attempting to decrypt raises InvalidToken")
def check_invalid_token(decrypt_error: Exception | None) -> None:
    assert decrypt_error is not None, (
        "Expected InvalidToken when decrypting with wrong key, but no exception was raised"
    )
    assert isinstance(decrypt_error, InvalidToken), (
        f"Expected InvalidToken, got {type(decrypt_error).__name__}: {decrypt_error}"
    )


# ===========================================================================
# security/input_sanitization.feature  —  5 scenarios (input validation)
# ===========================================================================


def _store_response(request: pytest.FixtureRequest, resp) -> None:
    """Persist the response for the shared status / error-mentions steps.

    Shared steps read either ``request.node._resp`` (conftest status step,
    alpha error steps) or ``request.node.response`` (change-password steps),
    so both are populated for whichever definition is registered last.
    """
    request.node._resp = resp
    request.node.response = resp


@then(parsers.parse('the error mentions "{text}"'))
def step_error_mentions(text: str, request: pytest.FixtureRequest) -> None:
    """Assert the error detail mentions the given text.

    FastAPI's automatic 422 ``detail`` is a list of Pydantic validation
    errors; each entry's ``loc`` is a path-segment list (e.g.
    ``['body', 'name']``). Joining those segments into a dotted string lets
    field paths like ``body.name`` match the step text, while plain-string
    details are matched directly.
    """
    body = request.node._resp.json()
    detail = body.get("detail", body)
    haystack = str(detail)
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict) and isinstance(item.get("loc"), list):
                haystack += " " + ".".join(str(part) for part in item["loc"])
    assert text.lower() in haystack.lower(), f"Expected error to mention {text!r}, got: {haystack[:500]}"


def _patch_runs_trigger(patches, pipeline, snapshot) -> None:
    """Wire the run-trigger route's DB calls to the given pipeline/snapshot mocks."""
    patcher = patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)
    patcher = patch("modulo.api.routes.runs.get_pipeline", new_callable=AsyncMock, return_value=pipeline)
    patcher.start()
    patches.append(patcher)
    patcher = patch(
        "modulo.api.routes.runs.create_snapshot_from_live_graph",
        new_callable=AsyncMock,
        return_value=snapshot,
    )
    patcher.start()
    patches.append(patcher)


@when("I POST /api/pipelines with empty JSON body")
def step_post_pipeline_empty_body(client, request: pytest.FixtureRequest) -> None:
    """POST a pipeline with an empty body — Pydantic rejects the missing name."""
    _store_response(request, client.post("/api/v1/pipelines", json={}))


@when(parsers.parse('I create a cron trigger with expression "{expression}"'))
def step_create_cron_trigger(expression: str, client, request: pytest.FixtureRequest) -> None:
    """Create a cron trigger with the given expression — invalid ones get 422."""
    _store_response(
        request,
        client.post(
            f"/api/v1/pipelines/{uuid.uuid4()}/triggers",
            json={"trigger_type": "cron", "cron_expression": expression},
        ),
    )


@when("I trigger a run on a pipeline with an empty graph")
def step_trigger_run_empty_graph(client, request: pytest.FixtureRequest, patches) -> None:
    """POST a manual run whose snapshot graph has no nodes — rejected with 422."""
    from tests.bdd.conftest import make_mock_pipeline, make_mock_snapshot

    pipeline = make_mock_pipeline()
    snapshot = make_mock_snapshot(graph_json={"nodes": [], "edges": []})
    _patch_runs_trigger(patches, pipeline, snapshot)

    _store_response(
        request,
        client.post("/api/v1/runs", json={"pipeline_id": str(pipeline.id), "input_payload": {}}),
    )


@when("I trigger a run on a pipeline with a cyclic graph")
def step_trigger_run_cyclic_graph(client, request: pytest.FixtureRequest, patches) -> None:
    """POST a manual run whose snapshot graph is cyclic — rejected with 422."""
    from tests.bdd.conftest import make_mock_pipeline, make_mock_snapshot

    pipeline = make_mock_pipeline()
    node_a = uuid.uuid4()
    node_b = uuid.uuid4()
    snapshot = make_mock_snapshot(
        graph_json={
            "nodes": [
                {"id": str(node_a), "type": "agent"},
                {"id": str(node_b), "type": "agent"},
            ],
            "edges": [
                {"source": str(node_a), "target": str(node_b)},
                {"source": str(node_b), "target": str(node_a)},
            ],
        }
    )
    _patch_runs_trigger(patches, pipeline, snapshot)

    _store_response(
        request,
        client.post("/api/v1/runs", json={"pipeline_id": str(pipeline.id), "input_payload": {}}),
    )


@when(parsers.parse('I set a weak password "{password}"'))
def step_set_weak_password(password: str, client, request: pytest.FixtureRequest) -> None:
    """Change the password to a value that fails the policy — rejected with 422.

    ``me.change_password`` verifies ``current_password`` against the stored
    hash *before* password-strength validation. With the mock session the
    account's ``password_hash`` is a MagicMock, so ``verify_password`` fails
    and the route returns 400/500 — never the 422 the scenario asserts. Mock
    the account lookup with a real bcrypt hash for the current password so the
    route reaches the strength check.
    """
    from modulo.auth.passwords import hash_password

    with patch("modulo.api.routes.me.get_account_by_id") as mock_get_account:
        account = MagicMock()
        account.password_hash = hash_password("correct-horse-battery")
        mock_get_account.return_value = account
        _store_response(
            request,
            client.put(
                "/api/v1/me/password",
                json={"current_password": "correct-horse-battery", "new_password": password},
            ),
        )


# ===========================================================================
# security/rls_enforcement.feature  —  7 scenarios
# ===========================================================================


# -- Scenario: Authenticated user can access their org's pipelines ----------


@when(
    parsers.parse('the service accesses pipelines as user in org "{org}"'),
    target_fixture="pipeline_response",
)
def step_access_pipelines(org: str, client, alt_org_client, mock_session):
    """GET /api/v1/pipelines with the correct TestClient for the org."""
    from types import SimpleNamespace
    from unittest.mock import patch

    test_client = alt_org_client if org == "other-org" else client
    with patch("modulo.api.routes.pipelines.list_pipelines") as mock_list:
        mock_list.return_value = SimpleNamespace(
            items=[], total=0, page=1, page_size=20, next_cursor=None, has_more=False
        )
        return test_client.get("/api/v1/pipelines")


@then("the response status is 200")
def step_status_200(pipeline_response) -> None:
    assert pipeline_response.status_code == 200, (
        f"Expected 200, got {pipeline_response.status_code}: {pipeline_response.text}"
    )


# -- Scenario: Cross-org pipeline access returns 404 ------------------------


@when(
    parsers.parse('the service accesses pipeline {pipeline_id} as user in org "{org}"'),
    target_fixture="pipeline_response",
)
def step_access_cross_org_pipeline(pipeline_id: str, org: str, alt_org_client, mock_session):
    """GET /api/v1/pipelines/{id} with alt org — simulates cross-org access returning 404."""
    from unittest.mock import patch

    with patch("modulo.api.routes.pipelines.get_pipeline") as mock_get:
        mock_get.return_value = None
        return alt_org_client.get(f"/api/v1/pipelines/{pipeline_id}")


@then("the response status is 404")
def step_status_404(pipeline_response) -> None:
    assert pipeline_response.status_code == 404, (
        f"Expected 404, got {pipeline_response.status_code}: {pipeline_response.text}"
    )


# -- Scenario: Unauthenticated request returns 401 --------------------------


@when("an unauthenticated request accesses pipelines", target_fixture="pipeline_response")
def step_unauthenticated_access(unauth_client):
    """GET /api/v1/pipelines without any auth headers."""
    return unauth_client.get("/api/v1/pipelines")


@then("the response status is 401")
def step_status_401(pipeline_response) -> None:
    assert pipeline_response.status_code == 401, (
        f"Expected 401, got {pipeline_response.status_code}: {pipeline_response.text}"
    )


# -- Scenario: Viewer role cannot create pipelines --------------------------


@when(
    parsers.parse("a viewer tries to create a pipeline named {name}"),
    target_fixture="create_response",
)
def step_viewer_create_pipeline(name: str, viewer_client):
    """POST /api/v1/pipelines as viewer — expecting 403.

    The ``viewer_client`` fixture overrides the app's auth dependencies
    (``get_current_user`` / ``get_current_tenant_user``) with a viewer
    ``TenantPrincipal``, so ``require_permission("pipeline.create")`` denies
    with 403 before the route body runs.
    """
    return viewer_client.post(
        "/api/v1/pipelines",
        json={"name": name, "description": ""},
    )


@then("the viewer pipeline creation is rejected")
def step_viewer_rejected(create_response) -> None:
    assert create_response.status_code == 403, (
        f"Expected 403 for viewer, got {create_response.status_code}: {create_response.text}"
    )


# -- Scenario: RLS context requires an active transaction -------------------


@when("RLS context is set outside a transaction", target_fixture="rls_error")
def step_rls_outside_tx(mock_session):
    """Call set_rls_org outside an active transaction and catch RuntimeError.

    ``in_transaction`` must be a SYNC MagicMock — AsyncMock's call returns an
    unawaited coroutine (truthy), which defeats the ``not session.in_transaction()``
    guard in ``_ensure_active_transaction``.
    """
    import asyncio

    mock_session.in_transaction = MagicMock(return_value=False)
    try:
        asyncio.run(set_rls_org(mock_session, uuid.uuid4()))
        return None
    except RuntimeError as exc:
        return exc


@then("a RuntimeError is raised")
def step_runtime_error(rls_error) -> None:
    assert rls_error is not None, "Expected RuntimeError but none was raised"
    assert isinstance(rls_error, RuntimeError), f"Expected RuntimeError, got {type(rls_error).__name__}"
    assert "requires an active transaction" in str(rls_error), f"Unexpected error message: {rls_error}"


# -- Scenario: set_rls_user_context requires active transaction --------------


@when("set_rls_user_context is called outside a transaction", target_fixture="rls_error")
def step_user_context_outside_tx(mock_session):
    """Call set_rls_user_context outside an active transaction and catch RuntimeError.

    Sync def (``asyncio.run``) so pytest-bdd can set the ``rls_error`` target
    fixture; an ``async def`` step returns a coroutine pytest-bdd never awaits,
    leaving the fixture unset.
    """
    import asyncio

    mock_session.in_transaction = MagicMock(return_value=False)
    try:
        asyncio.run(set_rls_user_context(mock_session, uuid.uuid4(), "admin"))
        return None
    except RuntimeError as exc:
        return exc


# -- Scenario: set_rls_user_context sets user ID and org role ----------------


@given("an active transaction", target_fixture="active_tx_session")
def step_active_tx(mock_session):
    """Mark the mock session as having an active transaction."""
    mock_session.in_transaction = MagicMock(return_value=True)
    return mock_session


@when(
    parsers.parse('set_rls_user_context is called with user "{username}" and role "{role}"'),
    target_fixture="user_context_result",
)
def step_set_user_context(username: str, role: str, mock_session):
    """Call set_rls_user_context and record what was executed.

    The mock session must report a Postgres dialect so the real
    ``set_config`` path runs (AsyncMock's default dialect is a MagicMock, which
    routes into the generic ``session.info`` branch and never calls
    ``session.execute``).
    """
    user_id = uuid.uuid5(uuid.NAMESPACE_DNS, username)
    mock_session.info.clear()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    mock_session.get_bind = AsyncMock(return_value=bind)
    mock_session.execute = AsyncMock()
    import asyncio

    asyncio.run(set_rls_user_context(mock_session, user_id, role))
    return {"user_id": user_id, "role": role}


@then("the RLS user context is set correctly")
def step_verify_user_context(user_context_result: dict, mock_session) -> None:
    """Verify the executed SQL includes set_config calls for user_id and org_role."""
    calls = mock_session.execute.call_args_list
    texts = [str(call[0][0]) for call in calls]

    found_user_id = any("app.user_id" in t for t in texts)
    found_org_role = any("app.org_role" in t for t in texts)
    assert found_user_id, "No set_config call for app.user_id found"
    assert found_org_role, "No set_config call for app.org_role found"
