"""Targeted coverage tests for ``modulo.api.routes.admin`` (FAR-573).

Complements test_admin.py by exercising endpoints and error branches the
existing suite leaves uncovered: org profile, regenerate-api-key, user
management (deactivate/reactivate/reset-password branches), team management
error mappings, queue metrics, billing overview, the org-deletion family,
eval regressions / OKR progress, publishers, retention/purge, sandbox/run
concurrency, storage info, and overdue HITL claims.

Unit tier: no DB - sessions are AsyncMock doubles, CRUD functions are patched
at the route-module seam, payloads use the real request models.
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

import modulo.api.routes.admin as admin
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.crud.team import TeamUpdateOutcome
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TARGET_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
_PUBLISHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)
_VALID_32 = "a" * 32

_DB_ERROR_PARAMS = [
    pytest.param(IntegrityError("stmt", {}, Exception("dup")), 409, id="integrity-409"),
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
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
    """A mocked execute() result covering every accessor the routes use."""
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


def _make_session(execute_results: list[MagicMock] | None = None) -> AsyncMock:
    """Mock session usable by routes that call ``set_rls_org`` in a transaction.

    The sqlite dialect keeps ``set_rls_org`` off the execute() call path, so
    per-test execute queues only carry the route's own statements.
    """
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
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    session.get = AsyncMock(return_value=None)
    if execute_results is None:
        session.execute = AsyncMock(return_value=_result())
    else:
        results = list(execute_results)

        async def _execute(*_a: object, **_kw: object) -> MagicMock:
            return results.pop(0) if len(results) > 1 else results[0]

        session.execute = AsyncMock(side_effect=_execute)
    return session


def _account(**overrides: Any) -> MagicMock:
    account = MagicMock()
    account.id = overrides.get("id", _TARGET_USER_ID)
    account.email = overrides.get("email", "target@test")
    account.display_name = overrides.get("display_name", "Target")
    account.active = overrides.get("active", True)
    account.is_break_glass = overrides.get("is_break_glass", False)
    account.auth_provider = overrides.get("auth_provider", "local")
    account.password_hash = overrides.get("password_hash", "$2b$12$existing")
    account.must_change_password = False
    account.created_at = overrides.get("created_at", _NOW)
    account.last_login = overrides.get("last_login")
    return account


def _membership(role: str = "runner", deactivated_at: Any = None) -> MagicMock:
    m = MagicMock()
    m.role = role
    m.deactivated_at = deactivated_at
    return m


def _team(**overrides: Any) -> MagicMock:
    team = MagicMock()
    team.id = overrides.get("id", _TEAM_ID)
    team.name = overrides.get("name", "team-a")
    team.description = overrides.get("description")
    team.account_id = overrides.get("account_id", _USER_ID)
    team.organisation_id = overrides.get("organisation_id", _ORG_ID)
    team.created_at = overrides.get("created_at", _NOW)
    team.updated_at = overrides.get("updated_at", _NOW)
    return team


def _publisher(**overrides: Any) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PUBLISHER_ID)
    p.name = overrides.get("name", "pub")
    p.contact_email = overrides.get("contact_email", "pub@test")
    p.public_key_hex = overrides.get("public_key_hex", "ab" * 32)
    p.trust_tier = overrides.get("trust_tier", "amber")
    p.verified_since = overrides.get("verified_since")
    p.website_url = overrides.get("website_url")
    p.created_at = overrides.get("created_at", _NOW)
    p.updated_at = overrides.get("updated_at", _NOW)
    return p


def _build_client(session: AsyncMock, role: str = "admin") -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
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
    client = _build_client(session, role="admin")
    yield client, session
    app.dependency_overrides.clear()


@pytest.fixture
def viewer() -> Generator[TestClient, None, None]:
    client = _build_client(_make_session(), role="viewer")
    yield client
    app.dependency_overrides.clear()


# ── Global search ──


def test_global_search_programming_error_501(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    resp = client.get("/api/v1/admin/search", params={"q": "needle"})
    assert resp.status_code == 501


# ── POST /users (create user) ──


def test_create_user_weak_password_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.post(
        "/api/v1/admin/users",
        json={"email": "new@test", "display_name": "New", "password": "12345678", "org_role": "runner"},
    )
    assert resp.status_code == 422
    assert "entropy" in resp.json()["detail"]


# NOTE: admin_create_user catches ProgrammingError/SQLAlchemyError itself and
# has no IntegrityError clause, so IntegrityError lands in the 503 mapping
# before handle_db_errors could turn it into a 409.
_CREATE_USER_ERROR_PARAMS = [
    pytest.param(IntegrityError("stmt", {}, Exception("dup")), 503, id="integrity-503"),
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
]


@pytest.mark.parametrize(("exc", "expected"), _CREATE_USER_ERROR_PARAMS)
def test_create_user_db_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_email", AsyncMock(side_effect=exc))
    resp = client.post(
        "/api/v1/admin/users",
        json={"email": "new@test", "display_name": "New", "password": "Sup3r-Secret!", "org_role": "runner"},
    )
    assert resp.status_code == expected


# ── POST /teams (create team) + audit-event branches ──


def test_create_team_duplicate_name_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=_team()))
    resp = client.post("/api/v1/admin/teams", json={"name": "team-a"})
    assert resp.status_code == 409


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_create_team_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "create_team", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/teams", json={"name": "team-a"})
    assert resp.status_code == expected


def test_create_team_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "create_team", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post("/api/v1/admin/teams", json={"name": "team-a"})
    assert resp.status_code == 500


def test_create_team_audit_integrity_error_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "create_team", AsyncMock(return_value=_team()))
    monkeypatch.setattr(admin, "append_audit_event", AsyncMock(side_effect=IntegrityError("s", {}, Exception("x"))))
    resp = client.post("/api/v1/admin/teams", json={"name": "team-a"})
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "exc",
    [ProgrammingError("s", {}, Exception("x")), SQLAlchemyError("boom")],
)
def test_create_team_audit_failure_is_fail_open(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "create_team", AsyncMock(return_value=_team()))
    monkeypatch.setattr(admin, "append_audit_event", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/teams", json={"name": "team-a"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "team-a"


# ── GET/PUT /org (org profile) ──


def _org(settings_json: dict[str, Any] | None = None) -> Organisation:
    return Organisation(
        id=_ORG_ID,
        name="Org",
        slug="org",
        status="active",
        created_at=_NOW,
        settings_json=settings_json,
    )


def test_get_org_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_organisation", AsyncMock(return_value=_org({"logo_url": "https://x/l.png"})))
    resp = client.get("/api/v1/admin/org")
    assert resp.status_code == 200
    data = resp.json()
    assert data["logo_url"] == "https://x/l.png"
    assert data["slug"] == "org"


def test_get_org_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_organisation", AsyncMock(return_value=None))
    resp = client.get("/api/v1/admin/org")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_org_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_organisation", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/org")
    assert resp.status_code == expected


def test_update_org_logo_and_plan(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    org = _org({"logo_url": "https://old/l.png"})
    monkeypatch.setattr(admin, "get_organisation", AsyncMock(return_value=org))

    async def _update(session: Any, org_id: Any, updates: dict[str, Any]) -> Organisation:
        org.settings_json = updates.get("settings_json", org.settings_json)
        org.plan_id = updates.get("plan_id", org.plan_id)
        return org

    monkeypatch.setattr(admin, "update_organisation", _update)
    resp = client.put(
        "/api/v1/admin/org",
        json={"name": "Renamed", "logo_url": "https://new/l.png", "plan_id": "pro"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["logo_url"] == "https://new/l.png"
    assert data["plan_id"] == "pro"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_org_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_organisation", AsyncMock(side_effect=exc))
    resp = client.put("/api/v1/admin/org", json={"name": "Renamed"})
    assert resp.status_code == expected


# ── POST /org/regenerate-api-key ──


def test_regenerate_api_key_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(
        "modulo.auth.api_key.create_api_key", AsyncMock(return_value=(MagicMock(), "mk_abcdef1234567890"))
    )
    resp = client.post("/api/v1/admin/org/regenerate-api-key")
    assert resp.status_code == 200
    data = resp.json()
    assert data["api_key"] == "mk_abcdef1234567890"
    assert data["lookup_prefix"] == "abcdef12"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_regenerate_api_key_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.auth.api_key.create_api_key", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/org/regenerate-api-key")
    assert resp.status_code == expected


# ── GET /users (list users) ──


def test_list_users_with_search_and_role_filters(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    account = _account()
    membership = _membership(role="runner")
    results = [_result(scalar=1), _result(rows=[(account, membership)])]

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if len(results) > 1 else results[0]

    session.execute = AsyncMock(side_effect=_execute)
    resp = client.get("/api/v1/admin/users", params={"page": 2, "page_size": 10, "search": "target", "role": "runner"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["page"] == 2
    assert data["items"][0]["email"] == "target@test"
    assert data["items"][0]["org_role"] == "runner"


def test_list_users_programming_error_501(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 501


# ── PUT /users/{user_id} ──


def test_update_user_account_missing_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=None))
    resp = client.put(f"/api/v1/admin/users/{_TARGET_USER_ID}", json={"org_role": "viewer"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


def test_update_user_last_admin_guard_unavailable_503(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(
        admin, "assert_not_last_admin", AsyncMock(side_effect=admin.LastAdminLockoutUnavailableError(org_id=_ORG_ID))
    )
    resp = client.put(f"/api/v1/admin/users/{_TARGET_USER_ID}", json={"org_role": "viewer"})
    assert resp.status_code == 503


def test_update_user_programming_error_501(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(
        admin, "assert_not_last_admin", AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    )
    resp = client.put(f"/api/v1/admin/users/{_TARGET_USER_ID}", json={"org_role": "viewer"})
    assert resp.status_code == 501


# ── POST /users/{user_id}/deactivate ──


def test_deactivate_break_glass_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account(is_break_glass=True)))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 422


def test_deactivate_membership_missing_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=None))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found in this organisation"


def test_deactivate_last_admin_lockout_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(
        admin,
        "assert_not_last_admin",
        AsyncMock(side_effect=admin.LastAdminLockoutError(org_id=_ORG_ID, reason="last admin")),
    )
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "last admin"


def test_deactivate_guard_unavailable_503(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(
        admin, "assert_not_last_admin", AsyncMock(side_effect=admin.LastAdminLockoutUnavailableError(org_id=_ORG_ID))
    )
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 503


def test_deactivate_integrity_error_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(
        admin, "list_team_memberships_for_account", AsyncMock(side_effect=IntegrityError("s", {}, Exception("x")))
    )
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 409


def test_deactivate_programming_error_501(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(
        admin, "list_team_memberships_for_account", AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    )
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 501


class _PgCodeError(Exception):
    pgcode = "M2010"


class _PgWrappedSQLAlchemyError(SQLAlchemyError):
    """SQLAlchemyError whose ``.orig`` carries the SECURITY DEFINER pgcode."""

    def __init__(self, orig: BaseException) -> None:
        super().__init__("wrapped db error")
        self.orig = orig


def test_deactivate_pgcode_m2010_403(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(
        admin,
        "list_team_memberships_for_account",
        AsyncMock(side_effect=_PgWrappedSQLAlchemyError(_PgCodeError("denied"))),
    )
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 403


def test_deactivate_sqlalchemy_error_503(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(admin, "list_team_memberships_for_account", AsyncMock(side_effect=SQLAlchemyError("boom")))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 503


def test_deactivate_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(admin, "list_team_memberships_for_account", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 500


def test_deactivate_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    account = _account()
    membership = _membership(role="runner")
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=account))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=membership))
    monkeypatch.setattr(admin, "assert_not_last_admin", AsyncMock())
    monkeypatch.setattr(admin, "list_team_memberships_for_account", AsyncMock(return_value=[]))
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", AsyncMock())
    # The SECURITY DEFINER tombstones the membership; session.refresh is what
    # surfaces the new deactivated_at on the ORM object (per-org is_active).
    session.refresh = AsyncMock(side_effect=lambda obj, *a, **k: setattr(obj, "deactivated_at", _NOW))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/deactivate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(_TARGET_USER_ID)
    assert data["is_active"] is False


# ── POST /users/{user_id}/reactivate ──


def test_reactivate_account_missing_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=None))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reactivate")
    assert resp.status_code == 404


def test_reactivate_break_glass_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account(is_break_glass=True)))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reactivate")
    assert resp.status_code == 422


def test_reactivate_membership_missing_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=None))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reactivate")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found in this organisation"


def test_reactivate_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    account = _account()
    membership = _membership(role="runner")
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=account))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=membership))
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", AsyncMock())
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_reactivate_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", AsyncMock(side_effect=exc))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reactivate")
    assert resp.status_code == expected


# ── POST /users/{user_id}/reset-password ──


def test_reset_password_account_missing_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=None))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reset-password")
    assert resp.status_code == 404


def test_reset_password_membership_missing_404(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=None))
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reset-password")
    assert resp.status_code == 404


def test_reset_password_programming_error_501(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=_account()))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(
        admin, "list_families_for_account", AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    )
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reset-password")
    assert resp.status_code == 501


def test_reset_password_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    account = _account()
    monkeypatch.setattr(admin, "get_account_by_id", AsyncMock(return_value=account))
    monkeypatch.setattr(admin, "get_membership_by_account_and_org", AsyncMock(return_value=_membership()))
    monkeypatch.setattr(admin, "list_families_for_account", AsyncMock(return_value=[]))
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", AsyncMock())
    resp = client.post(f"/api/v1/admin/users/{_TARGET_USER_ID}/reset-password")
    assert resp.status_code == 200
    data = resp.json()
    assert data["temporary_password"]
    assert account.must_change_password is True


# ── Team list / update / delete ──


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_list_teams_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "list_teams", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/teams")
    assert resp.status_code == expected


def test_list_teams_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "list_teams", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/teams")
    assert resp.status_code == 500


def test_update_team_name_conflict_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    other = _team(id=uuid.UUID("00000000-0000-0000-0000-0000000000b1"))
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=other))
    resp = client.put(f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "team-a"})
    assert resp.status_code == 409


def test_update_team_optimistic_not_found_404(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "update_team_if_unchanged", AsyncMock(return_value=(TeamUpdateOutcome.NOT_FOUND, None)))
    resp = client.put(
        f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "team-a", "expected_updated_at": _NOW.isoformat()}
    )
    assert resp.status_code == 404


def test_update_team_optimistic_stale_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "update_team_if_unchanged", AsyncMock(return_value=(TeamUpdateOutcome.STALE, None)))
    resp = client.put(
        f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "team-a", "expected_updated_at": _NOW.isoformat()}
    )
    assert resp.status_code == 409


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_team_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "crud_update_team", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "renamed"})
    assert resp.status_code == expected


def test_update_team_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "crud_update_team", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.put(f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "renamed"})
    assert resp.status_code == 500


def test_reassign_all_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team", AsyncMock(return_value=_team()))
    monkeypatch.setattr(admin, "reassign_team_resources_to_org", AsyncMock(return_value=(3, ["pipeline"])))
    resp = client.post(f"/api/v1/admin/teams/{_TEAM_ID}/reassign-all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reassigned"] == 3
    assert data["resource_types"] == ["pipeline"]


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_reassign_all_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team", AsyncMock(return_value=_team()))
    monkeypatch.setattr(admin, "reassign_team_resources_to_org", AsyncMock(side_effect=exc))
    resp = client.post(f"/api/v1/admin/teams/{_TEAM_ID}/reassign-all")
    assert resp.status_code == expected


def test_reassign_all_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_team", AsyncMock(return_value=_team()))
    monkeypatch.setattr(admin, "reassign_team_resources_to_org", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post(f"/api/v1/admin/teams/{_TEAM_ID}/reassign-all")
    assert resp.status_code == 500


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_delete_team_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "delete_team", AsyncMock(side_effect=exc))
    resp = client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")
    assert resp.status_code == expected


def test_delete_team_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "delete_team", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")
    assert resp.status_code == 500


def test_delete_team_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "delete_team", AsyncMock(return_value=False))
    resp = client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")
    assert resp.status_code == 404


def test_delete_team_audit_integrity_error_409(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "delete_team", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "modulo.core.audit_logger.append_audit_event", AsyncMock(side_effect=IntegrityError("s", {}, Exception("x")))
    )
    resp = client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")
    assert resp.status_code == 409


def test_delete_team_audit_programming_error_still_204(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "delete_team", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "modulo.core.audit_logger.append_audit_event", AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    )
    resp = client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")
    assert resp.status_code == 204


# ── GET /dashboard/summary (alias) ──


def test_admin_dashboard_summary_alias(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.api.routes.dashboard.dashboard_summary", AsyncMock(return_value={"runs": 4}))
    resp = client.get("/api/v1/admin/dashboard/summary")
    assert resp.status_code == 200
    assert resp.json() == {"runs": 4}


# ── GET /queues/metrics ──


def _redis_mock(llen_value: Any = 7) -> MagicMock:
    redis = MagicMock()
    redis.execute_command = AsyncMock(return_value=llen_value)
    redis.aclose = AsyncMock()
    return redis


def test_queue_metrics_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(_make_session(), role="admin")
    try:
        redis = _redis_mock(7)
        monkeypatch.setattr("redis.asyncio.Redis.from_url", MagicMock(return_value=redis))
        resp = client.get("/api/v1/admin/queues/metrics")
        data = resp.json()
    finally:
        app.dependency_overrides.clear()
    assert data["queues"]["runs"] == 7
    assert data["queues"]["system"] == 7


def test_queue_metrics_llen_failure_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(_make_session(), role="admin")
    try:
        redis = _redis_mock()
        redis.execute_command = AsyncMock(side_effect=RuntimeError("LLEN failed"))
        monkeypatch.setattr("redis.asyncio.Redis.from_url", MagicMock(return_value=redis))
        resp = client.get("/api/v1/admin/queues/metrics")
        data = resp.json()
    finally:
        app.dependency_overrides.clear()
    assert data["queues"]["runs"] == 0
    assert data["queues"]["system"] == 0


def test_queue_metrics_redis_unavailable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(_make_session(), role="admin")
    try:
        monkeypatch.setattr("redis.asyncio.Redis.from_url", MagicMock(side_effect=RuntimeError("no redis")))
        resp = client.get("/api/v1/admin/queues/metrics")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503


# ── GET /billing/overview ──


def test_billing_overview_org_missing_returns_404(
    api: tuple[TestClient, AsyncMock],
) -> None:
    """A missing org raises HTTPException(404), which must pass through the
    handler's except clauses unremapped instead of being converted to 503."""
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = client.get("/api/v1/admin/billing/overview")
    assert resp.status_code == 404


def test_billing_overview_unexpected_error_503(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_organisation", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/billing/overview")
    assert resp.status_code == 503


# ── Org deletion family ──


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_request_org_deletion_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.db.crud.org_deletion.request_org_deletion", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/org/deletion-request")
    assert resp.status_code == expected


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_confirm_org_deletion_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.db.crud.org_deletion.confirm_org_deletion", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": "tok"})
    assert resp.status_code == expected


def test_cancel_org_deletion_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(
        "modulo.db.crud.org_deletion.cancel_org_deletion", AsyncMock(return_value={"status": "cancelled"})
    )
    resp = client.patch("/api/v1/admin/org/deletion-cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_cancel_org_deletion_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.db.crud.org_deletion.cancel_org_deletion", AsyncMock(side_effect=exc))
    resp = client.patch("/api/v1/admin/org/deletion-cancel")
    assert resp.status_code == expected


def test_export_org_data_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    bundle = {
        "organisation": [
            {"id": str(_ORG_ID), "name": "Org", "slug": "org", "status": "active", "created_at": str(_NOW)}
        ],
        "exported_at": "2025-06-01T00:00:00+00:00",
    }
    monkeypatch.setattr("modulo.db.crud.org_deletion.export_org_data", AsyncMock(return_value=bundle))
    resp = client.get("/api/v1/admin/org/export")
    assert resp.status_code == 200
    data = resp.json()
    assert data["organisation"]["id"] == str(_ORG_ID)
    assert data["exported_at"] == "2025-06-01T00:00:00+00:00"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_export_org_data_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.db.crud.org_deletion.export_org_data", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/org/export")
    assert resp.status_code == expected


def test_delete_org_immediate_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(
        "modulo.db.crud.org_deletion.request_org_deletion",
        AsyncMock(return_value={"token": "tok", "export": {}}),
    )
    monkeypatch.setattr(
        "modulo.db.crud.org_deletion.confirm_org_deletion",
        AsyncMock(return_value={"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 2}),
    )
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", AsyncMock())
    resp = client.delete("/api/v1/admin/org")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted_organisation_id"] == str(_ORG_ID)
    assert data["hard_deleted_runs"] == 2


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_delete_org_immediate_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr("modulo.db.crud.org_deletion.request_org_deletion", AsyncMock(side_effect=exc))
    resp = client.delete("/api/v1/admin/org")
    assert resp.status_code == expected


# ── Eval dashboard / regressions / OKR ──


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_eval_dashboard_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "_eval_summary", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/evals/dashboard")
    assert resp.status_code == expected


def _alert() -> SimpleNamespace:
    return SimpleNamespace(
        eval_id=uuid.UUID("00000000-0000-0000-0000-0000000000d0"),
        eval_name="suite",
        prev_pass_rate=0.9,
        current_pass_rate=0.5,
        drop_pct=0.44,
        trend="declining",
        affected_run_ids=[uuid.UUID("00000000-0000-0000-0000-0000000000d1")],
    )


def test_eval_regressions_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "detect_regressions", AsyncMock(return_value=[_alert()]))
    resp = client.get(
        "/api/v1/admin/evals/regressions",
        params={"days": 14, "threshold": 0.2, "recent_window_ratio": 0.5, "trend": "declining"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_regressions"] == 1
    assert data["lookback_days"] == 14
    assert data["alerts"][0]["trend"] == "declining"
    assert data["alerts"][0]["affected_run_ids"] == ["00000000-0000-0000-0000-0000000000d1"]


def test_eval_regressions_invalid_trend_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.get("/api/v1/admin/evals/regressions", params={"trend": "sideways"})
    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_eval_regressions_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "detect_regressions", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/evals/regressions")
    assert resp.status_code == expected


def test_eval_regressions_unexpected_error_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "detect_regressions", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/evals/regressions")
    assert resp.status_code == 500


def test_eval_regressions_timeout_503(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "detect_regressions", AsyncMock(side_effect=TimeoutError()))
    resp = client.get("/api/v1/admin/evals/regressions")
    assert resp.status_code == 503


def _progress() -> SimpleNamespace:
    return SimpleNamespace(
        suite_id="suite-1",
        suite_name="Suite One",
        current_score=0.9,
        pass_threshold=0.8,
        trend=[],
        trend_direction="stable",
        days_to_target=None,
        breach=False,
    )


def test_okr_progress_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "track_okr_progress", AsyncMock(return_value=_progress()))
    resp = client.get("/api/v1/admin/evals/okr-progress/suite-1", params={"target_date": "2026-09-30"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["suite_id"] == "suite-1"
    assert data["current_score"] == pytest.approx(0.9)
    assert data["breach"] is False


def test_okr_progress_value_error_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "track_okr_progress", AsyncMock(side_effect=ValueError("unknown suite")))
    resp = client.get("/api/v1/admin/evals/okr-progress/suite-1")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_okr_progress_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "track_okr_progress", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/evals/okr-progress/suite-1")
    assert resp.status_code == expected


def test_okr_progress_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "track_okr_progress", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/evals/okr-progress/suite-1")
    assert resp.status_code == 500


# ── Publishers ──


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_list_publishers_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "list_publishers", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/publishers")
    assert resp.status_code == expected


def test_create_publisher_name_conflict_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_publisher_by_name", AsyncMock(return_value=_publisher()))
    resp = client.post("/api/v1/admin/publishers", json={"name": "pub", "public_key_hex": "ab" * 32})
    assert resp.status_code == 409


def test_create_publisher_key_conflict_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_publisher_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "get_publisher_by_key", AsyncMock(return_value=_publisher()))
    resp = client.post("/api/v1/admin/publishers", json={"name": "pub", "public_key_hex": "ab" * 32})
    assert resp.status_code == 409


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_create_publisher_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_publisher_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "get_publisher_by_key", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "create_publisher", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/publishers", json={"name": "pub", "public_key_hex": "ab" * 32})
    assert resp.status_code == expected


def test_create_publisher_value_error_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_publisher_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "get_publisher_by_key", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "create_publisher", AsyncMock(side_effect=ValueError("bad key")))
    resp = client.post("/api/v1/admin/publishers", json={"name": "pub", "public_key_hex": "ab" * 32})
    assert resp.status_code == 422


def test_update_publisher_key_conflict_409(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    other = _publisher(id=uuid.UUID("00000000-0000-0000-0000-0000000000c1"))
    monkeypatch.setattr(admin, "get_publisher_by_key", AsyncMock(return_value=other))
    resp = client.put(f"/api/v1/admin/publishers/{_PUBLISHER_ID}", json={"public_key_hex": "cd" * 32})
    assert resp.status_code == 409


def test_update_publisher_value_error_422(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_publisher_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "crud_update_publisher", AsyncMock(side_effect=ValueError("bad key")))
    resp = client.put(f"/api/v1/admin/publishers/{_PUBLISHER_ID}", json={"name": "renamed"})
    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_publisher_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_publisher_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(admin, "crud_update_publisher", AsyncMock(side_effect=exc))
    resp = client.put(f"/api/v1/admin/publishers/{_PUBLISHER_ID}", json={"name": "renamed"})
    assert resp.status_code == expected


def test_delete_publisher_not_found_404(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "crud_delete_publisher", AsyncMock(return_value=False))
    resp = client.delete(f"/api/v1/admin/publishers/{_PUBLISHER_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_delete_publisher_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "crud_delete_publisher", AsyncMock(side_effect=exc))
    resp = client.delete(f"/api/v1/admin/publishers/{_PUBLISHER_ID}")
    assert resp.status_code == expected


# ── Retention / purge ──


def test_retention_purge_runs_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "batch_delete_old_terminal_runs", AsyncMock(return_value=7))
    resp = client.post("/api/v1/admin/purge/runs", json={"max_age_days": 30})
    assert resp.status_code == 200
    assert resp.json()["deleted_run_count"] == 7


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_retention_purge_runs_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "batch_delete_old_terminal_runs", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/purge/runs", json={"max_age_days": 30})
    assert resp.status_code == expected


def test_retention_purge_runs_unexpected_error_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "batch_delete_old_terminal_runs", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post("/api/v1/admin/purge/runs", json={"max_age_days": 30})
    assert resp.status_code == 500


def test_manual_purge_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "purge_runs", AsyncMock(return_value={"purged": 3}))
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", AsyncMock())
    resp = client.post("/api/v1/admin/purge", json={"older_than": "2025-01-01"})
    assert resp.status_code == 200
    assert resp.json() == {"purged": 3}


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_manual_purge_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "purge_runs", AsyncMock(side_effect=exc))
    resp = client.post("/api/v1/admin/purge", json={"older_than": "2025-01-01"})
    assert resp.status_code == expected


def test_manual_purge_unexpected_error_500(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "purge_runs", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post("/api/v1/admin/purge", json={"older_than": "2025-01-01"})
    assert resp.status_code == 500


def test_purge_stale_runs_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(rowcount=5))
    resp = client.post("/api/v1/admin/runs/purge", json={"older_than_days": 90})
    assert resp.status_code == 200
    assert resp.json()["purged_count"] == 5


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_purge_stale_runs_error_mapping(
    api: tuple[TestClient, AsyncMock],
    exc: Exception,
    expected: int,
) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.post("/api/v1/admin/runs/purge", json={"older_than_days": 90})
    assert resp.status_code == expected


def test_purge_stale_runs_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.post("/api/v1/admin/runs/purge", json={"older_than_days": 90})
    assert resp.status_code == 500


def test_get_retention_string_setting(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none={"retention_days": "120"}))
    resp = client.get("/api/v1/admin/runs/retention")
    assert resp.status_code == 200
    assert resp.json()["retention_days"] == 120


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_retention_error_mapping(
    api: tuple[TestClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected: int,
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "set_rls_org", AsyncMock(side_effect=exc))
    resp = client.get("/api/v1/admin/runs/retention")
    assert resp.status_code == expected


def test_get_retention_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.get("/api/v1/admin/runs/retention")
    assert resp.status_code == 500


def test_update_retention_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    org = MagicMock()
    org.settings_json = None
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=org))
    resp = client.put("/api/v1/admin/runs/retention", json={"retention_days": 180})
    assert resp.status_code == 200
    assert resp.json()["retention_days"] == 180
    assert org.settings_json["retention_days"] == 180


def test_update_retention_org_missing_returns_404(api: tuple[TestClient, AsyncMock]) -> None:
    """A missing org raises HTTPException(404), which must pass through the
    handler's except clauses unremapped instead of being converted to 500."""
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = client.put("/api/v1/admin/runs/retention", json={"retention_days": 180})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_retention_error_mapping(
    api: tuple[TestClient, AsyncMock],
    exc: Exception,
    expected: int,
) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.put("/api/v1/admin/runs/retention", json={"retention_days": 180})
    assert resp.status_code == expected


# ── Sandbox / run concurrency ──


def test_get_sandbox_concurrency_unexpected_error_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_sandbox_concurrency_limit", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 500


def test_update_sandbox_concurrency_org_missing_returns_404(api: tuple[TestClient, AsyncMock]) -> None:
    """A missing org raises HTTPException(404), which must pass through the
    handler's except clauses unremapped instead of being converted to 500."""
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = client.put("/api/v1/admin/org/sandbox-concurrency", json={"sandbox_concurrency_limit": 4})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_sandbox_concurrency_error_mapping(
    api: tuple[TestClient, AsyncMock],
    exc: Exception,
    expected: int,
) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.put("/api/v1/admin/org/sandbox-concurrency", json={"sandbox_concurrency_limit": 4})
    assert resp.status_code == expected


def test_get_run_concurrency_unexpected_error_500(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "get_org_run_concurrency_limit", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 500


def test_update_run_concurrency_org_missing_returns_404(api: tuple[TestClient, AsyncMock]) -> None:
    """A missing org raises HTTPException(404), which must pass through the
    handler's except clauses unremapped instead of being converted to 500."""
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = client.put("/api/v1/admin/org/run-concurrency", json={"run_concurrency_limit": 4})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_run_concurrency_error_mapping(
    api: tuple[TestClient, AsyncMock],
    exc: Exception,
    expected: int,
) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.put("/api/v1/admin/org/run-concurrency", json={"run_concurrency_limit": 4})
    assert resp.status_code == expected


# ── GET /runs/storage ──


def test_get_storage_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    results = [
        _result(scalar=12),
        _result(rows=[SimpleNamespace(status="completed", cnt=8), SimpleNamespace(status="failed", cnt=4)]),
    ]

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if len(results) > 1 else results[0]

    session.execute = AsyncMock(side_effect=_execute)
    resp = client.get("/api/v1/admin/runs/storage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_runs"] == 12
    assert data["status_breakdown"]["completed"] == 8
    assert data["estimated_saved_bytes"] > 0


def test_get_storage_viewer_403(viewer: TestClient) -> None:
    resp = viewer.get("/api/v1/admin/runs/storage")
    assert resp.status_code == 403


def test_get_storage_programming_error_501(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x")))
    resp = client.get("/api/v1/admin/runs/storage")
    assert resp.status_code == 501


# ── GET /hitl/overdue ──


def test_overdue_claims_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = api
    claims = [
        {
            "id": "c1",
            "pipeline_run_id": "r1",
            "node_id": "n1",
            "created_at": "2025-06-01T00:00:00+00:00",
            "age_hours": 1.5,
            "status": "pending",
        }
    ]
    monkeypatch.setattr(admin, "get_overdue_claims", AsyncMock(return_value=claims))
    resp = client.get("/api/v1/admin/hitl/overdue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["claims"][0]["id"] == "c1"
    assert data["claims"][0]["age_hours"] == 1.5


def test_overdue_claims_viewer_403(viewer: TestClient) -> None:
    resp = viewer.get("/api/v1/admin/hitl/overdue")
    assert resp.status_code == 403


def test_overdue_claims_programming_error_501(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(admin, "set_rls_org", AsyncMock(side_effect=ProgrammingError("s", {}, Exception("x"))))
    resp = client.get("/api/v1/admin/hitl/overdue")
    assert resp.status_code == 501
