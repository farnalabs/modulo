"""Unit tests for /api/v1/admin/users/invite* endpoints (FAR-461)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PUBLIC_URL = "http://app.test"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url=_PUBLIC_URL,
    )


def _make_rls_mock_session() -> AsyncMock:
    """Mock session usable by routes that call ``set_rls_org`` inside a transaction."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    return session


def _override_tenant(role: str) -> None:
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    mock_session = _make_rls_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _override_tenant("admin")
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_rls_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _override_tenant("operator")
    yield TestClient(app)
    app.dependency_overrides.clear()


def _invitation_mock() -> tuple[MagicMock, str]:
    invitation = MagicMock()
    invitation.id = uuid.uuid4()
    invitation.email = "new.member@example.com"
    invitation.display_name = "New Member"
    invitation.org_role = "runner"
    invitation.invited_by = _USER_ID
    invitation.created_at = datetime(2026, 8, 27, tzinfo=UTC)
    invitation.expires_at = datetime(2026, 8, 30, tzinfo=UTC)
    return invitation, "PLAINTEXT_TOKEN_VALUE"


def test_invite_user_returns_single_use_link(admin_client: TestClient) -> None:
    invitation, plaintext = _invitation_mock()
    create_mock = AsyncMock(return_value=(invitation, plaintext))
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch("modulo.api.routes.admin.create_invitation", new=create_mock),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(invitation.id)
    # The invite URL is anchored to settings.modulo_public_url and carries the
    # plaintext token exactly once — in the URL FRAGMENT (never the query
    # string, so the credential is not sent to servers/proxies/logs).
    assert body["invite_url"] == f"{_PUBLIC_URL}/accept-invite#token={plaintext}"
    assert "T" in body["expires_at"]
    called_kwargs = create_mock.await_args.kwargs
    assert called_kwargs["organisation_id"] == _ORG_ID
    assert called_kwargs["invited_by"] == _USER_ID
    assert called_kwargs["email"] == "new.member@example.com"
    assert called_kwargs["org_role"] == "runner"


def test_invite_user_expiry_defaults_to_72h(admin_client: TestClient) -> None:
    invitation, _plaintext = _invitation_mock()
    create_mock = AsyncMock(return_value=(invitation, "tok"))
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch("modulo.api.routes.admin.create_invitation", new=create_mock),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    before = datetime.now(UTC).replace(microsecond=0)
    expires_at = create_mock.await_args.kwargs["expires_at"]
    hours = (expires_at.replace(tzinfo=UTC) - before).total_seconds() / 3600
    assert 71.9 < hours < 72.01


def test_invite_user_email_already_member_conflicts(admin_client: TestClient) -> None:
    existing_account = MagicMock()
    membership = MagicMock(deactivated_at=None)
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=existing_account)),
        patch(
            "modulo.api.routes.admin.get_membership_by_account_and_org",
            new=AsyncMock(return_value=membership),
        ),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "A user with this email already exists in this organisation"


def test_invite_user_tombstoned_member_can_be_reinvited(admin_client: TestClient) -> None:
    """A deactivated member (deactivated_at set, FAR-533 tombstone) is not a
    live member: the 409 must not fire so the admin can re-invite; acceptance
    later reactivates the membership (see auth.accept_invite)."""
    invitation, plaintext = _invitation_mock()
    existing_account = MagicMock()
    tombstoned = MagicMock(deactivated_at="2026-08-01T00:00:00+00:00")
    create_mock = AsyncMock(return_value=(invitation, plaintext))
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=existing_account)),
        patch(
            "modulo.api.routes.admin.get_membership_by_account_and_org",
            new=AsyncMock(return_value=tombstoned),
        ),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch("modulo.api.routes.admin.create_invitation", new=create_mock),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "returning.member@example.com", "display_name": "Returning Member", "org_role": "runner"},
        )
    assert resp.status_code == 201
    assert create_mock.await_args.kwargs["organisation_id"] == _ORG_ID


def test_invite_user_duplicate_pending_invitation_conflicts(admin_client: TestClient) -> None:
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=True)),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An active invitation for this email already exists in this organisation"


def test_invite_user_invalid_role_unprocessable(admin_client: TestClient) -> None:
    resp = admin_client.post(
        "/api/v1/admin/users/invite",
        json={"email": "x@example.com", "display_name": "X", "org_role": "superuser"},
    )
    assert resp.status_code == 422
    assert "Invalid role" in resp.json()["detail"]


@pytest.mark.parametrize(
    "email",
    ["a" * 321 + "@example.com", "not-an-email", "missing-at.example.com", "spaces in@example.com"],
)
def test_invite_user_malformed_or_overlength_email_422(admin_client: TestClient, email: str) -> None:
    """Over-length / malformed emails are rejected by pydantic with a 422 —
    never a Postgres DataError (503) further down."""
    resp = admin_client.post(
        "/api/v1/admin/users/invite",
        json={"email": email, "display_name": "X", "org_role": "runner"},
    )
    assert resp.status_code == 422


def test_invite_user_overlength_display_name_422(admin_client: TestClient) -> None:
    resp = admin_client.post(
        "/api/v1/admin/users/invite",
        json={"email": "x@example.com", "display_name": "a" * 256, "org_role": "runner"},
    )
    assert resp.status_code == 422


def test_invite_requires_admin(operator_client: TestClient) -> None:
    resp = operator_client.post(
        "/api/v1/admin/users/invite",
        json={"email": "x@example.com", "display_name": "X", "org_role": "runner"},
    )
    assert resp.status_code == 403


def test_list_invitations_returns_pending_items(admin_client: TestClient) -> None:
    invitation, _plaintext = _invitation_mock()
    list_mock = AsyncMock(return_value=([invitation], 1))
    with (
        patch("modulo.api.routes.admin.list_pending_for_org", new=list_mock),
    ):
        resp = admin_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == invitation.email
    assert body["items"][0]["id"] == str(invitation.id)
    # Tenancy: the caller's org is what scopes the CRUD query.
    assert list_mock.await_args.kwargs["org_id"] == _ORG_ID


def test_list_invitations_scoped_to_callers_org(admin_client: TestClient) -> None:
    """The list is scoped by the caller's org id at the CRUD seam — org A's
    data can never satisfy an org B admin's query."""
    org_b_admin = uuid.UUID("00000000-0000-0000-0000-00000000000b")
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin",
        organisation_id=org_b_admin,
        account_id=_USER_ID,
        org_role="admin",
    )
    list_mock = AsyncMock(return_value=([], 0))
    with patch("modulo.api.routes.admin.list_pending_for_org", new=list_mock):
        resp = admin_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 200
    assert not resp.json()["items"]
    assert resp.json()["total"] == 0
    assert list_mock.await_args.kwargs["org_id"] == org_b_admin


def test_list_invitations_requires_admin(operator_client: TestClient) -> None:
    resp = operator_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 403


def test_revoke_invitation_success(admin_client: TestClient) -> None:
    invitation_id = uuid.uuid4()
    revoke_mock = AsyncMock(return_value=True)
    with (
        patch("modulo.api.routes.admin.revoke_invitation", new=revoke_mock),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 204
    # Tenancy: the revoke is scoped to the caller's org.
    assert revoke_mock.await_args.kwargs["org_id"] == _ORG_ID


def test_revoke_invitation_not_in_org_returns_404(admin_client: TestClient) -> None:
    """Cross-org / missing / consumed invitations all map to an identical 404."""
    invitation_id = uuid.uuid4()
    with patch("modulo.api.routes.admin.revoke_invitation", new=AsyncMock(return_value=False)):
        resp = admin_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invitation not found"


def test_revoke_invitation_requires_admin(operator_client: TestClient) -> None:
    invitation_id = uuid.uuid4()
    resp = operator_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 403


def test_invite_user_integrity_error_conflicts(admin_client: TestClient) -> None:
    """A DB-level IntegrityError (e.g. a unique/check violation surfaced after
    the application-level 409 guards) maps to 409, never 500."""
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch(
            "modulo.api.routes.admin.create_invitation", new=AsyncMock(side_effect=IntegrityError("dup", None, None))
        ),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 409


def test_invite_user_programming_error_feature_unavailable(admin_client: TestClient) -> None:
    """A ProgrammingError (missing migration column) degrades to 501 instead of 500."""
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch(
            "modulo.api.routes.admin.create_invitation",
            new=AsyncMock(side_effect=ProgrammingError("no column", None, None)),
        ),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 501


def test_invite_user_sqlalchemy_error_503(admin_client: TestClient) -> None:
    """A generic SQLAlchemyError degrades to 503 (database temporarily unavailable)."""
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch("modulo.api.routes.admin.create_invitation", new=AsyncMock(side_effect=SQLAlchemyError("down"))),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 503


def test_list_invitations_programming_error_501(admin_client: TestClient) -> None:
    with patch(
        "modulo.api.routes.admin.list_pending_for_org",
        new=AsyncMock(side_effect=ProgrammingError("no column", None, None)),
    ):
        resp = admin_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 501


def test_list_invitations_sqlalchemy_error_503(admin_client: TestClient) -> None:
    with patch("modulo.api.routes.admin.list_pending_for_org", new=AsyncMock(side_effect=SQLAlchemyError("down"))):
        resp = admin_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 503


def test_revoke_invitation_integrity_error_conflicts(admin_client: TestClient) -> None:
    invitation_id = uuid.uuid4()
    with (
        patch(
            "modulo.api.routes.admin.revoke_invitation", new=AsyncMock(side_effect=IntegrityError("dup", None, None))
        ),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 409


def test_revoke_invitation_programming_error_501(admin_client: TestClient) -> None:
    invitation_id = uuid.uuid4()
    with (
        patch(
            "modulo.api.routes.admin.revoke_invitation",
            new=AsyncMock(side_effect=ProgrammingError("no column", None, None)),
        ),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 501


def test_revoke_invitation_sqlalchemy_error_503(admin_client: TestClient) -> None:
    invitation_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.admin.revoke_invitation", new=AsyncMock(side_effect=SQLAlchemyError("down"))),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 503


def test_invite_user_audit_failure_is_fail_open(admin_client: TestClient) -> None:
    """An audit-write failure must never fail the completed invite: the 201 still
    returns with the minted link (the audit append is best-effort)."""
    invitation, plaintext = _invitation_mock()
    create_mock = AsyncMock(return_value=(invitation, plaintext))
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.admin.has_live_for_email", new=AsyncMock(return_value=False)),
        patch("modulo.api.routes.admin.create_invitation", new=create_mock),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))),
    ):
        resp = admin_client.post(
            "/api/v1/admin/users/invite",
            json={"email": "new.member@example.com", "display_name": "New Member", "org_role": "runner"},
        )
    assert resp.status_code == 201
    assert resp.json()["invite_url"] == f"{_PUBLIC_URL}/accept-invite#token={plaintext}"
