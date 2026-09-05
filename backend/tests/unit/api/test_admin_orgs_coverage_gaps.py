"""Targeted coverage tests for ``modulo.api.routes.admin_orgs`` (FAR-573).

Complements test_admin_orgs.py by exercising the endpoints and branches the
existing suite leaves uncovered: org listing (orphan hidden), org deletion,
per-org license get/set/remove, the authz-enforce flag, the triggers-pause
kill-switch, the guardrails kill-switch, and the DB error mapping convention.

Unit tier: sessions are AsyncMock doubles, CRUD functions are patched at the
route-module seam, payloads use the module's real request models. The
system-admin principal bypasses the live-role read in the authz dependencies.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

import modulo.api.routes.admin_orgs as admin_orgs
from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.organisation import ORPHAN_ORG_ID, Organisation
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TARGET_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)

_DB_ERROR_PARAMS = [
    # admin_orgs endpoints catch SQLAlchemyError themselves (no IntegrityError
    # clause), so IntegrityError lands in 503 before handle_db_errors sees it.
    pytest.param(IntegrityError("stmt", {}, Exception("dup")), 503, id="integrity-503"),
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _result(
    *,
    scalar: Any = 0,
    scalar_one: Any = 0,
    scalar_one_or_none: Any = None,
    rows: list[Any] | None = None,
    scalars: list[Any] | None = None,
    rowcount: int = 0,
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
    r.rowcount = rowcount
    return r


def _make_session(results: list[MagicMock] | None = None) -> AsyncMock:
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
    session.execute = AsyncMock(return_value=_result())
    if results is not None:
        queue = list(results)

        async def _execute(*_a: object, **_kw: object) -> MagicMock:
            return queue.pop(0) if len(queue) > 1 else queue[0]

        session.execute = AsyncMock(side_effect=_execute)
    return session


def _org(org_id: Any = None, **overrides: Any) -> Organisation:
    return Organisation(
        id=overrides.get("id", org_id if org_id is not None else _TARGET_ORG_ID),
        name=overrides.get("name", "Target Org"),
        slug=overrides.get("slug", "target-org"),
        status=overrides.get("status", "active"),
        created_at=overrides.get("created_at", _NOW),
        settings_json=overrides.get("settings_json"),
    )


def _org_mock(**overrides: Any) -> MagicMock:
    org = MagicMock()
    org.id = overrides.get("id", _TARGET_ORG_ID)
    org.name = overrides.get("name", "Target Org")
    org.slug = overrides.get("slug", "target-org")
    org.status = overrides.get("status", "active")
    org.plan_id = overrides.get("plan_id")
    org.settings_json = overrides.get("settings_json")
    org.created_at = overrides.get("created_at", _NOW)
    org.triggers_paused = overrides.get("triggers_paused", False)
    org.triggers_paused_at = overrides.get("triggers_paused_at")
    org.guardrails_kill_switch = overrides.get("guardrails_kill_switch", False)
    org.guardrails_kill_switch_at = overrides.get("guardrails_kill_switch_at")
    return org


def _account(**overrides: Any) -> MagicMock:
    account = MagicMock()
    account.id = overrides.get("id", uuid.uuid4())
    account.email = overrides.get("email", "newuser@example.com")
    account.display_name = overrides.get("display_name", "New User")
    account.auth_provider = overrides.get("auth_provider", "local")
    account.password_hash = overrides.get("password_hash")
    account.created_at = overrides.get("created_at", _NOW)
    return account


def _build_client(session: AsyncMock, role: str = "admin") -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )
    return TestClient(app)


@pytest.fixture
def api() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    client = _build_client(session, role="admin")
    yield client, session
    app.dependency_overrides.clear()


# ── POST "" (create org) ──


def test_create_org_seed_failure_is_fail_open(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation_by_slug", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_orgs, "create_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "seed_cost_components_for_org", AsyncMock(side_effect=RuntimeError("seed blip")))
    resp = client.post("/api/v1/admin/orgs", json={"name": "Test Org", "slug": "test-org"})
    assert resp.status_code == 201
    assert resp.json()["slug"] == "target-org"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_create_org_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation_by_slug", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_orgs, "create_organisation", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/orgs", json={"name": "Test Org", "slug": "test-org"})
    assert resp.status_code == expected


def test_create_org_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation_by_slug", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_orgs, "create_organisation", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post("/api/v1/admin/orgs", json={"name": "Test Org", "slug": "test-org"})
    assert resp.status_code == 500


# ── GET "" (list orgs) ──


def test_list_orgs_hides_orphan_org(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    orphan = _org(org_id=ORPHAN_ORG_ID, name="orphan", slug="orphan")
    monkeypatch.setattr(admin_orgs, "list_organisations", AsyncMock(return_value=[_org(), orphan]))
    resp = client.get("/api/v1/admin/orgs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["slug"] == "target-org"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_list_orgs_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "list_organisations", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/orgs")
    assert resp.status_code == expected


def test_list_orgs_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "list_organisations", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/orgs")
    assert resp.status_code == 500


# ── DELETE /{org_id} ──


def test_delete_org_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "delete_organisation", AsyncMock(return_value=True))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}")
    assert resp.status_code == 204


def test_delete_org_missing_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}")
    assert resp.status_code == 404


def test_delete_org_delete_falsy_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "delete_organisation", AsyncMock(return_value=False))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_delete_org_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(side_effect=exc))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}")
    assert resp.status_code == expected


def test_delete_org_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "delete_organisation", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}")
    assert resp.status_code == 500


# ── POST /{org_id}/users (extra branches) ──


def test_create_org_user_membership_conflict_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "get_account_by_email", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin_orgs, "get_membership_by_account_and_org", AsyncMock(return_value=MagicMock()))
    resp = client.post(
        f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/users",
        json={"email": "newuser@example.com", "display_name": "New", "password": "Sup3r-Secret!", "org_role": "runner"},
    )
    assert resp.status_code == 409
    assert "already exists in this organisation" in resp.json()["detail"]


def test_create_org_user_cross_tenant_local_account_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    existing = _account(password_hash="$2b$12$hash", auth_provider="local")
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "get_account_by_email", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin_orgs, "get_membership_by_account_and_org", AsyncMock(return_value=None))
    resp = client.post(
        f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/users",
        json={"email": "newuser@example.com", "display_name": "New", "password": "Sup3r-Secret!", "org_role": "runner"},
    )
    assert resp.status_code == 409
    assert "EMAIL_ACCOUNT_EXISTS" in resp.json()["detail"]


def test_create_org_user_adopts_passwordless_account(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    existing = _account(password_hash=None, auth_provider="sso")
    membership = MagicMock()
    membership.role = "runner"
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "get_account_by_email", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin_orgs, "get_membership_by_account_and_org", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_orgs, "create_membership", AsyncMock(return_value=membership))
    resp = client.post(
        f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/users",
        json={"email": "newuser@example.com", "display_name": "New", "password": "Sup3r-Secret!", "org_role": "runner"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "newuser@example.com"
    assert data["auth_provider"] == "sso"


def test_create_org_user_weak_password_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.post(
        f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/users",
        json={"email": "newuser@example.com", "display_name": "New", "password": "12345678", "org_role": "runner"},
    )
    assert resp.status_code == 422
    assert "entropy" in resp.json()["detail"]


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_create_org_user_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "get_account_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_orgs, "create_account", AsyncMock(side_effect=exc))
    resp = client.post(
        f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/users",
        json={"email": "newuser@example.com", "display_name": "New", "password": "Sup3r-Secret!", "org_role": "runner"},
    )
    assert resp.status_code == expected


def test_create_org_user_unexpected_error_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org()))
    monkeypatch.setattr(admin_orgs, "get_account_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_orgs, "create_account", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post(
        f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/users",
        json={"email": "newuser@example.com", "display_name": "New", "password": "Sup3r-Secret!", "org_role": "runner"},
    )
    assert resp.status_code == 500


# ── GET/PUT/DELETE /{org_id}/license ──


def _license_data() -> SimpleNamespace:
    return SimpleNamespace(
        tier="pro", features=["f1"], expires_at="2026-01-01T00:00:00+00:00", org_id=str(_TARGET_ORG_ID)
    )


def test_get_org_license_from_org_key(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org(settings_json={"license_key": "org-key"})
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(return_value=SimpleNamespace(valid=True, license_data=_license_data(), error=None)),
    )
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_license"] is True
    assert data["tier"] == "pro"


def test_get_org_license_falls_back_to_system_license(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    org = _org(settings_json={"license_key": "invalid-org-key"})
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(return_value=SimpleNamespace(valid=False, license_data=None, error="bad signature")),
    )
    monkeypatch.setattr("modulo.core.license.get_license", MagicMock(return_value=_license_data()))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == 200
    assert resp.json()["tier"] == "pro"


def test_get_org_license_no_license(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org(settings_json=None)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr("modulo.core.license.get_license", MagicMock(return_value=None))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_license"] is False
    assert data["tier"] == "community"


def test_get_org_license_missing_org_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_org_license_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(side_effect=exc))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == expected


def test_set_org_license_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org(settings_json={})
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(return_value=SimpleNamespace(valid=True, license_data=_license_data(), error=None)),
    )
    update = AsyncMock(return_value=org)
    monkeypatch.setattr(admin_orgs, "update_organisation", update)
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license", json={"license_key": "valid-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_license"] is True
    assert data["tier"] == "pro"
    persisted = update.call_args.args[2]
    assert persisted["settings_json"]["license_key"] == "valid-key"


def test_set_org_license_invalid_key_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org(settings_json={})))
    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(return_value=SimpleNamespace(valid=False, license_data=None, error="expired")),
    )
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license", json={"license_key": "expired-key"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "expired"


def test_set_org_license_parse_raises_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org(settings_json={})))
    monkeypatch.setattr("modulo.core.license.parse_and_verify", MagicMock(side_effect=ValueError("malformed key")))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license", json={"license_key": "junk"})
    assert resp.status_code == 422


def test_set_org_license_missing_org_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license", json={"license_key": "k"})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_set_org_license_update_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org(settings_json={})))
    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(return_value=SimpleNamespace(valid=True, license_data=_license_data(), error=None)),
    )
    monkeypatch.setattr(admin_orgs, "update_organisation", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license", json={"license_key": "k"})
    assert resp.status_code == expected


def test_set_org_license_update_unexpected_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=_org(settings_json={})))
    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(return_value=SimpleNamespace(valid=True, license_data=_license_data(), error=None)),
    )
    monkeypatch.setattr(admin_orgs, "update_organisation", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license", json={"license_key": "k"})
    assert resp.status_code == 500


def test_remove_org_license_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org(settings_json={"license_key": "k", "other": 1})
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    update = AsyncMock(return_value=org)
    monkeypatch.setattr(admin_orgs, "update_organisation", update)
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_license"] is False
    persisted = update.call_args.args[2]
    assert "license_key" not in persisted["settings_json"]
    assert persisted["settings_json"]["other"] == 1


def test_remove_org_license_missing_org_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_remove_org_license_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(side_effect=exc))
    resp = client.delete(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/license")
    assert resp.status_code == expected


# ── PATCH /{org_id}/authz-enforce ──


def test_set_org_authz_enforce_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(rowcount=1))
    resp = client.patch(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/authz-enforce", json={"enforce": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["enforce"] is False
    assert data["org_id"] == str(_TARGET_ORG_ID)


def test_set_org_authz_enforce_missing_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(rowcount=0))
    resp = client.patch(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/authz-enforce", json={"enforce": True})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_set_org_authz_enforce_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.patch(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/authz-enforce", json={"enforce": True})
    assert resp.status_code == expected


def test_set_org_authz_enforce_unexpected_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.patch(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/authz-enforce", json={"enforce": True})
    assert resp.status_code == 500


# ── PUT /{org_id}/triggers/pause ──


def test_set_org_triggers_paused_enable(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org_mock(triggers_paused=False)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(admin_orgs, "append_audit_event", AsyncMock())
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/triggers/pause", json={"paused": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["paused"] is True
    assert org.triggers_paused is True
    assert org.triggers_paused_at is not None


def test_set_org_triggers_paused_idempotent_noop(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    paused_at = datetime.now(UTC)
    org = _org_mock(triggers_paused=True, triggers_paused_at=paused_at)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/triggers/pause", json={"paused": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["paused"] is True
    assert data["paused_at"] == paused_at.isoformat()


def test_set_org_triggers_paused_audit_failure_is_fail_open(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    org = _org_mock(triggers_paused=False)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(admin_orgs, "append_audit_event", AsyncMock(side_effect=SQLAlchemyError("boom")))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/triggers/pause", json={"paused": True})
    assert resp.status_code == 200
    assert org.triggers_paused is True


def test_set_org_triggers_paused_missing_org_404(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/triggers/pause", json={"paused": True})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_set_org_triggers_paused_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "set_rls_org", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/triggers/pause", json={"paused": True})
    assert resp.status_code == expected


def test_set_org_triggers_paused_unexpected_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "set_rls_org", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/triggers/pause", json={"paused": True})
    assert resp.status_code == 500


# ── GET/PUT /{org_id}/guardrails/kill-switch ──


def test_get_org_guardrails_kill_switch_on(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    switched_at = datetime.now(UTC)
    org = _org_mock(guardrails_kill_switch=True, guardrails_kill_switch_at=switched_at)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["enabled_at"] == switched_at.isoformat()


def test_get_org_guardrails_kill_switch_off(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org_mock(guardrails_kill_switch=False, guardrails_kill_switch_at=None)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["enabled_at"] is None


def test_get_org_guardrails_kill_switch_missing_org_404(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_org_guardrails_kill_switch_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "set_rls_org", AsyncMock(side_effect=exc))
    resp = client.get(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch")
    assert resp.status_code == expected


def test_set_org_guardrails_kill_switch_enable_notifies(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    org = _org_mock(guardrails_kill_switch=False)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(admin_orgs, "append_audit_event", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr("modulo.core.guardrails.notify_guardrail_event", notify)
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch", json={"enabled": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert org.guardrails_kill_switch is True
    assert notify.await_count == 1


def test_set_org_guardrails_kill_switch_disable_no_notify(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    org = _org_mock(guardrails_kill_switch=True, guardrails_kill_switch_at=datetime.now(UTC))
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    monkeypatch.setattr(admin_orgs, "append_audit_event", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr("modulo.core.guardrails.notify_guardrail_event", notify)
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch", json={"enabled": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert org.guardrails_kill_switch_at is None
    assert notify.await_count == 0


def test_set_org_guardrails_kill_switch_idempotent(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    switched_at = datetime.now(UTC)
    org = _org_mock(guardrails_kill_switch=True, guardrails_kill_switch_at=switched_at)
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=org))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled_at"] == switched_at.isoformat()


def test_set_org_guardrails_kill_switch_missing_org_404(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(return_value=None))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch", json={"enabled": True})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_set_org_guardrails_kill_switch_error_mapping(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: int
) -> None:
    client, _ = api
    monkeypatch.setattr(admin_orgs, "set_rls_org", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/orgs/{_TARGET_ORG_ID}/guardrails/kill-switch", json={"enabled": True})
    assert resp.status_code == expected
