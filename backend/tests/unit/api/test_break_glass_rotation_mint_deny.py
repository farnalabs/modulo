"""Break-glass mint-deny runtime proofs for the rotation routes (deliverable (B)).

`deny_break_glass_mint` is wired onto `rotate_key` (admin_rotation.py) and
`rotate_identity_secret` (product_analytics_identity.py). The oracle in
``test_break_glass_mint_deny.py`` asserts the markers are *present*; these
runtime tests prove they are *wired* — a break-glass admin is denied (403) and
a normal admin is not (success status).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_ROTATE_KEY_URL = "/api/v1/admin/rotation/rotate-key"
_ROTATE_SECRET_URL = "/api/v1/product-analytics/rotate"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="http://localhost:8000",
    )


def _make_principal(is_break_glass: bool = False, *, is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="breakglass-user" if is_break_glass else "testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
    )


def _make_account(*, is_break_glass: bool = True, live: bool = True) -> MagicMock:
    account = MagicMock()
    account.is_break_glass = is_break_glass
    account.active = True
    account.break_glass_deactivated_at = None
    account.break_glass_expires_at = (
        datetime.now(UTC) + timedelta(hours=1) if live else datetime.now(UTC) - timedelta(hours=1)
    )
    return account


def _make_session(account: object | None, *, raise_on_get: bool = False) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    if raise_on_get:
        session.get = AsyncMock(side_effect=Exception("db unavailable"))
    else:
        session.get = AsyncMock(return_value=account)
    return session


def _configure_auth(app_under_test: object, *, session: AsyncMock, principal: AuthenticatedPrincipal) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_user() -> AuthenticatedPrincipal:
        return principal

    app_under_test.dependency_overrides[get_db_session] = override_session
    app_under_test.dependency_overrides[get_current_user] = override_user


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# rotate_key (POST /api/v1/admin/rotation/rotate-key)
# ---------------------------------------------------------------------------


def test_break_glass_cannot_rotate_key(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post(_ROTATE_KEY_URL, json={"new_fernet_key": _VALID_32})
    assert resp.status_code == 403
    assert "Break-glass accounts" in resp.json()["detail"]


def test_denied_break_glass_cannot_rotate_key(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=False)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post(_ROTATE_KEY_URL, json={"new_fernet_key": _VALID_32})
    assert resp.status_code == 403


def test_normal_admin_can_rotate_key(client: TestClient) -> None:
    from modulo.api.routes import admin_rotation

    admin_rotation._rotation_in_progress = False
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=False)),
        principal=_make_principal(is_break_glass=False, is_system_admin=True),
    )
    with (
        patch("modulo.api.routes.admin_rotation.append_audit_event", new=AsyncMock()),
        patch("modulo.api.routes.admin_rotation.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.routes.admin_rotation.get_or_create_session_factory",
            return_value=AsyncMock(),
        ),
        patch("modulo.api.routes.admin_rotation._run_rotation_background", new=AsyncMock()),
    ):
        resp = client.post(_ROTATE_KEY_URL, json={"new_fernet_key": _VALID_32})
    assert resp.status_code == 202
    admin_rotation._rotation_in_progress = False


# ---------------------------------------------------------------------------
# rotate_identity_secret (POST /api/v1/product-analytics/rotate)
# ---------------------------------------------------------------------------


def test_break_glass_cannot_rotate_identity_secret(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post(
        _ROTATE_SECRET_URL,
        json={"old_secret": "x", "timestamp": 1.0, "sequence": 1, "hmac_digest": "y"},
    )
    assert resp.status_code == 403
    assert "Break-glass accounts" in resp.json()["detail"]


def test_denied_break_glass_cannot_rotate_identity_secret(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=False)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post(
        _ROTATE_SECRET_URL,
        json={"old_secret": "x", "timestamp": 1.0, "sequence": 1, "hmac_digest": "y"},
    )
    assert resp.status_code == 403


def test_normal_admin_can_rotate_identity_secret(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=False)),
        principal=_make_principal(is_break_glass=False, is_system_admin=True),
    )
    with (
        patch(
            "modulo.api.routes.product_analytics_identity.get_or_create_instance_identity",
            new=AsyncMock(return_value=(_ORG_ID, "current-secret")),
        ),
        patch(
            "modulo.api.routes.product_analytics_identity._constant_time_equal",
            return_value=True,
        ),
        patch(
            "modulo.api.routes.product_analytics_identity.verify_hmac",
            return_value=True,
        ),
        patch(
            "modulo.api.routes.product_analytics_identity._get_last_sequence",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "modulo.api.routes.product_analytics_identity._set_last_sequence",
            new=AsyncMock(),
        ),
        patch(
            "modulo.api.routes.product_analytics_identity.rotate_secret",
            new=AsyncMock(return_value="new-secret"),
        ),
    ):
        resp = client.post(
            _ROTATE_SECRET_URL,
            json={"old_secret": "x", "timestamp": 1.0, "sequence": 1, "hmac_digest": "y"},
        )
    assert resp.status_code == 200
    assert resp.json()["new_secret"] == "new-secret"
