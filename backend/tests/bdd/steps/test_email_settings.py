"""BDD step definitions for admin email settings.

Supports ``features/admin/email-settings.feature`` — GET/PUT of org SMTP email
settings (incl. the configurable ``smtp_timeout`` and the SMTP password length
limit), the test-send endpoint, and the viewer-auth 403 path.
"""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/admin/email-settings.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG_PATH = f"/api/v1/admin/org/{_ORG_ID}/email-settings"


@pytest.fixture
def email_ctx() -> dict[str, Any]:
    """Shared mutable context for the email-settings step definitions."""
    return {"_viewer": False}


def _make_org(settings_json: dict[str, Any] | None = None) -> MagicMock:
    from modulo.db.models.organisation import Organisation

    org = MagicMock(spec=Organisation)
    org.id = _ORG_ID
    org.settings_json = settings_json or {}
    return org


def _seed_session(ctx: dict[str, Any]) -> AsyncMock:
    from sqlalchemy.exc import ProgrammingError

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__.return_value = session
    begin_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=begin_cm)

    if ctx.get("_no_table"):
        session.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, 'relation "organisations" does not exist'))
        return session

    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute.return_value = exec_result
    return session


def _build_app(ctx: dict[str, Any]) -> FastAPI:
    from modulo.api.dependencies import get_db_session, get_plan_context
    from modulo.api.routes.admin_email import router as email_router
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings

    app = FastAPI()
    app.include_router(email_router)

    def _user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="viewer@test" if ctx["_viewer"] else "admin@test",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="viewer" if ctx["_viewer"] else "admin",
            is_system_admin=not ctx["_viewer"],
        )

    def _settings() -> Any:
        from modulo.settings import Settings

        return Settings(
            database_url="sqlite+aiosqlite:///./test.db",
            secret_key="a" * 32,
            fernet_key="b" * 32,
            modulo_admin_password="testpass",
            modulo_csrf_enabled=False,
        )

    async def _db() -> AsyncMock:
        return _seed_session(ctx)

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return app


def _call(method: str, path: str, ctx: dict[str, Any], request, json: dict | None = None) -> None:
    app = _build_app(ctx)
    client = TestClient(app)
    resp = client.request(method, path, json=json)
    request.node._resp = resp
    ctx["_last_resp"] = resp


@given("I am authenticated as a system admin")
def _given_sys_admin(email_ctx: dict[str, Any]) -> None:
    email_ctx["_viewer"] = False


@given("the organisation has no saved email settings")
def _given_no_saved_settings(email_ctx: dict[str, Any]) -> None:
    email_ctx["_settings_json"] = {}


@given(parsers.parse('email settings are configured with SMTP host "{host}"'))
def _given_configured_smtp(host: str, email_ctx: dict[str, Any]) -> None:
    email_ctx["_settings_json"] = {"email": {"smtp_host": host, "smtp_port": 587}}


@given("the SMTP relay accepts the test email")
def _given_relay_ok(email_ctx: dict[str, Any]) -> None:
    email_ctx["_send_ok"] = True


@given(parsers.parse('the SMTP relay rejects the test email with "{message}"'))
def _given_relay_fails(message: str, email_ctx: dict[str, Any]) -> None:
    from modulo.core.email_service import EmailSendingError

    email_ctx["_send_error"] = EmailSendingError(message)


@when("I GET the email settings for the test organisation")
def _when_get_settings(email_ctx: dict[str, Any], request) -> None:
    if getattr(request.node, "_viewer_auth", False):
        email_ctx["_viewer"] = True
    org = _make_org(email_ctx.get("_settings_json"))
    with patch("modulo.api.routes.admin_email.get_organisation", AsyncMock(return_value=org)):
        _call("GET", _ORG_PATH, email_ctx, request)


@when(parsers.parse("I PUT the email settings for the test organisation with a timeout of {timeout:d}"))
def _when_put_timeout(timeout: int, email_ctx: dict[str, Any], request) -> None:
    org = _make_org(email_ctx.get("_settings_json"))
    with (
        patch("modulo.api.routes.admin_email.get_organisation", AsyncMock(return_value=org)),
        patch("modulo.api.routes.admin_email.update_organisation", AsyncMock(return_value=org)),
    ):
        _call(
            "PUT",
            _ORG_PATH,
            email_ctx,
            request,
            json={
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_username": "",
                "smtp_password": "",
                "email_from": "",
                "smtp_timeout": timeout,
            },
        )


@when(parsers.parse("I PUT the email settings for the test organisation with a password of {size:d} characters"))
def _when_put_long_password(size: int, email_ctx: dict[str, Any], request) -> None:
    org = _make_org(email_ctx.get("_settings_json"))
    with (
        patch("modulo.api.routes.admin_email.get_organisation", AsyncMock(return_value=org)),
        patch("modulo.api.routes.admin_email.update_organisation", AsyncMock(return_value=org)),
    ):
        _call(
            "PUT",
            _ORG_PATH,
            email_ctx,
            request,
            json={
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_username": "",
                "smtp_password": "x" * size,
                "email_from": "",
            },
        )


@when(parsers.parse('I POST a test email to "{to}" for the test organisation'))
def _when_post_test(to: str, email_ctx: dict[str, Any], request) -> None:
    org = _make_org(email_ctx.get("_settings_json"))
    with patch("modulo.api.routes.admin_email.get_organisation", AsyncMock(return_value=org)):
        if email_ctx.get("_send_ok"):
            send = MagicMock(return_value=True)
        elif email_ctx.get("_send_error"):
            send = MagicMock(side_effect=email_ctx["_send_error"])
        else:
            send = MagicMock(return_value=True)
        with patch("modulo.api.routes.admin_email.send_email", send):
            _call(
                "POST",
                f"{_ORG_PATH}/test",
                email_ctx,
                request,
                json={"to": to},
            )


@then("the email settings response has masked password and default timeout 30")
def _then_default_timeout(request) -> None:
    data = request.node._resp.json()
    assert data["smtp_password"] == "********"
    assert data["smtp_timeout"] == 30


@then(parsers.parse("the email settings response includes a timeout of {timeout:d}"))
def _then_timeout(request, timeout: int) -> None:
    assert request.node._resp.json()["smtp_timeout"] == timeout


@then("the response confirms the test email was sent")
def _then_test_sent(request) -> None:
    data = request.node._resp.json()
    assert data["ok"] is True


@then("the response reports the test email failed")
def _then_test_failed(request) -> None:
    data = request.node._resp.json()
    assert data["ok"] is False
