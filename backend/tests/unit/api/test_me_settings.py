"""Unit tests for PUT /api/v1/me/settings (FAR-620 Phase 1, item 7).

The endpoint now writes through the row-locked ``update_account_preferences``
helper: the happy-path response contract (merged preferences echoed back) is
unchanged; the missing-account case is a loud 404 — the previous silent
success (the input echoed back unchanged, no 404) is the test's delta.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.account import Account
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_ENDPOINT = "/api/v1/me/settings"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
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
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    client = TestClient(app)
    client.mock_session = mock_session  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


def test_put_settings_merges_and_echoes_settings_response(client: TestClient) -> None:
    account = MagicMock()
    account.preferences = {"hitl_email": {"default": True, "pipeline_overrides": {}}}
    client.mock_session.get = AsyncMock(return_value=account)  # type: ignore[attr-defined]
    resp = client.put(_ENDPOINT, json={"theme": "dark", "locale": "en-GB"})

    assert resp.status_code == 200
    # Contract unchanged: the response is the SettingsResponse view
    # (theme/locale only) — the sibling hitl_email key stays persisted but is
    # never echoed here.
    assert resp.json() == {"theme": "dark", "locale": "en-GB"}
    # The merge preserved the sibling key under the row lock.
    assert account.preferences["hitl_email"] == {"default": True, "pipeline_overrides": {}}
    # The row lock is the sibling-key serialisation point.
    client.mock_session.get.assert_awaited_once_with(  # type: ignore[attr-defined]
        Account,
        _USER_ID,
        with_for_update=True,
    )


def test_put_settings_missing_account_returns_404(client: TestClient) -> None:
    """The unlocked helper's silent-success-on-missing-account delta: the
    input is no longer echoed back as success — the write is 404-loud."""
    client.mock_session.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    resp = client.put(_ENDPOINT, json={"theme": "dark"})
    assert resp.status_code == 404


# ── GET /me/settings ────────────────────────────────────────────────


class TestGetUserSettings:
    def test_returns_preferences(self, client: TestClient) -> None:
        account = MagicMock()
        account.preferences = {"theme": "dark", "locale": "en-GB"}
        with patch("modulo.api.routes.me.get_account_by_id", return_value=account):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json() == {"theme": "dark", "locale": "en-GB"}

    def test_absent_preferences_returns_defaults(self, client: TestClient) -> None:
        account = MagicMock()
        account.preferences = {}
        with patch("modulo.api.routes.me.get_account_by_id", return_value=account):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json() == {"theme": None, "locale": None}

    def test_missing_account_returns_404(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.get_account_by_id", return_value=None):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 404

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.me.get_account_by_id",
            side_effect=ProgrammingError("stmt", "params", Exception("orig")),
        ):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 501

    def test_needs_auth(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            c = TestClient(app)
            resp = c.get(_ENDPOINT)
            assert resp.status_code in (401, 403)
        finally:
            app.dependency_overrides.clear()


# ── PUT /me/settings with no body (req=None path) ──────────────────


class TestUpdateUserSettingsNoBody:
    def test_no_body_returns_current_settings(self, client: TestClient) -> None:
        account = MagicMock()
        account.preferences = {"theme": "light"}
        with patch("modulo.api.routes.me.get_account_by_id", return_value=account):
            resp = client.put(_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json() == {"theme": "light", "locale": None}

    def test_no_body_missing_account_returns_404(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.get_account_by_id", return_value=None):
            resp = client.put(_ENDPOINT)
        assert resp.status_code == 404

    def test_no_body_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.me.get_account_by_id",
            side_effect=ProgrammingError("stmt", "params", Exception("orig")),
        ):
            resp = client.put(_ENDPOINT)
        assert resp.status_code == 501

    def test_with_body_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.me.update_account_preferences",
            side_effect=ProgrammingError("stmt", "params", Exception("orig")),
        ):
            resp = client.put(_ENDPOINT, json={"theme": "dark"})
        assert resp.status_code == 501

    def test_with_body_account_not_found_returns_404(self, client: TestClient) -> None:
        from modulo.db.crud.account import AccountNotFoundError

        with patch(
            "modulo.api.routes.me.update_account_preferences",
            side_effect=AccountNotFoundError(),
        ):
            resp = client.put(_ENDPOINT, json={"theme": "dark"})
        assert resp.status_code == 404
