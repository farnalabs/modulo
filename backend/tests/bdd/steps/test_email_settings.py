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


@pytest.fixture(autouse=True)
def _reset_email_send_limiter() -> None:
    """Start every scenario with a fresh test-send budget for every org."""
    from modulo.api.routes import admin_email as admin_email_mod

    admin_email_mod.test_send_limiter.reset()
    yield
    admin_email_mod.test_send_limiter.reset()


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
    ctx["response"] = resp


@given("I am authenticated as a system admin")
def _given_sys_admin(email_ctx: dict[str, Any]) -> None:
    email_ctx["_viewer"] = False


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def _given_viewer(org: str, email_ctx: dict[str, Any], request) -> None:
    """Email-local viewer-auth step.

    Defined locally (rather than relying on the shared step in ``conftest.py``
    / other step modules) so this feature file is self-contained: it flips the
    ``email_ctx`` viewer flag the ``_build_app`` auth override reads and sets
    the ``request.node._viewer_auth`` marker ``_when_get_settings`` checks.
    """
    email_ctx["_viewer"] = True
    request.node._viewer_auth = True


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


@given("the test-send rate limit budget is exhausted for the test organisation")
def _given_rate_limit_exhausted(email_ctx: dict[str, Any]) -> None:
    """Consume every test-send slot for the shared org id up front."""
    import asyncio

    from modulo.api.routes import admin_email as admin_email_mod

    async def _exhaust() -> None:
        for _ in range(admin_email_mod.test_send_limiter.limit):
            await admin_email_mod.test_send_limiter.acquire(_ORG_ID)

    asyncio.run(_exhaust())


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
        with patch("modulo.api.routes.admin_email.send_email", send) as mock_send:
            _call(
                "POST",
                f"{_ORG_PATH}/test",
                email_ctx,
                request,
                json={"to": to},
            )
            email_ctx["_send_attempts"] = mock_send.call_count


@when("I POST a test email with header injection for the test organisation")
def _when_post_test_header_injection(email_ctx: dict[str, Any], request) -> None:
    org = _make_org(email_ctx.get("_settings_json"))
    with (
        patch("modulo.api.routes.admin_email.get_organisation", AsyncMock(return_value=org)),
        patch("modulo.api.routes.admin_email.send_email", MagicMock(return_value=True)) as mock_send,
    ):
        _call(
            "POST",
            f"{_ORG_PATH}/test",
            email_ctx,
            request,
            json={"to": "admin@example.com\r\nBcc: victim@example.com"},
        )
        email_ctx["_send_attempts"] = mock_send.call_count


@then("no test email is sent to the SMTP relay")
def _then_no_send(email_ctx: dict[str, Any]) -> None:
    import modulo.api.routes.admin_email as admin_email_mod

    if email_ctx.get("_send_attempts") is not None:
        assert email_ctx["_send_attempts"] == 0
    else:
        assert admin_email_mod.send_email.call_count == 0


@then("the response carries a Retry-After header")
def _then_retry_after(request) -> None:
    resp = request.node._resp
    assert resp.status_code == 429
    assert resp.headers.get("retry-after")


@then(parsers.parse("the response status is {status:d}"))
def _then_response_status(status: int, request) -> None:
    """Email-local status check reading the response stored by ``_call``.

    Mirrors the shared ``the response status is {status:d}`` step in
    ``conftest.py`` so this feature file resolves deterministically regardless
    of which other step modules are collected in the same session.
    """
    resp = request.node._resp
    assert resp.status_code == status, f"Expected status {status}, got {resp.status_code}"


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
