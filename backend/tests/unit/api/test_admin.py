"""Unit tests for /api/v1/admin endpoints (org deletion flow)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.team import TeamUpdateOutcome
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)
_TOKEN = "test-deletion-token-1234567890abcdef"
_TOKEN_EXPIRES = "2025-06-02T00:00:00+00:00"
_EXPORT = {
    "organisation": [
        {
            "id": str(_ORG_ID),
            "name": "Test Org",
            "slug": "test-org",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    ],
    "memberships": [{"id": str(_USER_ID), "email": "admin@test.com"}],
    "pipelines": [],
    "runs": [],
    "audit_events": [],
    "library_primitives": [],
    "connector_instances": [],
    "model_backends": [],
    "exported_at": "2025-06-01T12:00:00+00:00",
}
_OTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


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
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
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
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_rls_mock_session() -> AsyncMock:
    """Mock session usable by routes that call ``set_rls_org`` inside a transaction.

    ``set_rls_org``/``set_rls_user_context`` call ``session.in_transaction()`` and
    ``session.get_bind().dialect.name``; the plain ``_make_mock_session`` does not
    configure those, so the user offboarding routes (which run RLS setup inside
    ``async with session.begin()``) would raise RuntimeError.
    """
    session = _make_mock_session()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    return session


def _make_role_client(role: str, account_id: uuid.UUID) -> Generator[TestClient, None, None]:
    mock_session = _make_rls_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=account_id,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_rls_client() -> Generator[TestClient, None, None]:
    yield from _make_role_client("admin", _USER_ID)


@pytest.fixture
def operator_rls_client() -> Generator[TestClient, None, None]:
    yield from _make_role_client("operator", _USER_ID)


@pytest.fixture
def admin_rls_with_session() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    """``admin_rls_client`` paired with its mock session, for statement assertions."""
    mock_session = _make_rls_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), mock_session
    app.dependency_overrides.clear()


def _update_statements(mock_session: AsyncMock, table: str, marker: str) -> list[tuple[str, dict[str, Any]]]:
    """Compiled (sql, params) of every ``UPDATE <table> ... <marker>`` statement
    executed against the mock session (mirrors test_refresh_endpoint.py's
    blacklist-SQL capture pattern)."""
    pairs: list[tuple[str, dict[str, Any]]] = []
    for call in mock_session.execute.call_args_list:
        stmt = call.args[0] if call.args else None
        if stmt is None:
            continue
        try:
            compiled_obj = stmt.compile()
        except Exception:  # pragma: no cover - non-compilable debug object
            continue
        compiled = str(compiled_obj).lower()
        if f"update {table}" in compiled and marker in compiled:
            pairs.append((compiled, dict(compiled_obj.params)))
    return pairs


def _fake_membership(deactivated: bool = False) -> MagicMock:
    membership = MagicMock()
    membership.role = "runner"
    # FAR-533 per-org semantics: deactivation tombstones the caller's-org
    # membership (deactivated_at) and leaves accounts.active true; the route
    # refreshes the membership before serialising, so the fake mirrors the
    # post-refresh state the response is built from.
    membership.deactivated_at = _NOW if deactivated else None
    return membership


def _fake_offboarding_account(active: bool = False) -> MagicMock:
    account = MagicMock()
    account.id = _OTHER_USER_ID
    account.email = "user@test.com"
    account.display_name = "Test User"
    account.auth_provider = "local"
    account.created_at = _NOW
    account.last_login = None
    account.is_break_glass = False
    account.active = active
    return account


class TestUserDeactivateAuthorization:
    """Admin-only authorization for POST /admin/users/{id}/deactivate and /reactivate."""

    URL = "/api/v1/admin/users"

    def test_deactivate_non_admin_returns_403(self, operator_rls_client: TestClient) -> None:
        resp = operator_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")
        assert resp.status_code == 403

    def test_reactivate_non_admin_returns_403(self, operator_rls_client: TestClient) -> None:
        resp = operator_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/reactivate")
        assert resp.status_code == 403

    def test_self_deactivation_returns_422(self, admin_rls_client: TestClient) -> None:
        resp = admin_rls_client.post(f"{self.URL}/{_USER_ID}/deactivate")
        assert resp.status_code == 422
        assert "Cannot deactivate yourself" in resp.json()["detail"]

    def test_deactivate_unauthenticated_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")
        assert resp.status_code == 401

    def test_reactivate_unauthenticated_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"{self.URL}/{_OTHER_USER_ID}/reactivate")
        assert resp.status_code == 401

    def test_deactivate_malformed_uuid_returns_422(self, admin_rls_client: TestClient) -> None:
        resp = admin_rls_client.post(f"{self.URL}/not-a-uuid/deactivate")
        assert resp.status_code == 422

    def test_reactivate_malformed_uuid_returns_422(self, admin_rls_client: TestClient) -> None:
        resp = admin_rls_client.post(f"{self.URL}/not-a-uuid/reactivate")
        assert resp.status_code == 422

    def test_deactivate_removes_all_team_memberships(self, admin_rls_client: TestClient) -> None:
        membership_one = MagicMock()
        membership_one.id = uuid.uuid4()
        membership_two = MagicMock()
        membership_two.id = uuid.uuid4()
        fake_membership = MagicMock()
        fake_membership.role = "admin"

        with (
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account()),
            ),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=fake_membership),
            ),
            patch("modulo.api.routes.admin.assert_not_last_admin", AsyncMock()),
            patch(
                "modulo.api.routes.admin.list_team_memberships_for_account",
                AsyncMock(return_value=[membership_one, membership_two]),
            ),
            patch(
                "modulo.api.routes.admin.remove_team_member",
                AsyncMock(return_value=True),
            ) as remove_member,
            patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
        ):
            resp = admin_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")

        assert resp.status_code == 200
        assert remove_member.await_count == 2
        remove_member.assert_any_await(ANY, membership_one.id)
        remove_member.assert_any_await(ANY, membership_two.id)

    def test_deactivate_success_returns_inactive_user(self, admin_rls_client: TestClient) -> None:
        fake_membership = MagicMock()
        fake_membership.role = "admin"
        # Per-org deactivation (FAR-533): accounts.active stays true and the
        # CALLER'S-ORG membership carries the deactivated_at tombstone.
        fake_membership.deactivated_at = _NOW

        with (
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account(active=True)),
            ),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=fake_membership),
            ),
            patch("modulo.api.routes.admin.assert_not_last_admin", AsyncMock()),
            patch("modulo.api.routes.admin.list_team_memberships_for_account", AsyncMock(return_value=[])),
            patch("modulo.api.routes.admin.remove_team_member", AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
        ):
            resp = admin_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"] is False
        assert body["id"] == str(_OTHER_USER_ID)

    def test_reactivate_success_returns_active_user(self, admin_rls_client: TestClient) -> None:
        fake_membership = MagicMock()
        fake_membership.role = "admin"
        # FAR-533: reactivation clears the caller's-org tombstone only.
        fake_membership.deactivated_at = None

        with (
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account(active=True)),
            ),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=fake_membership),
            ),
            patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
        ):
            resp = admin_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/reactivate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"] is True


class TestUpdateUserDeactivationRevocation:
    """PUT is_active=False revokes token families + org API keys (FAR-537).

    The POST deactivate path revokes via the caller-bound
    ``deactivate_break_glass`` SECURITY DEFINER; the PUT path must perform the
    equivalent org-scoped revocations at the route layer so live access tokens
    cannot outlive a PUT deactivation until TTL. Assertions inspect the
    executed UPDATE statements (compiled SQL + bound params) because the unit
    harness mocks the session.
    """

    URL = "/api/v1/admin/users"

    def _resolved_user_patches(self, deactivated: bool = False) -> ExitStack:
        """Patch the PUT route's user/membership resolution.

        ``active=True`` always: FAR-533 per-org semantics â€” the PUT path never
        flips the account-global flag; the caller's-org membership tombstone
        (mirrored by *deactivated* on the fake) carries the deactivation.
        """
        stack = ExitStack()
        for ctx in (
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=_fake_membership(deactivated=deactivated)),
            ),
            patch("modulo.api.routes.admin.assert_not_last_admin", AsyncMock()),
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account(active=True)),
            ),
        ):
            stack.enter_context(ctx)
        return stack

    def test_put_deactivate_blacklists_token_families_scoped_to_caller_org(
        self, admin_rls_with_session: tuple[TestClient, AsyncMock]
    ) -> None:
        client, mock_session = admin_rls_with_session
        with self._resolved_user_patches(deactivated=True):
            resp = client.put(f"{self.URL}/{_OTHER_USER_ID}", json={"is_active": False})

        assert resp.status_code == 200
        family_updates = _update_statements(mock_session, "token_families", "is_blacklisted")
        assert len(family_updates) == 1
        compiled, params = family_updates[0]
        assert "is_blacklisted" in compiled
        # Idempotency guard: already-blacklisted families are never re-stamped.
        assert "is false" in compiled
        # Org-scoped to the caller's org, targeting only the deactivated account.
        assert str(_ORG_ID) in {str(v) for v in params.values()}
        assert str(_OTHER_USER_ID) in {str(v) for v in params.values()}

    def test_put_deactivate_revokes_live_org_api_keys_scoped_to_caller_org(
        self, admin_rls_with_session: tuple[TestClient, AsyncMock]
    ) -> None:
        client, mock_session = admin_rls_with_session
        with self._resolved_user_patches(deactivated=True):
            resp = client.put(f"{self.URL}/{_OTHER_USER_ID}", json={"is_active": False})

        assert resp.status_code == 200
        key_updates = _update_statements(mock_session, "org_api_keys", "revoked_at")
        assert len(key_updates) == 1
        compiled, params = key_updates[0]
        assert "revoked_at" in compiled
        # Idempotency guard: only live (non-revoked) keys are revoked.
        assert "is null" in compiled
        assert str(_ORG_ID) in {str(v) for v in params.values()}
        assert str(_OTHER_USER_ID) in {str(v) for v in params.values()}

    def test_double_deactivate_is_idempotent(self, admin_rls_with_session: tuple[TestClient, AsyncMock]) -> None:
        client, mock_session = admin_rls_with_session
        with self._resolved_user_patches(deactivated=True):
            first = client.put(f"{self.URL}/{_OTHER_USER_ID}", json={"is_active": False})
            second = client.put(f"{self.URL}/{_OTHER_USER_ID}", json={"is_active": False})

        assert first.status_code == 200
        assert second.status_code == 200
        # Both attempts carry the guards, so a re-deactivation matches zero
        # already-revoked rows instead of duplicating the revocation.
        for compiled, _ in _update_statements(mock_session, "token_families", "is_blacklisted"):
            assert "is false" in compiled
        for compiled, _ in _update_statements(mock_session, "org_api_keys", "revoked_at"):
            assert "is null" in compiled

    def test_put_reactivate_does_not_revoke(self, admin_rls_with_session: tuple[TestClient, AsyncMock]) -> None:
        client, mock_session = admin_rls_with_session
        with self._resolved_user_patches():
            resp = client.put(f"{self.URL}/{_OTHER_USER_ID}", json={"is_active": True})

        assert resp.status_code == 200
        # Revoked families/keys cannot be un-revoked; reactivation must not
        # emit any revocation-shaped statements (re-login re-mints).
        assert not _update_statements(mock_session, "token_families", "is_blacklisted")
        assert not _update_statements(mock_session, "org_api_keys", "revoked_at")

    def test_put_role_change_only_does_not_revoke(self, admin_rls_with_session: tuple[TestClient, AsyncMock]) -> None:
        client, mock_session = admin_rls_with_session
        with self._resolved_user_patches():
            resp = client.put(f"{self.URL}/{_OTHER_USER_ID}", json={"org_role": "runner"})

        assert resp.status_code == 200
        assert not _update_statements(mock_session, "token_families", "is_blacklisted")
        assert not _update_statements(mock_session, "org_api_keys", "revoked_at")


class TestDeletionRequest:
    URL = "/api/v1/admin/org/deletion-request"

    def test_admin_requests_deletion_returns_202(self, client: TestClient) -> None:
        crud_result = {
            "token": _TOKEN,
            "token_expires_at": _TOKEN_EXPIRES,
            "export": _EXPORT,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                return_value=crud_result,
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 202
        data = resp.json()
        assert data["token"] == _TOKEN
        assert data["token_expires_at"] == _TOKEN_EXPIRES
        assert data["export_summary"]["organisation"] == "Test Org"
        assert data["export_summary"]["user_count"] == 1

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL)
        assert resp.status_code in (401, 403)

    def test_already_deleted_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation is already deleted"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 409

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 409


class TestDeletionConfirm:
    URL = "/api/v1/admin/org/deletion-confirm"

    def test_admin_confirms_deletion_returns_200(self, client: TestClient) -> None:
        crud_result = {
            "deleted_organisation_id": str(_ORG_ID),
            "hard_deleted_runs": 5,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                return_value=crud_result,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 5
        assert "permanently deleted" in data["message"]

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code in (401, 403)

    def test_invalid_token_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Invalid deletion token"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": "wrong"})
        assert resp.status_code == 409

    def test_expired_token_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Deletion token has expired"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 409

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 409


class TestOrgExport:
    URL = "/api/v1/admin/org/export"

    def test_admin_exports_org_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.export_org_data",
                return_value=_EXPORT,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["organisation"]["name"] == "Test Org"
        assert data["organisation"]["status"] == "active"
        assert data["exported_at"] == "2025-06-01T12:00:00+00:00"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_org_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.export_org_data",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 404


class TestDeleteOrgImmediate:
    URL = "/api/v1/admin/org"

    def test_admin_deletes_org_returns_200(self, client: TestClient) -> None:
        request_result = {
            "token": _TOKEN,
            "token_expires_at": _TOKEN_EXPIRES,
            "export": _EXPORT,
        }
        confirm_result = {
            "deleted_organisation_id": str(_ORG_ID),
            "hard_deleted_runs": 0,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                return_value=request_result,
            ),
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                return_value=confirm_result,
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 0
        assert "permanently deleted" in data["message"]

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.delete(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete(self.URL)
        assert resp.status_code in (401, 403)

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 409

    def test_already_deleted_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation is already deleted"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 409


class TestAdminListTeamsOwnedResourceCount:
    """GET /api/v1/admin/teams includes owned_resource_count (PRD Â§9.3)."""

    URL = "/api/v1/admin/teams"

    def _mock_team(self, team_id: uuid.UUID, name: str) -> MagicMock:
        t = MagicMock()
        t.id = team_id
        t.organisation_id = _ORG_ID
        t.name = name
        t.description = None
        t.account_id = _USER_ID
        t.created_at = _NOW
        t.updated_at = _NOW
        return t

    def test_includes_owned_resource_count(self, admin_rls_client: TestClient) -> None:
        team_a = self._mock_team(uuid.uuid4(), "Team A")
        team_b = self._mock_team(uuid.uuid4(), "Team B")
        page_result = MagicMock(items=[team_a, team_b], total=2, page=1, page_size=20)

        # Configure the RLS mock session so the member-count GROUP BY query returns [team_a:3].
        from modulo.api.dependencies import get_db_session

        session = _make_mock_session()
        member_row = MagicMock()
        member_row.team_id = team_a.id
        member_row.cnt = 3
        member_count_result = MagicMock()
        member_count_result.all = MagicMock(return_value=[member_row])
        session.execute = AsyncMock(return_value=member_count_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        admin_rls_client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch("modulo.api.routes.admin.list_teams", new=AsyncMock(return_value=page_result)),
                patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
                patch(
                    "modulo.api.routes.admin.count_owned_resources",
                    new=AsyncMock(return_value={team_a.id: 4, team_b.id: 2}),
                ),
            ):
                resp = admin_rls_client.get(self.URL)
        finally:
            admin_rls_client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 200
        data = resp.json()
        by_name = {item["name"]: item for item in data["items"]}
        assert by_name["Team A"]["member_count"] == 3
        assert by_name["Team A"]["owned_resource_count"] == 4
        assert by_name["Team B"]["owned_resource_count"] == 2
        assert by_name["Team A"]["updated_at"]

    def test_operator_returns_403(self, operator_rls_client: TestClient) -> None:
        resp = operator_rls_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)


class TestAdminUpdateTeamOptimisticLock:
    """PUT /api/v1/admin/teams/{id} with expected_updated_at â€” optimistic concurrency."""

    URL = "/api/v1/admin/teams"

    def _mock_team(self, team_id: uuid.UUID, name: str) -> MagicMock:
        t = MagicMock()
        t.id = team_id
        t.organisation_id = _ORG_ID
        t.name = name
        t.description = None
        t.account_id = _USER_ID
        t.created_at = _NOW
        t.updated_at = _NOW
        return t

    def test_stale_expected_updated_at_returns_409(self, admin_rls_client: TestClient) -> None:
        team = self._mock_team(uuid.uuid4(), "Current")

        with (
            patch(
                "modulo.api.routes.admin.update_team_if_unchanged",
                new=AsyncMock(return_value=(TeamUpdateOutcome.STALE, None)),
            ),
            patch("modulo.api.routes.admin.get_team_by_name", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
        ):
            resp = admin_rls_client.put(
                f"{self.URL}/{team.id}",
                json={"name": "Renamed", "expected_updated_at": "2024-01-01T00:00:00+00:00"},
            )
        assert resp.status_code == 409
        assert "optimistic lock" in resp.json()["detail"].lower()

    def test_stale_expected_updated_at_does_not_update(self, admin_rls_client: TestClient) -> None:
        team = self._mock_team(uuid.uuid4(), "Current")

        with (
            patch(
                "modulo.api.routes.admin.update_team_if_unchanged",
                new=AsyncMock(return_value=(TeamUpdateOutcome.STALE, None)),
            ),
            patch("modulo.api.routes.admin.get_team_by_name", new=AsyncMock(return_value=None)),
            patch(
                "modulo.api.routes.admin.crud_update_team",
                new=AsyncMock(),
            ) as crud_update_mock,
            patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
        ):
            resp = admin_rls_client.put(
                f"{self.URL}/{team.id}",
                json={"name": "Renamed", "expected_updated_at": "2024-01-01T00:00:00+00:00"},
            )
        assert resp.status_code == 409
        crud_update_mock.assert_not_awaited()

    def test_missing_team_with_expected_updated_at_returns_404(self, admin_rls_client: TestClient) -> None:
        team = self._mock_team(uuid.uuid4(), "Current")

        with (
            patch(
                "modulo.api.routes.admin.update_team_if_unchanged",
                new=AsyncMock(return_value=(TeamUpdateOutcome.NOT_FOUND, None)),
            ),
            patch("modulo.api.routes.admin.get_team_by_name", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
        ):
            resp = admin_rls_client.put(
                f"{self.URL}/{team.id}",
                json={"name": "Renamed", "expected_updated_at": "2024-01-01T00:00:00+00:00"},
            )
        assert resp.status_code == 404

    def test_matching_expected_updated_at_succeeds(self, admin_rls_client: TestClient) -> None:
        team = self._mock_team(uuid.uuid4(), "Updated")
        expected = team.updated_at.isoformat()
        with (
            patch("modulo.api.routes.admin.get_team_by_name", new=AsyncMock(return_value=None)),
            patch(
                "modulo.api.routes.admin.update_team_if_unchanged",
                new=AsyncMock(return_value=(TeamUpdateOutcome.UPDATED, team)),
            ),
            patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
            patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
        ):
            resp = admin_rls_client.put(
                f"{self.URL}/{team.id}",
                json={"name": "Updated", "expected_updated_at": expected},
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"


class TestUpdateTeamIfUnchanged:
    """Direct CRUD tests of the atomic optimistic-lock update."""

    @pytest.mark.asyncio
    async def test_matching_timestamp_updates_and_returns_team(self) -> None:
        team = MagicMock()
        team.id = uuid.uuid4()
        team.updated_at = _NOW

        session = _make_mock_session()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=team)
        session.execute = AsyncMock(return_value=result)

        from modulo.db.crud.team import update_team_if_unchanged

        outcome, returned = await update_team_if_unchanged(
            session,
            team.id,
            {"name": "Renamed"},
            _NOW.isoformat(),
        )
        assert outcome is TeamUpdateOutcome.UPDATED
        assert returned is team

    @pytest.mark.asyncio
    async def test_stale_timestamp_returns_stale_without_update(self) -> None:
        team = MagicMock()
        team.id = uuid.uuid4()
        team.updated_at = _NOW

        session = _make_mock_session()
        update_result = MagicMock()
        update_result.scalar_one_or_none = MagicMock(return_value=None)
        exists_result = MagicMock()
        exists_result.scalar_one_or_none = MagicMock(return_value=team.id)
        session.execute = AsyncMock(side_effect=[update_result, exists_result])

        from modulo.db.crud.team import update_team_if_unchanged

        outcome, returned = await update_team_if_unchanged(
            session,
            team.id,
            {"name": "Renamed"},
            "2024-01-01T00:00:00+00:00",
        )
        assert outcome is TeamUpdateOutcome.STALE
        assert returned is None

    @pytest.mark.asyncio
    async def test_missing_team_returns_not_found(self) -> None:
        team = MagicMock()
        team.id = uuid.uuid4()
        team.updated_at = _NOW

        session = _make_mock_session()
        update_result = MagicMock()
        update_result.scalar_one_or_none = MagicMock(return_value=None)
        exists_result = MagicMock()
        exists_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(side_effect=[update_result, exists_result])

        from modulo.db.crud.team import update_team_if_unchanged

        outcome, returned = await update_team_if_unchanged(
            session,
            uuid.uuid4(),
            {"name": "Renamed"},
            _NOW.isoformat(),
        )
        assert outcome is TeamUpdateOutcome.NOT_FOUND
        assert returned is None

    @pytest.mark.asyncio
    async def test_unparseable_expected_timestamp_returns_stale(self) -> None:
        session = _make_mock_session()

        from modulo.db.crud.team import update_team_if_unchanged

        outcome, returned = await update_team_if_unchanged(
            session,
            uuid.uuid4(),
            {"name": "Renamed"},
            "not-a-timestamp",
        )
        assert outcome is TeamUpdateOutcome.STALE
        assert returned is None
        session.execute.assert_not_awaited()


@pytest.fixture
def admin_client_and_session() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), mock_session
    app.dependency_overrides.clear()


class TestAdminReassignAllTeamResources:
    """POST /api/v1/admin/teams/{id}/reassign-all (PRD 9.3 bulk reassignment)."""

    URL = f"/api/v1/admin/teams/{_ORG_ID}/reassign-all"

    def test_reassign_all_reports_per_resource_counts(
        self, admin_client_and_session: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = admin_client_and_session
        counts = iter([3, 1, 0, 2])

        def _execute(*args: object, **_kwargs: object) -> MagicMock:
            # The require_permission authz kill-switch read must not consume a
            # handler-side rowcount slot.
            stmt = args[0] if args else None
            if stmt is not None and "authz_enforce" in str(stmt):
                result = MagicMock()
                result.scalar_one_or_none.return_value = True
                return result
            # Extra calls beyond the four per-resource updates return rowcount 0.
            return MagicMock(rowcount=next(counts, 0))

        session.execute.side_effect = _execute
        team = MagicMock()
        team.id = _ORG_ID
        team.organisation_id = _ORG_ID
        with (
            patch("modulo.api.routes.admin.get_team", return_value=team),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.set_rls_user_context"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["reassigned"] == 6
        assert data["resource_types"] == ["pipeline", "connector", "library primitive"]

    def test_reassign_all_idempotent_when_team_has_no_resources(
        self, admin_client_and_session: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = admin_client_and_session
        session.execute.side_effect = lambda *_a, **_k: MagicMock(rowcount=0)
        team = MagicMock()
        team.id = _ORG_ID
        team.organisation_id = _ORG_ID
        with (
            patch("modulo.api.routes.admin.get_team", return_value=team),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.set_rls_user_context"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["reassigned"] == 0
        assert not data["resource_types"]

    def test_reassign_all_missing_team_returns_404(
        self, admin_client_and_session: tuple[TestClient, AsyncMock]
    ) -> None:
        client, _session = admin_client_and_session
        with (
            patch("modulo.api.routes.admin.get_team", return_value=None),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.set_rls_user_context"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 404

    def test_reassign_all_foreign_org_team_returns_404(
        self, admin_client_and_session: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = admin_client_and_session
        session.execute.side_effect = lambda *_a, **_k: MagicMock(rowcount=0)
        team = MagicMock()
        team.id = uuid.uuid4()
        team.organisation_id = uuid.uuid4()
        with (
            patch("modulo.api.routes.admin.get_team", return_value=team),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.set_rls_user_context"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 404

    def test_reassign_all_non_admin_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL)
        assert resp.status_code == 403


class TestBillingOverviewAggregation:
    """GET /api/v1/admin/billing/overview must aggregate counts across the org:
    memberships (users), teams, pipelines, and runs created this month."""

    URL = "/api/v1/admin/billing/overview"

    def test_aggregates_org_counts(self, client: TestClient) -> None:
        fake_org = MagicMock()
        fake_org.plan_id = "pro_monthly"
        fake_org.daily_spend_limit = 250.0
        fake_org.settings_json = {"license_key": "LIC-1234-ABCD"}

        counts = [7, 3, 12, 41]
        call_index = 0

        async def _execute(*_args, **_kwargs):
            nonlocal call_index
            result = MagicMock()
            result.scalar.return_value = counts[call_index]
            call_index += 1
            return result

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.execute = _execute

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        plan_ctx = MagicMock()
        plan_ctx.tier.return_value = "community"

        try:
            with (
                patch("modulo.api.routes.admin.set_rls_org"),
                patch("modulo.api.routes.admin.get_organisation", AsyncMock(return_value=fake_org)),
                patch("modulo.api.routes.admin.resolve_plan_context", new_callable=AsyncMock, return_value=plan_ctx),
            ):
                resp = client.get(self.URL)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_users"] == 7
        assert body["total_teams"] == 3
        assert body["total_pipelines"] == 12
        assert body["total_runs_this_month"] == 41
        assert body["daily_spend_limit"] == 250.0

    def test_zero_counts_when_org_empty(self, client: TestClient) -> None:
        fake_org = MagicMock()
        fake_org.plan_id = "community"
        fake_org.daily_spend_limit = None
        fake_org.settings_json = {}

        async def _execute(_stmt, *args, **kwargs):
            result = MagicMock()
            result.scalar.return_value = 0
            return result

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.execute = _execute

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        plan_ctx = MagicMock()
        plan_ctx.tier.return_value = "community"

        try:
            with (
                patch("modulo.api.routes.admin.set_rls_org"),
                patch("modulo.api.routes.admin.get_organisation", AsyncMock(return_value=fake_org)),
                patch("modulo.api.routes.admin.resolve_plan_context", new_callable=AsyncMock, return_value=plan_ctx),
            ):
                resp = client.get(self.URL)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_users"] == 0
        assert body["total_teams"] == 0
        assert body["total_pipelines"] == 0
        assert body["total_runs_this_month"] == 0
        assert body["daily_spend_limit"] is None


class TestOrgSlugImmutability:
    """The org slug is immutable once set â€” the profile update endpoint
    (PUT /api/v1/admin/org) must never change it."""

    URL = "/api/v1/admin/org"

    def test_update_org_ignores_slug_changes(self, client: TestClient) -> None:
        fake_org = MagicMock()
        fake_org.id = _ORG_ID
        fake_org.name = "Test Org"
        fake_org.slug = "immutable-slug"
        fake_org.settings_json = {}
        fake_org.plan_id = None
        fake_org.created_at = _NOW

        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch(
                "modulo.api.routes.admin.get_organisation",
                AsyncMock(return_value=fake_org),
            ),
            patch(
                "modulo.api.routes.admin.update_organisation",
                AsyncMock(return_value=fake_org),
            ) as mock_update,
        ):
            resp = client.put(self.URL, json={"name": "Renamed", "slug": "hacked-slug"})

        assert resp.status_code == 200
        body = resp.json()
        # The response keeps the immutable slug.
        assert body["slug"] == "immutable-slug"
        # The update call must never carry a slug key.
        for call in mock_update.call_args_list:
            updates = call.args[2]
            assert "slug" not in updates, f"update_organisation must not receive slug: {updates}"
            assert updates.get("name") == "Renamed"

    def test_update_org_model_has_no_slug_field(self) -> None:
        """The request model exposes no slug field â€” a client cannot even
        express a slug change."""
        from modulo.api.routes.admin import UpdateOrgRequest

        assert "slug" not in UpdateOrgRequest.model_fields
        assert set(UpdateOrgRequest.model_fields) <= {"name", "logo_url", "plan_id"}


def _fake_lifecycle_account(active: bool = True) -> MagicMock:
    """Account mock for create-user / reset-password lifecycle tests."""
    account = MagicMock()
    account.id = uuid.uuid4()
    account.email = "lifecycle@test.com"
    account.display_name = "Lifecycle User"
    account.auth_provider = "local"
    account.created_at = _NOW
    account.last_login = None
    account.is_break_glass = False
    account.active = active
    account.password_hash = None
    account.must_change_password = False
    return account


class TestAdminCreateUserAudit:
    """POST /api/v1/admin/users emits ``user_created_by_admin`` audit events."""

    URL = "/api/v1/admin/users"

    def test_create_user_emits_user_created_by_admin(self, client: TestClient) -> None:
        account = _fake_lifecycle_account()
        membership = MagicMock()
        membership.role = "runner"
        mock_audit = AsyncMock()
        with (
            patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.account.create_account",
                new=AsyncMock(return_value=account),
            ),
            patch("modulo.api.routes.admin.create_membership", new=AsyncMock(return_value=membership)),
            patch("modulo.api.routes.admin.validate_password_strength", return_value=None),
            patch("modulo.api.routes.admin.hash_password", return_value="hashed"),
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.append_audit_event", new=mock_audit),
        ):
            resp = client.post(
                self.URL,
                json={
                    "email": account.email,
                    "display_name": account.display_name,
                    "password": "Str0ngPassw0rd",
                    "org_role": "runner",
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == account.email
        assert body["org_role"] == "runner"
        mock_audit.assert_awaited_once()
        kwargs = mock_audit.await_args.kwargs
        assert kwargs["event_type"] == "user_created_by_admin"
        assert kwargs["resource_id"] == account.id
        assert kwargs["payload_json"]["target_user_id"] == str(account.id)
        assert kwargs["payload_json"]["org_role"] == "runner"
        # FAR-460: an admin-minted credential must be replaced by its owner on
        # first sign-in. Mirror the reset path â€” the create path must set the flag.
        assert account.must_change_password is True

    def test_create_user_audit_write_is_fail_open(self, client: TestClient) -> None:
        account = _fake_lifecycle_account()
        membership = MagicMock()
        membership.role = "viewer"

        async def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit chain write failed")

        with (
            patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.account.create_account",
                new=AsyncMock(return_value=account),
            ),
            patch("modulo.api.routes.admin.create_membership", new=AsyncMock(return_value=membership)),
            patch("modulo.api.routes.admin.validate_password_strength", return_value=None),
            patch("modulo.api.routes.admin.hash_password", return_value="hashed"),
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.append_audit_event", side_effect=_boom),
        ):
            resp = client.post(
                self.URL,
                json={
                    "email": account.email,
                    "display_name": account.display_name,
                    "password": "Str0ngPassw0rd",
                    "org_role": "viewer",
                },
            )

        # The user creation ALWAYS commits; a failed audit write never fails it.
        assert resp.status_code == 201
        assert resp.json()["org_role"] == "viewer"


class TestAdminResetPasswordLifecycle:
    """POST /api/v1/admin/users/{id}/reset-password forces a password change
    on next login (FAR-460) and emits ``user_password_reset_by_admin``."""

    URL = f"/api/v1/admin/users/{_OTHER_USER_ID}/reset-password"

    def test_reset_sets_must_change_password_and_emits_audit(self, admin_rls_client: TestClient) -> None:
        account = _fake_lifecycle_account()
        mock_audit = AsyncMock()
        with (
            patch("modulo.api.routes.admin.get_account_by_id", new=AsyncMock(return_value=account)),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("modulo.api.routes.admin.list_families_for_account", new=AsyncMock(return_value=[])),
            patch("modulo.api.routes.admin.blacklist_family", new=AsyncMock()),
            patch("modulo.api.routes.admin.append_audit_event", new=mock_audit),
        ):
            resp = admin_rls_client.post(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["temporary_password"]) >= 8
        # FAR-460: the temporary credential must be replaced by its owner.
        assert account.must_change_password is True
        mock_audit.assert_awaited_once()
        kwargs = mock_audit.await_args.kwargs
        assert kwargs["event_type"] == "user_password_reset_by_admin"
        assert kwargs["resource_id"] == _OTHER_USER_ID
        assert kwargs["payload_json"]["target_user_id"] == str(_OTHER_USER_ID)

    def test_reset_audit_write_is_fail_open(self, admin_rls_client: TestClient) -> None:
        account = _fake_lifecycle_account()

        async def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit chain write failed")

        with (
            patch("modulo.api.routes.admin.get_account_by_id", new=AsyncMock(return_value=account)),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("modulo.api.routes.admin.list_families_for_account", new=AsyncMock(return_value=[])),
            patch("modulo.api.routes.admin.blacklist_family", new=AsyncMock()),
            patch("modulo.api.routes.admin.append_audit_event", side_effect=_boom),
        ):
            resp = admin_rls_client.post(self.URL)

        assert resp.status_code == 200
        assert "temporary_password" in resp.json()

    def test_reset_break_glass_rejected_keeps_flag_untouched(self, admin_rls_client: TestClient) -> None:
        account = _fake_lifecycle_account()
        account.is_break_glass = True
        mock_audit = AsyncMock()
        with (
            patch("modulo.api.routes.admin.get_account_by_id", new=AsyncMock(return_value=account)),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("modulo.api.routes.admin.list_families_for_account", new=AsyncMock(return_value=[])),
            patch("modulo.api.routes.admin.blacklist_family", new=AsyncMock()),
            patch("modulo.api.routes.admin.append_audit_event", new=mock_audit),
        ):
            resp = admin_rls_client.post(self.URL)

        assert resp.status_code == 422
        assert account.must_change_password is False
        mock_audit.assert_not_awaited()
