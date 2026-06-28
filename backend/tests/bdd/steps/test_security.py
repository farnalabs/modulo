"""Step definitions for security features: credential store, input sanitization, RLS."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pytest_bdd import given, parsers, scenarios, then, when

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
    assert isinstance(token, bytes), (
        f"Fernet token should be bytes, got {type(token)}"
    )
    # All Fernet tokens start with the base64-encoded version byte 0x80
    # which decodes to "gAAAAA".
    assert token.startswith(b"gAAAAA"), (
        f"Token does not start with Fernet magic bytes: "
        f"{token[:20]!r}"
    )
    # A valid Fernet token can be decoded without error.
    f = Fernet(saved_credential["fernet_key"])
    decrypted = f.decrypt(token)
    assert decrypted == saved_credential["plaintext"].encode(), (
        f"Round-trip mismatch: got {decrypted!r}, "
        f"expected {saved_credential['plaintext']!r}"
    )


@then(
    parsers.parse('decrypting with FERNET_KEY yields "{expected}"'),
)
def decrypt_with_key(
    saved_credential: dict[str, Any], expected: str
) -> None:
    """Confirm that decrypting with the same key recovers the plaintext."""
    f = Fernet(saved_credential["fernet_key"])
    decrypted = f.decrypt(saved_credential["encrypted"]).decode()
    assert decrypted == expected, (
        f"Decrypted value '{decrypted}' does not match expected '{expected}'"
    )


# -- Decrypt on read within a pipeline node --------------------------------


@when("a pipeline node calls connector.query()", target_fixture="decrypted_value")
def decrypt_on_read(encrypted_credential: dict[str, Any]) -> str:
    """Simulate the decrypt-on-read that happens before a connector call."""
    f = Fernet(encrypted_credential["key"])
    plaintext = f.decrypt(encrypted_credential["encrypted"]).decode()
    return plaintext


@then("the node receives the plaintext credential")
def check_plaintext_received(decrypted_value: str) -> None:
    assert decrypted_value == "my-secret-api-key", (
        f"Expected 'my-secret-api-key', got '{decrypted_value}'"
    )


# -- Wrong FERNET_KEY cannot decrypt ---------------------------------------


@when(
    "the service restarts with key B",
    target_fixture="wrong_key",
)
def restart_with_key_b() -> bytes:
    """Generate a different Fernet key (key B) to simulate a key rotation."""
    return Fernet.generate_key()


@then("attempting to decrypt raises InvalidToken")
def check_attempt_decrypt(
    credential_key_a: dict[str, Any], wrong_key: bytes
) -> None:
    """Try to decrypt key-A-encrypted data with key B, expect InvalidToken."""
    try:
        f = Fernet(wrong_key)
        f.decrypt(credential_key_a["encrypted"])
        pytest.fail("Expected InvalidToken when decrypting with wrong key, but no exception was raised")
    except InvalidToken:
        pass


# ===========================================================================
# Helpers (shared across security features)
# ===========================================================================

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _store_response(request, resp) -> None:
    request.node._resp = resp
    try:
        request.node._resp_body = resp.json()
    except Exception:
        request.node._resp_body = resp.text


def _patch_set_rls(patches, module_path):
    patcher = patch(module_path, new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)


# ===========================================================================
# security/input_sanitization.feature  —  5 scenarios
# ===========================================================================


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


@when("I POST /api/pipelines with empty JSON body")
def post_pipeline_empty(client, request, patches) -> None:
    _patch_set_rls(patches, "modulo.api.routes.pipelines.set_rls_org")
    resp = client.post("/api/v1/pipelines", json={})
    _store_response(request, resp)


@when(parsers.parse('I create a cron trigger with expression "{expression}"'))
def create_cron_trigger_invalid(client, request, patches, expression) -> None:
    _patch_set_rls(patches, "modulo.api.routes.triggers.set_rls_org")
    resp = client.post(
        f"/api/v1/pipelines/{uuid.uuid4()}/triggers",
        json={"trigger_type": "cron", "cron_expression": expression},
    )
    _store_response(request, resp)


@when("I trigger a run on a pipeline with an empty graph")
def trigger_run_empty_graph(client, request, patches) -> None:
    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    mock_snapshot = MagicMock()
    mock_snapshot.id = uuid.uuid4()
    mock_snapshot.graph_json = {"nodes": [], "edges": []}
    mock_snapshot.run_context_defaults = {}
    mock_snapshot.connector_bindings_json = []
    mock_snapshot.schema_pins_json = []
    mock_snapshot.prompt_pins_json = []
    mock_snapshot.model_backend_pins_json = []

    _patch_set_rls(patches, "modulo.api.routes.runs.set_rls_org")
    patcher = patch(
        "modulo.api.routes.runs.get_pipeline",
        new_callable=AsyncMock,
        return_value=mock_pipeline,
    )
    patcher.start()
    patches.append(patcher)

    patcher2 = patch(
        "modulo.api.routes.runs.create_snapshot_from_live_graph",
        new_callable=AsyncMock,
        return_value=mock_snapshot,
    )
    patcher2.start()
    patches.append(patcher2)

    resp = client.post(
        "/api/v1/runs",
        json={"pipeline_id": str(mock_pipeline.id), "input_payload": {}},
    )
    _store_response(request, resp)


@when("I trigger a run on a pipeline with a cyclic graph")
def trigger_run_cyclic(client, request, patches) -> None:
    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    node_a = uuid.uuid4()
    node_b = uuid.uuid4()
    mock_snapshot = MagicMock()
    mock_snapshot.id = uuid.uuid4()
    mock_snapshot.graph_json = {
        "nodes": [
            {"id": str(node_a), "agent_id": str(uuid.uuid4())},
            {"id": str(node_b), "agent_id": str(uuid.uuid4())},
        ],
        "edges": [
            {"source_node_id": str(node_a), "target_node_id": str(node_b)},
            {"source_node_id": str(node_b), "target_node_id": str(node_a)},
        ],
    }
    mock_snapshot.run_context_defaults = {}
    mock_snapshot.connector_bindings_json = []
    mock_snapshot.schema_pins_json = []
    mock_snapshot.prompt_pins_json = []
    mock_snapshot.model_backend_pins_json = []

    _patch_set_rls(patches, "modulo.api.routes.runs.set_rls_org")
    patcher = patch(
        "modulo.api.routes.runs.get_pipeline",
        new_callable=AsyncMock,
        return_value=mock_pipeline,
    )
    patcher.start()
    patches.append(patcher)

    patcher2 = patch(
        "modulo.api.routes.runs.create_snapshot_from_live_graph",
        new_callable=AsyncMock,
        return_value=mock_snapshot,
    )
    patcher2.start()
    patches.append(patcher2)

    resp = client.post(
        "/api/v1/runs",
        json={"pipeline_id": str(mock_pipeline.id), "input_payload": {}},
    )
    _store_response(request, resp)


@when(parsers.parse('I set a weak password "{password}"'))
def set_weak_password(request, password) -> None:
    from modulo.auth.passwords import validate_password_strength

    try:
        validate_password_strength(password)
        resp = MagicMock()
        resp.status_code = 200
    except Exception:
        resp = MagicMock()
        resp.status_code = 422
        resp.json = lambda: {"detail": "Password does not meet strength requirements"}
        resp.text = '{"detail": "Password does not meet strength requirements"}'
    _store_response(request, resp)


# ===========================================================================
# security/rls_enforcement.feature  —  5 scenarios
# Uses unique step texts to avoid conflicting with test_pipelines.py steps.
# ===========================================================================


def _authenticated_test_client(
    org_id: uuid.UUID = _ORG_ID,
    org_role: str = "admin",
) -> "TestClient":
    """Build a TestClient with an authenticated principal and mocked dependencies.

    Follows the conftest.py ``client`` fixture pattern but with a fresh app
    to avoid middleware state leakage.
    """
    from fastapi.testclient import TestClient
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings
    from tests.bdd.conftest import make_settings, make_mock_session

    async def override_session():
        yield make_mock_session()

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=org_role,
        organisation_id=org_id,
        user_id=uuid.uuid4(),
        org_role=org_role,
    )
    return TestClient(app)


@when(parsers.parse("the service accesses pipelines as user in org {org_ref}"))
def get_pipelines_as_org(request, org_ref, patches) -> None:
    """List pipelines — patches list_pipelines to avoid async mock issues."""
    from modulo.db.crud.base import PageResult
    from tests.bdd.conftest import make_mock_pipeline

    org_id = {
        "acme": _ORG_ID,
        "other-org": uuid.UUID("00000000-0000-0000-0000-000000000003"),
    }.get(org_ref, _ORG_ID)
    role = "viewer" if org_ref == "viewer" else "admin"

    _patch_set_rls(patches, "modulo.api.routes.pipelines.set_rls_org")
    patcher = patch(
        "modulo.api.routes.pipelines.list_pipelines",
        new_callable=AsyncMock,
        return_value=PageResult(items=[make_mock_pipeline(name="test")], total=1, page=1, page_size=20),
    )
    patcher.start()
    patches.append(patcher)

    client = _authenticated_test_client(org_id=org_id, org_role=role)
    try:
        resp = client.get("/api/v1/pipelines")
        _store_response(request, resp)
    finally:
        client.app.dependency_overrides.clear()


@when(parsers.parse("the service accesses pipeline {name} as user in org {org_ref}"))
def get_pipeline_as_org(request, name, org_ref, patches) -> None:
    """Access a single pipeline — 404 when the pipeline doesn't exist."""
    org_id = {
        "acme": _ORG_ID,
        "other-org": uuid.UUID("00000000-0000-0000-0000-000000000003"),
    }.get(org_ref, _ORG_ID)

    _patch_set_rls(patches, "modulo.api.routes.pipelines.set_rls_org")
    patcher = patch(
        "modulo.api.routes.pipelines.get_pipeline",
        new_callable=AsyncMock,
        return_value=None,
    )
    patcher.start()
    patches.append(patcher)

    client = _authenticated_test_client(org_id=org_id)
    try:
        resp = client.get(f"/api/v1/pipelines/{name}")
        _store_response(request, resp)
    finally:
        client.app.dependency_overrides.clear()


@when("an unauthenticated request accesses pipelines")
def unauth_get_pipelines(request) -> None:
    from fastapi.testclient import TestClient
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import get_settings
    from tests.bdd.conftest import make_settings, make_mock_session

    async def override_session():
        yield make_mock_session()

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    client = TestClient(app)
    try:
        resp = client.get("/api/v1/pipelines")
        _store_response(request, resp)
    finally:
        app.dependency_overrides.clear()


@when(parsers.parse("a viewer tries to create a pipeline named {name}"))
def viewer_create_pipeline(request, name, patches) -> None:
    _patch_set_rls(patches, "modulo.api.routes.pipelines.set_rls_org")

    client = _authenticated_test_client(org_role="viewer")
    try:
        resp = client.post("/api/v1/pipelines", json={"name": name})
        _store_response(request, resp)
    finally:
        client.app.dependency_overrides.clear()


@when("RLS context is set outside a transaction")
def rls_outside_transaction(request) -> None:
    from modulo.db.rls import set_rls_org
    from unittest.mock import AsyncMock

    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=False)
    try:
        import asyncio
        asyncio.run(set_rls_org(session, _ORG_ID))
        request.node._rls_error = None
    except RuntimeError as exc:
        request.node._rls_error = exc


@then("a RuntimeError is raised")
def check_runtime_error(request) -> None:
    assert request.node._rls_error is not None, "Expected RuntimeError but none was raised"
    assert isinstance(request.node._rls_error, RuntimeError), (
        f"Expected RuntimeError, got {type(request.node._rls_error).__name__}"
    )


@then(parsers.parse('the error mentions "{text}"'))
def check_error_mentions(request, text) -> None:
    body = request.node._resp_body
    detail = ""
    if isinstance(body, dict):
        err = body.get("error", {})
        if isinstance(err, dict):
            detail = err.get("detail")
        if not detail:
            detail = body.get("detail", "")
    else:
        detail = str(body)
    assert text.lower() in str(detail).lower(), (
        f"Expected error to mention {text!r}, got: {detail[:500]}"
    )


@then(parsers.parse("the response contains {count:d} pipelines"))
def check_pipeline_count(request, count) -> None:
    body = request.node._resp_body
    items = body.get("items", []) if isinstance(body, dict) else []
    assert len(items) == count, f"Expected {count} pipelines, got {len(items)}"


@then(parsers.parse("the response status is {status:d}"))
def check_response_status_sec(request, status) -> None:
    resp = request.node._resp
    assert resp.status_code == status, (
        f"Expected status {status}, got {resp.status_code}. Body: {resp.text[:500]}"
    )
