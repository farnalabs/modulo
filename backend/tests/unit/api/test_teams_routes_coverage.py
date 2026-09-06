"""Route-level coverage tests for the team endpoints (FAR-618).

Complements ``test_teams.py`` (CRUD happy paths, RBAC/last-operator guards,
optimistic locking) by covering the per-route DB error-convention matrices
(IntegrityError→409, ProgrammingError→501, SQLAlchemyError→503, generic
Exception→500), the team-has-resources delete guard, the whole
``reassign-org`` endpoint, the team-role ceiling 422 branches in the
membership helpers, and the update-team 404 on a None row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.crud.team import TeamUpdateOutcome
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
_MEMBERSHIP_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_INTEGRITY = IntegrityError("s", {}, Exception())
_RUNTIME = RuntimeError("kaboom")

_PREFIX = "modulo.api.routes.teams."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session(*, count_result: int = 0) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        # Count queries (delete resource checks, last-operator guard) read
        # scalar_one/scalar; everything else is an empty read.
        result.scalar_one.return_value = count_result
        result.scalar_one_or_none.return_value = None
        result.scalar.return_value = count_result
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        result.scalars.return_value.first.return_value = None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _assert_error_matrix(
    http: TestClient,
    *,
    method: str,
    url: str,
    json_body: dict | None,
    patch_target: str,
    expected: dict,
    extra_patches: tuple = (),
) -> None:
    for exc, status_code in expected:
        with patch(f"{_PREFIX}{patch_target}", new=AsyncMock(side_effect=exc)) as _p:
            ctxs = [patch(f"{_PREFIX}{target}", **kwargs) for target, kwargs in extra_patches]
            for c in ctxs:
                c.__enter__()
            try:
                kwargs = {"json": json_body} if json_body is not None else {}
                resp = getattr(http, method.lower())(url, **kwargs)
            finally:
                for c in reversed(ctxs):
                    c.__exit__(None, None, None)
        assert resp.status_code == status_code, f"{patch_target} {exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# GET /teams/my — error mapping
# ---------------------------------------------------------------------------


def test_my_teams_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url="/api/v1/teams/my",
        json_body=None,
        patch_target="list_team_memberships_for_account",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# GET /teams — error mapping (IntegrityError maps to 409 here)
# ---------------------------------------------------------------------------


def test_list_teams_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url="/api/v1/teams",
        json_body=None,
        patch_target="list_teams",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# POST /teams — duplicate name 409 + error mapping
# ---------------------------------------------------------------------------


def test_create_team_duplicate_name_returns_409(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    existing = MagicMock()
    existing.id = uuid.uuid4()
    with patch(f"{_PREFIX}get_team_by_name", new=AsyncMock(return_value=existing)):
        resp = http.post("/api/v1/teams", json={"name": "dup"})

    assert resp.status_code == 409, resp.text
    assert "already exists" in resp.json()["detail"]


def test_create_team_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="POST",
        url="/api/v1/teams",
        json_body={"name": "New Team"},
        patch_target="create_team",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_team_by_name", {"new": AsyncMock(return_value=None)}),),
    )


def test_create_team_audit_failure_does_not_block(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    team = MagicMock()
    team.id = _TEAM_ID
    team.name = "New Team"
    team.description = None
    team.account_id = _USER_ID
    team.created_at = None
    with (
        patch(f"{_PREFIX}get_team_by_name", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}create_team", new=AsyncMock(return_value=team)),
        patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))),
    ):
        resp = http.post("/api/v1/teams", json={"name": "New Team"})

    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# GET /teams/{team_id} — error mapping
# ---------------------------------------------------------------------------


def test_get_team_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url=f"/api/v1/teams/{_TEAM_ID}",
        json_body=None,
        patch_target="get_team",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# PATCH /teams/{team_id} — error mapping + None-row 404
# ---------------------------------------------------------------------------


def test_update_team_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="PATCH",
        url=f"/api/v1/teams/{_TEAM_ID}",
        json_body={"name": "Renamed"},
        patch_target="_apply_team_update",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


async def test_apply_team_update_success_none_row_returns_404() -> None:
    from modulo.api.routes.teams import _apply_team_update

    session = _make_session()
    with (
        patch(f"{_PREFIX}update_team_if_unchanged", new=AsyncMock(return_value=(TeamUpdateOutcome.UPDATED, None))),
        pytest.raises(HTTPException, match="Team not found"),
    ):
        await _apply_team_update(session, _ORG_ID, _TEAM_ID, {"name": "x"}, "2025-01-01T00:00:00Z")


def test_update_team_request_accepts_explicit_none_name() -> None:
    from modulo.api.routes.teams import UpdateTeamRequest

    req = UpdateTeamRequest(name=None, description="d")
    assert req.name is None


# ---------------------------------------------------------------------------
# DELETE /teams/{team_id} — resource guard + error mapping
# ---------------------------------------------------------------------------


def test_delete_team_with_owned_resources_returns_409() -> None:
    session = _make_session(count_result=2)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        http = TestClient(app)
        resp = http.delete(f"/api/v1/teams/{_TEAM_ID}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert "team_has_resources" in resp.json()["detail"]
    assert "2 pipeline(s)" in resp.json()["detail"]


def test_delete_team_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="DELETE",
        url=f"/api/v1/teams/{_TEAM_ID}",
        json_body=None,
        patch_target="delete_team",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# POST /teams/{team_id}/reassign-org — full endpoint coverage
# ---------------------------------------------------------------------------


def test_reassign_team_resources_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_team", new=AsyncMock(return_value=MagicMock())),
        patch(f"{_PREFIX}reassign_team_resources_to_org", new=AsyncMock(return_value=(7, []))),
    ):
        resp = http.post(f"/api/v1/teams/{_TEAM_ID}/reassign-org")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["team_id"] == str(_TEAM_ID)
    assert body["reassigned"] == 7


def test_reassign_team_resources_unknown_team_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}get_team", new=AsyncMock(return_value=None)):
        resp = http.post(f"/api/v1/teams/{_TEAM_ID}/reassign-org")

    assert resp.status_code == 404, resp.text


def test_reassign_team_resources_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="POST",
        url=f"/api/v1/teams/{_TEAM_ID}/reassign-org",
        json_body=None,
        patch_target="reassign_team_resources_to_org",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_team", {"new": AsyncMock(return_value=MagicMock())}),),
    )


# ---------------------------------------------------------------------------
# Membership endpoints — error mapping
# ---------------------------------------------------------------------------


def test_list_members_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url=f"/api/v1/teams/{_TEAM_ID}/members",
        json_body=None,
        patch_target="list_team_members",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_team", {"new": AsyncMock(return_value=MagicMock(organisation_id=_ORG_ID))}),),
    )


def test_add_member_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="POST",
        url=f"/api/v1/teams/{_TEAM_ID}/members",
        json_body={"user_id": str(_USER_ID), "role": "viewer"},
        patch_target="_add_team_member_checked",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_team", {"new": AsyncMock(return_value=MagicMock())}),),
    )


def test_remove_member_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="DELETE",
        url=f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
        json_body=None,
        patch_target="_remove_member_checked",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_change_member_role_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="PATCH",
        url=f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
        json_body={"role": "viewer"},
        patch_target="_change_member_role_checked",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_team", {"new": AsyncMock(return_value=MagicMock())}),),
    )


# ---------------------------------------------------------------------------
# Helper branches
# ---------------------------------------------------------------------------


def test_remove_sole_operator_membership_allowed(client: tuple[TestClient, AsyncMock]) -> None:
    """Removing the ONLY member (an operator) skips the last-operator guard."""
    http, _session = client
    membership = MagicMock()
    membership.team_id = _TEAM_ID
    membership.role = "operator"
    membership.account_id = _USER_ID
    with (
        patch(f"{_PREFIX}_load_team_membership", new=AsyncMock(return_value=membership)),
        patch(f"{_PREFIX}remove_team_member", new=AsyncMock(return_value=None)),
    ):
        resp = http.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")

    assert resp.status_code == 204, resp.text


def test_add_member_role_above_operator_caller_ceiling_returns_422() -> None:
    """A team-operator caller cannot grant a role above their own team role."""
    import asyncio

    from modulo.api.routes.teams import _add_team_member_checked

    caller = TenantPrincipal(username="op@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="operator")
    runner_membership = MagicMock()
    runner_membership.role = "runner"

    async def _run() -> None:
        session = _make_session()
        with patch(f"{_PREFIX}_require_team_operator_caller", new=AsyncMock(return_value=runner_membership)):
            await _add_team_member_checked(session, caller, _TEAM_ID, _USER_ID, "operator")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 422
    assert "above your own team role 'runner'" in str(excinfo.value.detail)


def test_change_role_above_operator_caller_ceiling_returns_422() -> None:
    """A team-operator caller cannot promote a member above their own role."""
    import asyncio

    from modulo.api.routes.teams import _change_member_role_checked

    caller = TenantPrincipal(username="op@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="operator")
    runner_membership = MagicMock()
    runner_membership.role = "runner"

    async def _run() -> None:
        session = _make_session()
        with patch(f"{_PREFIX}_require_team_operator_caller", new=AsyncMock(return_value=runner_membership)):
            await _change_member_role_checked(session, caller, _TEAM_ID, _MEMBERSHIP_ID, "operator")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 422
    assert "above your own team role 'runner'" in str(excinfo.value.detail)
