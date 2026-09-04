"""Targeted coverage tests for ``modulo.api.routes.admin_sso`` (FAR-573).

Complements test_admin_sso.py by exercising the error-mapping branches the
existing suite leaves uncovered (DB errors on every endpoint, the 400
empty-update guard, the 404 paths), the SsoProviderResponse validators
(scopes / client_secret normalisation), and the OIDC/SAML connection-test
flows including SSRF rejection and metadata parsing.

Unit tier: sessions are AsyncMock doubles, CRUD and outbound-HTTP seams are
patched at the route-module boundary, payloads use the real request models.
"""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

import modulo.api.routes.admin_sso as admin_sso
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_system_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)

_DB_ERROR_PARAMS = [
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
    pytest.param(RuntimeError("boom"), 500, id="unexpected-500"),
]

_SAML_XML_WITH_BINDING = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example.com/entity">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <md:KeyInfo>
        <md:X509Data>
          <md:X509Certificate>MIICertificateDataMIICertificateDataMIICertificateDataAA</md:X509Certificate>
        </md:X509Data>
      </md:KeyInfo>
    </md:KeyDescriptor>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

_SAML_XML_NO_BINDING = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example.com/entity">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" Location="https://idp.example.com/sso-post"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

_SAML_XML_NO_DESCRIPTOR = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example.com/entity">
</md:EntityDescriptor>"""


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _result(
    *,
    scalar: int = 0,
    scalar_one: object = True,
    scalar_one_or_none: object = None,
    rows: list | None = None,
    scalars: list | None = None,
) -> MagicMock:
    r = MagicMock()
    r.scalar = MagicMock(return_value=scalar)
    r.scalar_one = MagicMock(return_value=scalar_one)
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.all = MagicMock(return_value=rows if rows is not None else [])
    sc = MagicMock()
    sc.all = MagicMock(return_value=scalars if scalars is not None else [])
    sc.__iter__.return_value = iter(scalars if scalars is not None else [])
    r.scalars = MagicMock(return_value=sc)
    r.first = MagicMock(return_value=None)
    return r


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=_result())
    return session


def _provider(**overrides: object) -> MagicMock:
    provider = MagicMock()
    provider.id = overrides.get("id", _PROVIDER_ID)
    provider.provider_type = overrides.get("provider_type", "oidc")
    provider.provider_id = overrides.get("provider_id", "test-oidc-provider")
    provider.name = overrides.get("name", "Test OIDC Provider")
    provider.client_id = overrides.get("client_id", "test-client-id")
    provider.client_secret = overrides.get("client_secret", "test-client-secret")
    provider.discovery_url = overrides.get("discovery_url", "https://idp.example.com/.well-known/openid-configuration")
    provider.metadata_url = overrides.get("metadata_url")
    provider.metadata_xml = overrides.get("metadata_xml")
    provider.entity_id = overrides.get("entity_id")
    provider.scopes = overrides.get("scopes", json.dumps(["openid", "profile", "email"]))
    provider.enabled = overrides.get("enabled", True)
    provider.auto_provision = overrides.get("auto_provision", True)
    provider.default_role = overrides.get("default_role", "runner")
    provider.group_mappings = overrides.get("group_mappings", [])
    provider.created_at = _NOW
    provider.updated_at = _NOW
    return provider


def _http_client(get_json: dict | None = None, get_text: str = "") -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=get_json if get_json is not None else {})
    resp.text = get_text
    client.get = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    client.timeout = None
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _build_client(session: AsyncMock, system_session: AsyncMock, role: str = "admin") -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
        is_system_admin=False,
    )
    return TestClient(app)


@pytest.fixture
def api() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    system_session = _make_session()
    client = _build_client(session, system_session, role="admin")
    yield client, session
    app.dependency_overrides.clear()


# ── GET /providers + response-model validators ──


def test_get_providers_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "list_providers", AsyncMock(return_value=[_provider()]))
    resp = client.get("/api/v1/admin/sso/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["provider_id"] == "test-oidc-provider"
    assert data[0]["client_secret"] == _MASKED_SECRET


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param(json.dumps(["openid"]), ["openid"], id="json-string"),
        pytest.param("not-json", None, id="invalid-json-string"),
        pytest.param([], [], id="empty-list"),
        pytest.param(["a", "b"], ["a", "b"], id="string-list"),
        pytest.param(123, None, id="non-list"),
    ],
)
def test_get_providers_scopes_validator(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, scopes: object, expected: object
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "list_providers", AsyncMock(return_value=[_provider(scopes=scopes)]))
    resp = client.get("/api/v1/admin/sso/providers")
    assert resp.status_code == 200
    assert resp.json()[0]["scopes"] == expected


# NOTE: the response model's SensitiveValue type masks any non-empty secret
# as bullets in the JSON payload, so "configured" serialises as bullets;
# an empty secret serialises as "" (nothing to mask).
_MASKED_SECRET = "\u2022\u2022\u2022\u2022\u2022\u2022"


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param("raw", _MASKED_SECRET, id="string"),
        pytest.param("", "", id="empty-string"),
        pytest.param(b"raw-bytes", _MASKED_SECRET, id="bytes"),
    ],
)
def test_get_providers_client_secret_validator(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, secret: object, expected: object
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "list_providers", AsyncMock(return_value=[_provider(client_secret=secret)]))
    resp = client.get("/api/v1/admin/sso/providers")
    assert resp.status_code == 200
    assert resp.json()[0]["client_secret"] == expected


def test_get_providers_client_secret_unsupported_type_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted row (non-str secret) fails response serialisation as 500."""
    client, _ = api
    monkeypatch.setattr(admin_sso, "list_providers", AsyncMock(return_value=[_provider(client_secret=123)]))
    resp = client.get("/api/v1/admin/sso/providers")
    assert resp.status_code == 500


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_providers_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "list_providers", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/sso/providers")
    assert resp.status_code == expected


# ── POST /providers (create) ──


def _create_payload() -> dict:
    return {
        "provider_type": "oidc",
        "name": "New Provider",
        "provider_id": "new-oidc",
        "client_id": "cid",
        "client_secret": "sec",
        "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    }


def test_create_provider_value_error_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "create_provider", AsyncMock(side_effect=ValueError("bad config")))
    resp = client.post("/api/v1/admin/sso/providers", json=_create_payload())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "bad config"


def test_create_provider_integrity_error_provider_id_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    exc = IntegrityError("stmt", {}, Exception("duplicate key uq_sso_providers_provider_id"))
    monkeypatch.setattr(admin_sso, "create_provider", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/sso/providers", json=_create_payload())
    assert resp.status_code == 409
    assert "provider ID" in resp.json()["detail"]


def test_create_provider_integrity_error_name_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    exc = IntegrityError("stmt", {}, Exception("duplicate key value violates unique constraint"))
    monkeypatch.setattr(admin_sso, "create_provider", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/sso/providers", json=_create_payload())
    assert resp.status_code == 409
    assert "name already exists" in resp.json()["detail"]


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_create_provider_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "create_provider", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/sso/providers", json=_create_payload())
    assert resp.status_code == expected


def test_create_provider_invalid_type_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    payload = _create_payload()
    payload["provider_type"] = "ldap"
    resp = client.post("/api/v1/admin/sso/providers", json=payload)
    assert resp.status_code == 422


# ── PUT /providers/{id} (update) ──


def test_update_provider_empty_body_400(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "No fields to update"


def test_update_provider_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "update_provider", AsyncMock(return_value=None))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}", json={"name": "Renamed"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "SSO provider not found"


def test_update_provider_value_error_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "update_provider", AsyncMock(side_effect=ValueError("bad role")))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}", json={"name": "Renamed"})
    assert resp.status_code == 409


def test_update_provider_integrity_error_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    exc = IntegrityError("stmt", {}, Exception("duplicate key"))
    monkeypatch.setattr(admin_sso, "update_provider", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}", json={"name": "Renamed"})
    assert resp.status_code == 409
    assert "name already exists" in resp.json()["detail"]


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_provider_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "update_provider", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}", json={"name": "Renamed"})
    assert resp.status_code == expected


# ── DELETE /providers/{id} ──


def test_delete_provider_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "delete_provider", AsyncMock(return_value=False))
    resp = client.delete(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}")
    assert resp.status_code == 404


def test_delete_provider_integrity_error_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    exc = IntegrityError("stmt", {}, Exception("fk violation"))
    monkeypatch.setattr(admin_sso, "delete_provider", AsyncMock(side_effect=exc))
    resp = client.delete(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}")
    assert resp.status_code == 409


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_delete_provider_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "delete_provider", AsyncMock(side_effect=exc))
    resp = client.delete(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}")
    assert resp.status_code == expected


# ── PUT /providers/{id}/toggle ──


def test_toggle_provider_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "toggle_provider", AsyncMock(return_value=None))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
    assert resp.status_code == 404


def test_toggle_provider_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "toggle_provider", AsyncMock(return_value=_provider(enabled=False)))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_toggle_provider_integrity_error_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    exc = IntegrityError("stmt", {}, Exception("fk violation"))
    monkeypatch.setattr(admin_sso, "toggle_provider", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
    assert resp.status_code == 409


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_toggle_provider_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "toggle_provider", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
    assert resp.status_code == expected


# ── POST /providers/{id}/test (OIDC) ──


def _patch_oidc_fetch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: MagicMock | None = None,
    client_error: Exception | None = None,
    validate_error: Exception | None = None,
) -> MagicMock:
    validate = AsyncMock(side_effect=validate_error) if validate_error else AsyncMock()
    monkeypatch.setattr(admin_sso, "validate_outbound_url_async", validate)
    if client_error is not None:
        fetch = AsyncMock(side_effect=client_error)
    else:
        fetch = AsyncMock(return_value=client if client is not None else _http_client())
    monkeypatch.setattr(admin_sso, "pinned_async_client", fetch)
    return fetch


_DISCOVERY_DOC = {
    "issuer": "https://idp.example.com",
    "authorization_endpoint": "https://idp.example.com/auth",
    "token_endpoint": "https://idp.example.com/token",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
    "jwks_uri": "https://idp.example.com/jwks",
    "scopes_supported": ["openid", "email"],
}


def _oidc_provider(**overrides: object) -> MagicMock:
    return _provider(provider_type="oidc", **overrides)


def test_test_connection_provider_missing_404(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=None))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_test_connection_db_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(side_effect=exc))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == expected


def test_test_connection_oidc_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=_oidc_provider(client_id="cid")))
    _patch_oidc_fetch(monkeypatch, client=_http_client(get_json=_DISCOVERY_DOC))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["provider_info"]["authorization_endpoint"] == "https://idp.example.com/auth"
    assert data["provider_info"]["client_id_validated"] is True


def test_test_connection_oidc_missing_discovery_url(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=_oidc_provider(discovery_url=None)))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Discovery URL is required" in data["message"]


def test_test_connection_oidc_ssrf_rejected(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=_oidc_provider()))
    _patch_oidc_fetch(monkeypatch, validate_error=ValueError("private address"))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Rejected" in data["message"]


def test_test_connection_oidc_fetch_failure(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=_oidc_provider()))
    _patch_oidc_fetch(monkeypatch, client_error=ValueError("conn refused"))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["message"] == "Failed to fetch discovery document"


def test_test_connection_oidc_missing_authorization_endpoint(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=_oidc_provider()))
    _patch_oidc_fetch(monkeypatch, client=_http_client(get_json={"issuer": "https://idp"}))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "authorization_endpoint" in data["message"]


def test_test_connection_generic_exception_reported(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=_oidc_provider()))
    _patch_oidc_fetch(monkeypatch, client_error=RuntimeError("explode"))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "explode" in data["message"]


# ── POST /providers/{id}/test (SAML) ──


def _saml_provider(**overrides: object) -> MagicMock:
    return _provider(provider_type="saml", **overrides)


def test_test_connection_saml_success_with_metadata_xml(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=_SAML_XML_WITH_BINDING)
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["provider_info"]["entity_id"] == "https://idp.example.com/entity"
    assert data["provider_info"]["sso_url"] == "https://idp.example.com/sso"
    assert data["provider_info"]["certificates"][0]["use"] == "signing"


def test_test_connection_saml_redirect_binding_fallback(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=_SAML_XML_NO_BINDING)
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["provider_info"]["sso_url"] == "https://idp.example.com/sso-post"


def test_test_connection_saml_no_metadata_422_message(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=None, metadata_url=None)
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Metadata URL or Metadata XML is required" in data["message"]


def test_test_connection_saml_metadata_url_fetch_success(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=None, metadata_url="https://idp.example.com/metadata")
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    _patch_oidc_fetch(monkeypatch, client=_http_client(get_text=_SAML_XML_WITH_BINDING))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["provider_info"]["entity_id"] == "https://idp.example.com/entity"


def test_test_connection_saml_metadata_url_ssrf_rejected(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=None, metadata_url="https://idp.example.com/metadata")
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    _patch_oidc_fetch(monkeypatch, validate_error=ValueError("private address"))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Rejected" in data["message"]


def test_test_connection_saml_metadata_url_fetch_failure(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=None, metadata_url="https://idp.example.com/metadata")
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    _patch_oidc_fetch(monkeypatch, client_error=ValueError("conn refused"))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["message"] == "Failed to fetch metadata"


def test_test_connection_saml_invalid_xml(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml="<not-xml")
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Failed to parse metadata XML" in data["message"]


def test_test_connection_saml_no_sso_descriptor(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    provider = _saml_provider(metadata_xml=_SAML_XML_NO_DESCRIPTOR)
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "No IDPSSODescriptor found" in data["message"]


# ── Group mappings ──


_MAPPINGS = [{"idp_group": "eng", "team_id": "team-1", "team_role": "viewer"}]


def test_set_group_mappings_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    provider = _provider(group_mappings=_MAPPINGS)
    monkeypatch.setattr(admin_sso, "set_group_mappings", AsyncMock(return_value=provider))
    resp = client.put(
        f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
        json={"mappings": _MAPPINGS},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mappings"][0]["idp_group"] == "eng"


def test_set_group_mappings_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "set_group_mappings", AsyncMock(return_value=None))
    resp = client.put(
        f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
        json={"mappings": _MAPPINGS},
    )
    assert resp.status_code == 404


def test_set_group_mappings_integrity_error_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    exc = IntegrityError("stmt", {}, Exception("fk violation"))
    monkeypatch.setattr(admin_sso, "set_group_mappings", AsyncMock(side_effect=exc))
    resp = client.put(
        f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
        json={"mappings": _MAPPINGS},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_set_group_mappings_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "set_group_mappings", AsyncMock(side_effect=exc))
    resp = client.put(
        f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
        json={"mappings": _MAPPINGS},
    )
    assert resp.status_code == expected


def test_get_group_mappings_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    provider = _provider(group_mappings=_MAPPINGS)
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=provider))
    resp = client.get(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mappings"][0]["team_id"] == "team-1"


def test_get_group_mappings_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(return_value=None))
    resp = client.get(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_group_mappings_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_sso, "get_provider", AsyncMock(side_effect=exc))
    resp = client.get(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings")
    assert resp.status_code == expected
