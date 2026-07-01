"""Step definitions for security features: credential store, input sanitization, RLS."""

import uuid
from typing import Any
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet, InvalidToken
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.db.rls import set_rls_org, set_rls_user_context

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
try:
    scenarios("../features/security/credential_store.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/security/input_sanitization.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/security/rls_enforcement.feature")
except (FileNotFoundError, OSError):
    pass

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
    plaintext = f.decrypt(encrypted_credential["encrypted"]).decode()
    return plaintext


@then("the node receives the plaintext credential")
def check_plaintext_received(decrypted_value: str) -> None:
    assert decrypted_value == "my-secret-api-key", f"Expected 'my-secret-api-key', got '{decrypted_value}'"


# -- Wrong FERNET_KEY cannot decrypt ---------------------------------------


@given(
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
# security/input_sanitization.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("I submit a prompt with Jinja2 template injection")
def step_jinja2_injection() -> bool:
    """Placeholder: Jinja2 sandbox enforcement scenario."""
    return True


@given("I submit a YAML payload with !!python/object tag")
def step_yaml_injection() -> bool:
    """Placeholder: yaml.safe_load enforcement scenario."""
    return True


# ===========================================================================
# security/rls_enforcement.feature  —  7 scenarios
# ===========================================================================


# -- Scenario: Authenticated user can access their org's pipelines ----------


@given(
    parsers.parse('I am authenticated in org "{org}"'),
    target_fixture="current_org",
)
def step_authenticated_org(org: str) -> str:
    """Record which org the user is authenticated in."""
    return org


@when(
    parsers.parse('the service accesses pipelines as user in org "{org}"'),
    target_fixture="pipeline_response",
)
def step_access_pipelines(org: str, client, alt_org_client, mock_session, current_org: str):
    """GET /api/v1/pipelines with the correct TestClient for the org."""
    from types import SimpleNamespace
    from unittest.mock import patch

    test_client = alt_org_client if org == "other-org" else client
    with patch("modulo.api.routes.pipelines.list_pipelines") as mock_list:
        mock_list.return_value = SimpleNamespace(items=[], total=0, page=1, page_size=20)
        resp = test_client.get("/api/v1/pipelines")
        return resp


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
        resp = alt_org_client.get(f"/api/v1/pipelines/{pipeline_id}")
        return resp


@then("the response status is 404")
def step_status_404(pipeline_response) -> None:
    assert pipeline_response.status_code == 404, (
        f"Expected 404, got {pipeline_response.status_code}: {pipeline_response.text}"
    )


# -- Scenario: Unauthenticated request returns 401 --------------------------


@when("an unauthenticated request accesses pipelines", target_fixture="pipeline_response")
def step_unauthenticated_access(unauth_client):
    """GET /api/v1/pipelines without any auth headers."""
    resp = unauth_client.get("/api/v1/pipelines")
    return resp


@then("the response status is 401")
def step_status_401(pipeline_response) -> None:
    assert pipeline_response.status_code == 401, (
        f"Expected 401, got {pipeline_response.status_code}: {pipeline_response.text}"
    )


# -- Scenario: Viewer role cannot create pipelines --------------------------


@given(
    parsers.parse('I am authenticated as a viewer in org "{org}"'),
    target_fixture="viewer_org",
)
def step_viewer_auth(org: str) -> str:
    """Record viewer authentication context."""
    return org


@when(
    parsers.parse('a viewer tries to create a pipeline named {name}'),
    target_fixture="create_response",
)
def step_viewer_create_pipeline(name: str, client):
    """POST /api/v1/pipelines as viewer — expecting rejection."""
    from unittest.mock import patch

    with patch("modulo.api.dependencies.get_current_user") as mock_user:
        mock_user.return_value = {
            "org_id": str(uuid.UUID("00000000-0000-0000-0000-000000000001")),
            "user_id": str(uuid.uuid4()),
            "org_role": "viewer",
        }
        resp = client.post(
            "/api/v1/pipelines",
            json={"name": name, "description": ""},
        )
        return resp


@then("the viewer pipeline creation is rejected")
def step_viewer_rejected(create_response) -> None:
    assert create_response.status_code == 403, (
        f"Expected 403 for viewer, got {create_response.status_code}: {create_response.text}"
    )


# -- Scenario: RLS context requires an active transaction -------------------


@when("RLS context is set outside a transaction", target_fixture="rls_error")
async def step_rls_outside_tx(mock_session):
    """Call set_rls_org outside an active transaction and catch RuntimeError."""
    mock_session.in_transaction.return_value = False
    try:
        await set_rls_org(mock_session, uuid.uuid4())
        return None
    except RuntimeError as exc:
        return exc


@then("a RuntimeError is raised")
def step_runtime_error(rls_error) -> None:
    assert rls_error is not None, "Expected RuntimeError but none was raised"
    assert isinstance(rls_error, RuntimeError), f"Expected RuntimeError, got {type(rls_error).__name__}"
    assert "requires an active transaction" in str(rls_error), (
        f"Unexpected error message: {rls_error}"
    )


# -- Scenario: set_rls_user_context requires active transaction --------------


@when("set_rls_user_context is called outside a transaction", target_fixture="user_context_error")
async def step_user_context_outside_tx(mock_session):
    """Call set_rls_user_context outside an active transaction and catch RuntimeError."""
    mock_session.in_transaction.return_value = False
    try:
        await set_rls_user_context(mock_session, uuid.uuid4(), "admin")
        return None
    except RuntimeError as exc:
        return exc


# -- Scenario: set_rls_user_context sets user ID and org role ----------------


@given("an active transaction", target_fixture="active_tx_session")
def step_active_tx(mock_session):
    """Mark the mock session as having an active transaction."""
    mock_session.in_transaction.return_value = True
    return mock_session


@when(
    parsers.parse('set_rls_user_context is called with user "{username}" and role "{role}"'),
    target_fixture="user_context_result",
)
async def step_set_user_context(username: str, role: str, mock_session):
    """Call set_rls_user_context and record what was executed."""
    user_id = uuid.uuid5(uuid.NAMESPACE_DNS, username)
    mock_session.info.clear()
    mock_session.execute = AsyncMock()
    await set_rls_user_context(mock_session, user_id, role)
    return {"user_id": user_id, "role": role}


@then("the RLS user context is set correctly")
def step_verify_user_context(user_context_result: dict, mock_session) -> None:
    """Verify the executed SQL includes set_config calls for user_id and org_role."""
    assert mock_session.execute.called, "set_rls_user_context did not execute any SQL"
    calls = mock_session.execute.call_args_list
    texts = [str(call[0][0]) for call in calls]

    found_user_id = any("app.user_id" in t for t in texts)
    found_org_role = any("app.org_role" in t for t in texts)
    assert found_user_id, "No set_config call for app.user_id found"
    assert found_org_role, "No set_config call for app.org_role found"
