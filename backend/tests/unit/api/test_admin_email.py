"""Tests for the admin email settings API."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal

ORG_ID = uuid4()
USER_ID = uuid4()
ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="admin@test",
    organisation_id=ORG_ID,
    account_id=USER_ID,
    org_role="admin",
)
VIEWER_PRINCIPAL = AuthenticatedPrincipal(
    username="viewer@test",
    organisation_id=ORG_ID,
    account_id=uuid4(),
    org_role="viewer",
)
SYSTEM_ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="sysadmin@test",
    organisation_id=ORG_ID,
    account_id=uuid4(),
    org_role="admin",
    is_system_admin=True,
)


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.begin = MagicMock()
    session.begin.return_value.__aenter__.return_value = session
    session.begin.return_value.__aexit__.return_value = None
    session.execute.return_value.scalar_one_or_none.return_value = None
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.flush.return_value = None
    return session


@pytest.fixture
def client_admin(mock_session):
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: VIEWER_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_system_admin(mock_session):
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: SYSTEM_ADMIN_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


class TestGetEmailSettings:
    async def test_get_settings_success(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={
                "email": {
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 587,
                    "smtp_username": "user",
                    "email_from": "noreply@example.com",
                }
            },
        )
        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            resp = await client_admin.get(f"/api/v1/admin/org/{ORG_ID}/email-settings")
            assert resp.status_code == 200
            data = resp.json()
            assert data["smtp_host"] == "smtp.example.com"
            assert data["smtp_port"] == 587
            assert data["smtp_username"] == "user"
            assert data["smtp_password"] == "********"
            assert data["email_from"] == "noreply@example.com"
        finally:
            admin_email.get_organisation = original

    async def test_get_settings_empty(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(id=ORG_ID, name="Test", slug="test", settings_json={})
        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            resp = await client_admin.get(f"/api/v1/admin/org/{ORG_ID}/email-settings")
            assert resp.status_code == 200
            data = resp.json()
            assert data["smtp_host"] == ""
            assert data["smtp_port"] == 587
            assert data["email_from"] == ""
        finally:
            admin_email.get_organisation = original

    async def test_get_settings_not_found(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email

        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=None)
        try:
            resp = await client_admin.get(f"/api/v1/admin/org/{ORG_ID}/email-settings")
            assert resp.status_code == 404
        finally:
            admin_email.get_organisation = original

    async def test_get_settings_viewer_forbidden(self, client_viewer):
        resp = await client_viewer.get(f"/api/v1/admin/org/{uuid4()}/email-settings")
        assert resp.status_code == 403


class TestPutEmailSettings:
    async def test_put_settings_success(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(id=ORG_ID, name="Test", slug="test", settings_json={})
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_update = admin_email.update_organisation
        admin_email.update_organisation = AsyncMock(return_value=org)
        try:
            resp = await client_admin.put(
                f"/api/v1/admin/org/{ORG_ID}/email-settings",
                json={
                    "smtp_host": "smtp.new.com",
                    "smtp_port": 465,
                    "smtp_username": "newuser",
                    "smtp_password": "newpass",
                    "email_from": "new@example.com",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["smtp_host"] == "smtp.new.com"
            assert data["smtp_port"] == 465
            assert data["smtp_username"] == "newuser"
            assert data["email_from"] == "new@example.com"
            assert data["smtp_password"] == "********"

            admin_email.update_organisation.assert_called_once()
            call_updates = admin_email.update_organisation.call_args[0][2]
            assert call_updates["settings_json"]["email"]["smtp_host"] == "smtp.new.com"
            assert call_updates["settings_json"]["email"]["smtp_password"] == "newpass"
        finally:
            admin_email.get_organisation = original_get
            admin_email.update_organisation = original_update

    async def test_put_settings_not_found(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email

        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=None)
        try:
            resp = await client_admin.put(
                f"/api/v1/admin/org/{ORG_ID}/email-settings",
                json={
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 587,
                    "smtp_username": "",
                    "smtp_password": "",
                    "email_from": "",
                },
            )
            assert resp.status_code == 404
        finally:
            admin_email.get_organisation = original

    async def test_put_settings_viewer_forbidden(self, client_viewer):
        resp = await client_viewer.put(
            f"/api/v1/admin/org/{uuid4()}/email-settings",
            json={
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_username": "",
                "smtp_password": "",
                "email_from": "",
            },
        )
        assert resp.status_code == 403


class TestTestEmail:
    async def test_email_test_no_config(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(id=ORG_ID, name="Test", slug="test", settings_json={})
        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "admin@example.com"},
            )
            assert resp.status_code == 422
        finally:
            admin_email.get_organisation = original

    async def test_email_test_not_found(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email

        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=None)
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "admin@example.com"},
            )
            assert resp.status_code == 404
        finally:
            admin_email.get_organisation = original

    async def test_email_test_viewer_forbidden(self, client_viewer):
        resp = await client_viewer.post(
            f"/api/v1/admin/org/{uuid4()}/email-settings/test",
            json={"to": "admin@example.com"},
        )
        assert resp.status_code == 403
