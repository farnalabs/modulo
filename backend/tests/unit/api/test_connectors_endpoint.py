"""Unit tests for /api/v1/connectors endpoints.

Credentials (raw credential strings) must NEVER appear in responses.
Only `has_credentials: true/false` is exposed.
"""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CONNECTOR_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_CRUD_PATCH_PREFIX = "modulo.api.routes.connectors."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


@pytest.fixture(autouse=True)
def _stub_get_connector_instance() -> Generator[None, None, None]:
    """The IDOR ownership check reads the connector via ``get_connector_instance``
    before the write CRUD, but the write-path cases only mock
    ``update_connector_instance`` / ``delete_connector_instance``. Supply a
    same-org connector so the ownership check passes for the legitimate
    (same-org) principal these tests use."""
    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=_make_connector(),
    ):
        yield


def _make_connector(credentials_ciphertext: bytes = b"encrypted", tier: str = "native") -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.organisation_id = _ORG_ID
    ci.name = "Test Connector"
    ci.connector_type_id = "filesystem"
    ci.credentials_ciphertext = credentials_ciphertext
    ci.config_json = {}
    ci.allowed_operations = []
    ci.status = "active"
    ci.visibility = "org"
    ci.owner_team_id = None
    ci.tier = tier
    ci.created_at = _NOW
    ci.updated_at = _NOW
    # Nullable degraded markers: a bare MagicMock would auto-create these as
    # non-serialisable mocks, so mirror a healthy ORM row explicitly.
    ci.degraded_at = None
    ci.last_skip_error = None
    return ci


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="vieweruser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="viewer"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


_CREATE_BODY = {
    "name": "Test Connector",
    "connector_type_id": "filesystem",
    "credentials": '{"token": "secret123"}',
}


def _crud_cases() -> list[dict[str, object]]:
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    connector = _make_connector()
    updated = _make_connector()
    updated.name = "Updated"
    return [
        {
            "id": "list",
            "method": "GET",
            "url": "/api/v1/connectors",
            "body": None,
            "patches": [("list_connector_instances", page_result)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["total"] == 1,
        },
        {
            "id": "get",
            "method": "GET",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": None,
            "patches": [("get_connector_instance", connector)],
            "expected_status": 200,
            "check": lambda resp: "credentials_ciphertext" not in resp.json() and resp.json()["has_credentials"],
        },
        {
            "id": "get_not_found",
            "method": "GET",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": None,
            "patches": [("get_connector_instance", None)],
            "expected_status": 404,
        },
        {
            "id": "update",
            "method": "PATCH",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": {"name": "Updated"},
            "patches": [("get_connector_instance", connector), ("update_connector_instance", updated)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["name"] == "Updated",
        },
        {
            "id": "update_not_found",
            "method": "PATCH",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": {"name": "x"},
            "patches": [("get_connector_instance", None), ("update_connector_instance", None)],
            "expected_status": 404,
        },
        {
            "id": "delete",
            "method": "DELETE",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": None,
            "patches": [("delete_connector_instance", True)],
            "expected_status": 204,
        },
        {
            "id": "delete_not_found",
            "method": "DELETE",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": None,
            "patches": [("delete_connector_instance", False)],
            "expected_status": 404,
        },
    ]


@pytest.mark.parametrize("case", _crud_cases(), ids=lambda c: c["id"])
def test_crud(client: TestClient, case: dict[str, object]) -> None:
    method = case["method"]
    url = case["url"]
    body = case.get("body")
    expected_status = case["expected_status"]
    check = case.get("check")

    patchers = []
    for func_name, ret in case["patches"]:
        patchers.append(patch(f"{_CRUD_PATCH_PREFIX}{func_name}", return_value=ret))
    patchers.append(patch(f"{_CRUD_PATCH_PREFIX}set_rls_org"))
    patchers.append(patch(f"{_CRUD_PATCH_PREFIX}set_rls_user_context"))

    for p in patchers:
        p.start()

    try:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        elif method == "PUT":
            resp = client.put(url, json=body or {})
        else:
            raise ValueError(f"Unsupported method: {method}")

        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
        if check:
            assert check(resp)
    finally:
        for p in patchers:
            p.stop()


def test_create_connector_does_not_expose_credentials(client: TestClient) -> None:
    connector = _make_connector(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_CREATE_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert "credentials" not in body
    assert body["has_credentials"] is True


def _make_github_body(token: str) -> dict[str, str]:
    return {
        "name": "GitHub Connector",
        "connector_type_id": "github",
        "credentials": token,
        "config_json": {},
    }


def test_create_github_fine_grained_token_accepted(client: TestClient) -> None:
    """A fine-grained PAT (github_pat_ prefix) is not rejected by the classic scope check."""
    token = "github_pat_11ABC"
    connector = _make_connector(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.GitHubConnector.verify_scopes", new=AsyncMock(return_value=set())),
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_make_github_body(token))

    assert resp.status_code == 201


def test_create_github_fine_grained_missing_permission_reports_fine_grained_detail(client: TestClient) -> None:
    """Fine-grained missing permissions surface the PRD §7.11 permission set, not classic scopes."""
    token = "github_pat_11ABC"
    with (
        patch(
            "modulo.api.routes.connectors.GitHubConnector.verify_scopes",
            new=AsyncMock(return_value={"contents:write", "pull_requests:write"}),
        ),
        patch("modulo.api.routes.connectors.create_connector_instance"),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_make_github_body(token))

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "fine-grained permissions" in detail
    assert "pull_requests:write" in detail
    assert "repo" not in detail
    assert "contents:write" in detail


def test_create_github_classic_missing_scope_reports_classic_detail(client: TestClient) -> None:
    """Classic tokens keep the classic OAuth-scope rejection detail."""
    token = "ghp_classic123"
    with (
        patch(
            "modulo.api.routes.connectors.GitHubConnector.verify_scopes",
            new=AsyncMock(return_value={"repo"}),
        ),
        patch("modulo.api.routes.connectors.create_connector_instance"),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_make_github_body(token))

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "OAuth scopes" in detail
    assert "repo" in detail


def test_create_connector_encrypts_credentials(client: TestClient) -> None:
    captured: list[bytes] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs["credentials_ciphertext"])  # type: ignore[arg-type]
        return _make_connector(credentials_ciphertext=kwargs["credentials_ciphertext"])  # type: ignore[arg-type]

    with (
        patch("modulo.api.routes.connectors.create_connector_instance", new=fake_create),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        client.post("/api/v1/connectors", json=_CREATE_BODY)

    assert captured, "create_connector_instance was not called"
    ciphertext = captured[0]
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    assert decrypted == '{"token": "secret123"}'
    assert b"secret123" not in ciphertext


def test_connector_no_credentials_shows_false(client: TestClient) -> None:
    connector = _make_connector(credentials_ciphertext=b"")
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.json()["has_credentials"] is False


def test_list_connectors_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/connectors")
    assert resp.status_code in (401, 403)


def test_list_connectors_default_excludes_in_dev(client: TestClient) -> None:
    """The list endpoint defaults to the CRUD in_dev exclusion (excluded_tiers=None)."""
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result) as mock_list,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors")
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def test_list_connectors_include_in_dev_passes_empty_exclusions(client: TestClient) -> None:
    """?include_in_dev=true reveals In-Dev connectors in the actual response JSON."""
    in_dev = _make_connector(tier="in_dev")
    page_result = MagicMock(items=[in_dev], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result) as mock_list,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors", params={"include_in_dev": "true"})
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert not mock_list.await_args.kwargs["excluded_tiers"]
    tiers = [item["tier"] for item in resp.json()["items"]]
    assert "in_dev" in tiers, f"Expected an in_dev connector in the response, got tiers: {tiers}"


def test_list_connectors_include_in_dev_denied_for_viewer(viewer_client: TestClient) -> None:
    """Viewers can list connectors but must NOT be able to reveal In-Dev items."""
    resp = viewer_client.get("/api/v1/connectors", params={"include_in_dev": "true"})
    assert resp.status_code == 403
    assert "connector.list.in_dev" in resp.json()["detail"]


def test_list_connectors_include_in_dev_operator_reveals_in_dev(client: TestClient) -> None:
    """An operator+ principal (admin fixture) can list In-Dev connectors."""
    in_dev = _make_connector(tier="in_dev")
    page_result = MagicMock(items=[in_dev], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors", params={"include_in_dev": "true"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["tier"] == "in_dev"


def test_list_connectors_include_in_dev_false_keeps_exclusion(client: TestClient) -> None:
    """?include_in_dev=false behaves exactly like omitting the parameter."""
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result) as mock_list,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors", params={"include_in_dev": "false"})
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def _foreign_org_connector() -> MagicMock:
    """A connector owned by a different organisation than the test principal."""
    foreign = MagicMock()
    foreign.id = _CONNECTOR_ID
    foreign.organisation_id = uuid.uuid4()
    return foreign


def test_update_connector_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not update a connector it
    does not own. The ownership check must raise 404 before any write."""
    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=_foreign_org_connector(),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={})
    assert resp.status_code == 404


def test_delete_connector_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not delete a connector it
    does not own."""
    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=_foreign_org_connector(),
    ):
        resp = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 404


def _make_rest_connector(credentials_ciphertext: bytes) -> MagicMock:
    ci = _make_connector(credentials_ciphertext=credentials_ciphertext)
    ci.connector_type_id = "rest"
    return ci


def _encrypt_creds(creds: dict[str, object]) -> bytes:
    return Fernet(_FERNET_KEY.encode()).encrypt(json.dumps(creds).encode())


def test_patch_identity_only_edit_preserves_secret_and_updates_identity(client: TestClient) -> None:
    """PATCH with credentials containing an identity change but NO secret value
    must leave the stored secret intact AND apply the new identity (so
    ``_normalise_auth`` reads the new identity)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "", "in": "header", "header_name": "X-Key-V2"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps(incoming), "name": "Test Connector"},
        )
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["api_key"] == "secret123", f"Secret must be preserved, got {decoded}"
    assert decoded["header_name"] == "X-Key-V2", f"Identity must be updated, got {decoded}"
    assert decoded["auth_mode"] == "api_key"


def test_patch_with_real_secret_replaces_it(client: TestClient) -> None:
    """PATCH with a real (non-empty) secret value replaces the stored secret."""
    stored = {"auth_mode": "api_key", "api_key": "old-secret", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "new-secret", "in": "header", "header_name": "X-Key"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["api_key"] == "new-secret", f"Secret must be replaced, got {decoded}"


def test_patch_identity_only_edit_round_trip_reads_new_identity(client: TestClient) -> None:
    """After an identity-only PATCH, the persisted credential, when decrypted and
    passed to the REST connector's ``_normalise_auth``, yields the NEW identity."""
    from modulo.connectors.rest import RestConnector

    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "", "in": "header", "header_name": "X-Key-V2"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    stored_after = json.loads(
        Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode()
    )
    auth = RestConnector._normalise_auth(stored_after)
    assert auth["header_name"] == "X-Key-V2"
    assert auth["api_key"] == "secret123"


def test_patch_non_rest_full_replace_not_overlaid(client: TestClient) -> None:
    """Non-REST connectors keep historical FULL-REPLACE semantics: a partial
    credential dict REPLACES the stored credential outright (no identity-vs-secret
    overlay), so the secret the incoming payload omits is dropped — unchanged
    behaviour from before FAR-466."""
    stored = {"bot_token": "secret123", "other": "x"}
    existing = _make_connector(_encrypt_creds(stored))  # connector_type_id = "filesystem"
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"other": "y"}  # partial — omits bot_token
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded == {"other": "y"}, f"Non-REST must FULL-REPLACE (no overlay), got {decoded}"
    assert "bot_token" not in decoded, f"Non-REST full-replace must drop an omitted field, got {decoded}"


def test_patch_undecryptable_ciphertext_does_not_wipe_secret(client: TestClient) -> None:
    """A stored ciphertext that EXISTS but cannot be decrypted must never be
    silently degraded to {} and re-encrypted as a secret-free overlay (which
    would wipe the secret). The PATCH fails loudly (5xx) and does NOT commit a
    credential write."""
    existing = _make_rest_connector(b"not-a-valid-fern-ciphertext")
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "api_key", "api_key": "", "header_name": "X-Key"})},
        )
    assert resp.status_code == 500
    assert "could not be decrypted" in resp.json()["detail"]
    # The secret was never re-encrypted & written: update_connector_instance did not run.
    mock_update.assert_not_awaited()


def test_patch_non_rest_name_only_null_credentials_no_500(client: TestClient) -> None:
    """A non-REST name-only edit posting credentials: null (empty config textarea)
    must NOT 500 — the credential write is skipped and the name is updated."""
    existing = _make_connector()  # connector_type_id = "filesystem"
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"name": "Renamed", "credentials": None},
        )
    assert resp.status_code == 200
    assert captured["updates"].get("name") == "Renamed"
    assert "credentials_ciphertext" not in captured["updates"]


def test_patch_rest_overlay_bearer_without_token_rejected(client: TestClient) -> None:
    """A direct PATCH overlaying auth_mode=bearer onto an api_key connector with
    no token must be rejected (422) and must NOT persist the broken credential.
    The UI blocks this; the API must not silently save a connector whose first
    run raises at ``RestConnector._normalise_auth`` (bearer requires token)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "bearer"})},
        )
    assert resp.status_code == 422
    assert "REST bearer auth requires creds['token']" in resp.json()["detail"]
    mock_update.assert_not_awaited()


def test_patch_rest_overlay_bearer_with_token_succeeds(client: TestClient) -> None:
    """A PATCH supplying a complete bearer credential succeeds and persists it
    (the connector's auth contract is satisfied after the overlay)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "bearer", "token": "t"})},
        )
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["auth_mode"] == "bearer"
    assert decoded["token"] == "t"


def _make_github_patch_connector() -> MagicMock:
    ci = _make_connector(credentials_ciphertext=_encrypt_creds({"token": "old"}))
    ci.connector_type_id = "github"
    return ci


def test_patch_github_raw_token_not_validated(client: TestClient) -> None:
    """The GitHub raw-token (non-JSON credential) PATCH path keeps its historical
    semantics — it is persisted verbatim and is NOT run through the REST
    auth-contract validation, so a raw token is never rejected as a broken REST
    credential."""
    existing = _make_github_patch_connector()
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    token = "ghp_rawtokenvalue"
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.GitHubConnector.verify_scopes", new=AsyncMock(return_value=set())),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": token},
        )
    assert resp.status_code == 200
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode()
    assert decrypted == token  # persisted verbatim, not overlaid/validated


def test_patch_rest_bare_string_credential_rejected(client: TestClient) -> None:
    """A raw non-JSON (bare-string) credential payload on a REST connector must be
    rejected (422), NOT encrypted verbatim. REST credentials are always a JSON
    object; a bare string would make ``_normalise_auth`` (which calls ``.get()``)
    blow up on the first run. The raw-token path is legit only for non-REST
    connectors (e.g. github), which the test above covers."""
    existing = _make_rest_connector(_encrypt_creds({"auth_mode": "api_key", "api_key": "secret123"}))
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": "raw-token-value"},
        )
    assert resp.status_code == 422
    assert "must be a JSON object" in resp.json()["detail"]
    mock_update.assert_not_awaited()


def test_create_rest_bearer_without_token_rejected(client: TestClient) -> None:
    """POST a REST connector with auth_mode=bearer but NO token must be rejected
    (422) and NOT saved. REST credentials are validated at the create boundary so
    a direct POST cannot persist a broken credential the connector rejects at run
    time."""
    body = {
        "name": "REST Connector",
        "connector_type_id": "rest",
        "credentials": json.dumps({"auth_mode": "bearer"}),
        "config_json": {},
    }
    with (
        patch("modulo.api.routes.connectors.create_connector_instance") as mock_create,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=body)
    assert resp.status_code == 422
    assert "REST bearer auth requires creds['token']" in resp.json()["detail"]
    mock_create.assert_not_awaited()


def test_create_rest_bearer_with_token_succeeds(client: TestClient) -> None:
    """POST a REST connector with a complete bearer credential succeeds (201)."""
    body = {
        "name": "REST Connector",
        "connector_type_id": "rest",
        "credentials": json.dumps({"auth_mode": "bearer", "token": "t"}),
        "config_json": {},
    }
    connector = _make_rest_connector(b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=body)
    assert resp.status_code == 201
    assert resp.json()["has_credentials"] is True


def test_update_connector_null_credentials_skips_write(client: TestClient) -> None:
    """FAR-495 QA regression: PATCH {"credentials": null} must NOT 500. An explicit
    ``null`` credential is treated as "no credential change" — the stored secret is
    left intact (never wiped or re-encrypted) and the request succeeds. The
    historical bug was ``_encrypt(None, ...)`` raising ``AttributeError`` → unhandled
    500; the fix is to skip the credential write, not to reject with 422."""
    existing = _make_connector()  # has stored credentials
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": None})
    assert resp.status_code == 200
    assert "credentials_ciphertext" not in captured["updates"], (
        "Explicit null credentials must NOT wipe the stored secret"
    )


def test_connector_response_surfaces_degraded_markers(client: TestClient) -> None:
    """FAR-495 read path: degraded_at / last_skip_error written by
    ``mark_instances_degraded`` must be surfaced on the GET connector response
    (operators can see broken connectors), not left as write-only columns."""
    degraded = _make_connector()
    degraded.degraded_at = _NOW
    degraded.last_skip_error = "ValueError: Missing credential key 'token'"

    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=degraded,
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded_at"] is not None
    assert body["degraded_at"].startswith("2025-01-01T00:00:00")
    assert body["last_skip_error"] == "ValueError: Missing credential key 'token'"
