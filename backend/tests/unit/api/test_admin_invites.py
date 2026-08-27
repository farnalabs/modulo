"""Unit tests for /api/v1/admin/users/invite* endpoints (FAR-461)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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
    # plaintext token exactly once.
    assert body["invite_url"] == f"{_PUBLIC_URL}/accept-invite?token={plaintext}"
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
    membership = MagicMock()
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


def test_invite_requires_admin(operator_client: TestClient) -> None:
    resp = operator_client.post(
        "/api/v1/admin/users/invite",
        json={"email": "x@example.com", "display_name": "X", "org_role": "runner"},
    )
    assert resp.status_code == 403


def test_list_invitations_returns_pending_items(admin_client: TestClient) -> None:
    invitation, _plaintext = _invitation_mock()
    with (
        patch("modulo.api.routes.admin.list_pending_for_org", new=AsyncMock(return_value=([invitation], 1))),
    ):
        resp = admin_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == invitation.email
    assert body["items"][0]["id"] == str(invitation.id)


def test_list_invitations_requires_admin(operator_client: TestClient) -> None:
    resp = operator_client.get("/api/v1/admin/users/invitations")
    assert resp.status_code == 403


def test_revoke_invitation_success(admin_client: TestClient) -> None:
    invitation_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.admin.revoke_invitation", new=AsyncMock(return_value=True)),
        patch("modulo.api.routes.admin.append_audit_event", new=AsyncMock()),
    ):
        resp = admin_client.delete(f"/api/v1/admin/users/invitations/{invitation_id}")
    assert resp.status_code == 204


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
