"""Tests for the admin email settings API."""

import base64
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

import modulo.api.routes.admin_email as admin_email_mod
from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

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

FERNET_KEY = base64.urlsafe_b64encode(b"a" * 32).decode()  # valid Fernet key


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=FERNET_KEY,
        modulo_admin_password="testpass",
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
    class _FakeResult:
        def scalar_one_or_none(self):
            return "admin"

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    app.dependency_overrides[get_settings] = _make_settings
    mock_session.execute = AsyncMock(return_value=_FakeResult())
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    class _FakeResult:
        def scalar_one_or_none(self):
            return None

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: VIEWER_PRINCIPAL
    mock_session.execute = AsyncMock(return_value=_FakeResult())
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


@pytest.fixture(autouse=True)
def _reset_test_send_limiter():
    """Ensure the module-level test-send limiter starts fresh for every test."""
    admin_email_mod.test_send_limiter.reset()
    yield
    admin_email_mod.test_send_limiter.reset()


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
            assert not data["smtp_host"]
            assert data["smtp_port"] == 587
            assert not data["email_from"]
            assert data["smtp_timeout"] == 30
        finally:
            admin_email.get_organisation = original

    async def test_get_settings_includes_stored_timeout(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_timeout": 10}},
        )
        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            resp = await client_admin.get(f"/api/v1/admin/org/{ORG_ID}/email-settings")
            assert resp.status_code == 200
            assert resp.json()["smtp_timeout"] == 10
        finally:
            admin_email.get_organisation = original

    async def test_get_settings_non_numeric_timeout_falls_back_to_default(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_timeout": "not-a-number"}},
        )
        original = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            resp = await client_admin.get(f"/api/v1/admin/org/{ORG_ID}/email-settings")
            assert resp.status_code == 200
            assert resp.json()["smtp_timeout"] == 30
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
            # Password should be encrypted, not stored as plaintext
            stored_password = call_updates["settings_json"]["email"]["smtp_password"]
            assert stored_password != "newpass"
            assert stored_password.startswith("gAAAAA")  # Fernet prefix
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

    async def test_put_settings_timeout_persisted(self, client_admin, mock_session):
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
                    "smtp_port": 587,
                    "smtp_username": "",
                    "smtp_password": "",
                    "email_from": "",
                    "smtp_timeout": 10,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["smtp_timeout"] == 10
            call_updates = admin_email.update_organisation.call_args[0][2]
            assert call_updates["settings_json"]["email"]["smtp_timeout"] == 10
        finally:
            admin_email.get_organisation = original_get
            admin_email.update_organisation = original_update

    async def test_put_settings_timeout_too_low_422(self, client_admin, mock_session):
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
                    "smtp_timeout": 0,
                },
            )
            assert resp.status_code == 422
        finally:
            admin_email.get_organisation = original

    async def test_put_settings_timeout_too_high_422(self, client_admin, mock_session):
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
                    "smtp_timeout": 121,
                },
            )
            assert resp.status_code == 422
        finally:
            admin_email.get_organisation = original

    async def test_put_settings_password_too_long_422(self, client_admin, mock_session):
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
                    "smtp_password": "x" * 300,
                    "email_from": "",
                },
            )
            assert resp.status_code == 422
        finally:
            admin_email.get_organisation = original


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

    async def test_email_test_success(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(return_value=True)
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "admin@example.com"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "message": "Test email sent successfully"}
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_passes_configured_timeout(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_timeout": 8}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(return_value=True)
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "admin@example.com"},
            )
            assert resp.status_code == 200
            temp_settings = admin_email.send_email.call_args[0][0]
            assert temp_settings.smtp_timeout == 8
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_smtp_failure_returns_descriptive_message(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.core.email_service import EmailSendingError
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(side_effect=EmailSendingError("Connection refused"))
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "admin@example.com"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": False, "message": "Connection refused"}
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_unexpected_exception_does_not_leak_internals(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(side_effect=RuntimeError("secret internal detail: db host 10.0.0.1"))
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "admin@example.com"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is False
            assert "10.0.0.1" not in data["message"]
            assert "secret internal" not in data["message"]
            assert data["message"] == "Unexpected error while sending the test email"
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_invalid_recipient_422(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(return_value=True)
        try:
            resp = await client_admin.post(
                f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                json={"to": "not-an-email"},
            )
            assert resp.status_code == 422
            assert "recipient" in resp.json()["detail"].lower()
            admin_email.send_email.assert_not_called()
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_requires_email_shape(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(return_value=True)
        try:
            for bad in ("https://example.com", "Admin <admin@example.com>", "a@example.com, b@example.com"):
                resp = await client_admin.post(
                    f"/api/v1/admin/org/{ORG_ID}/email-settings/test",
                    json={"to": bad},
                )
                assert resp.status_code == 422
            admin_email.send_email.assert_not_called()
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_rate_limited_429_with_retry_after(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(return_value=True)
        try:
            url = f"/api/v1/admin/org/{ORG_ID}/email-settings/test"
            for _ in range(admin_email.test_send_limiter.limit):
                resp = await client_admin.post(url, json={"to": "admin@example.com"})
                assert resp.status_code == 200
            resp = await client_admin.post(url, json={"to": "admin@example.com"})
            assert resp.status_code == 429
            assert resp.headers.get("retry-after")
            assert "Too many test emails" in resp.json()["detail"]
            assert admin_email.send_email.call_count == admin_email.test_send_limiter.limit
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send

    async def test_email_test_rate_limit_is_per_org(self, client_admin, mock_session):
        import modulo.api.routes.admin_email as admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=ORG_ID,
            name="Test",
            slug="test",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        other_org = Organisation(
            id=uuid4(),
            name="Other",
            slug="other",
            settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}},
        )
        original_get = admin_email.get_organisation
        original_send = admin_email.send_email
        admin_email.send_email = MagicMock(return_value=True)
        try:
            url = f"/api/v1/admin/org/{ORG_ID}/email-settings/test"
            for _ in range(admin_email.test_send_limiter.limit):
                admin_email.get_organisation = AsyncMock(return_value=org)
                resp = await client_admin.post(url, json={"to": "admin@example.com"})
                assert resp.status_code == 200
            admin_email.get_organisation = AsyncMock(return_value=org)
            resp = await client_admin.post(url, json={"to": "admin@example.com"})
            assert resp.status_code == 429

            admin_email.get_organisation = AsyncMock(return_value=other_org)
            other_url = f"/api/v1/admin/org/{other_org.id}/email-settings/test"
            resp = await client_admin.post(other_url, json={"to": "admin@example.com"})
            assert resp.status_code == 200
        finally:
            admin_email.get_organisation = original_get
            admin_email.send_email = original_send
