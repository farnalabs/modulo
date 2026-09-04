"""Unit tests for the /api/v1/me/hitl-email-preferences endpoints (FAR-602).

GET/PUT round-trip for the CALLER's own HITL email-alert preferences,
persisted under the ``hitl_email`` key of ``Account.preferences``. Follows
the mock-session route-test pattern of the sibling ``me`` route tests.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_ENDPOINT = "/api/v1/me/hitl-email-preferences"


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


def _make_account(preferences: object) -> MagicMock:
    account = MagicMock()
    account.preferences = preferences
    return account


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


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestGetHitlEmailPreferences:
    def test_needs_auth(self, unauth_client: TestClient) -> None:
        assert unauth_client.get(_ENDPOINT).status_code == 401

    def test_absent_key_returns_all_off(self, client: TestClient) -> None:
        account = _make_account({"theme": "dark"})
        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=account) as mock_get,
        ):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json() == {"default": False, "pipeline_overrides": {}}
        mock_get.assert_called_once()

    def test_stored_preferences_are_returned(self, client: TestClient) -> None:
        stored = {"hitl_email": {"default": True, "pipeline_overrides": {str(_PIPELINE_ID): False}}}
        account = _make_account(stored)
        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=account),
        ):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json() == {"default": True, "pipeline_overrides": {str(_PIPELINE_ID): False}}

    def test_malformed_stored_preferences_normalise_to_off(self, client: TestClient) -> None:
        account = _make_account({"hitl_email": {"default": "yes", "pipeline_overrides": "bad"}})
        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=account),
        ):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json() == {"default": False, "pipeline_overrides": {}}

    def test_account_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=None),
        ):
            resp = client.get(_ENDPOINT)
        assert resp.status_code == 404


class TestUpdateHitlEmailPreferences:
    def test_needs_auth(self, unauth_client: TestClient) -> None:
        assert unauth_client.put(_ENDPOINT, json={"default": True}).status_code == 401

    def _session_gets(self, client: TestClient, account: object) -> None:
        """Point the route's row-locked ``session.get`` at *account*."""
        client.mock_session.get = AsyncMock(return_value=account)  # type: ignore[attr-defined]

    def test_put_persists_under_hitl_email_key(self, client: TestClient) -> None:
        account = _make_account({})
        self._session_gets(client, account)
        resp = client.put(_ENDPOINT, json={"default": True, "pipeline_overrides": {str(_PIPELINE_ID): False}})

        assert resp.status_code == 200
        assert account.preferences == {
            "hitl_email": {"default": True, "pipeline_overrides": {str(_PIPELINE_ID): False}},
        }

    def test_put_merges_with_existing_preferences(self, client: TestClient) -> None:
        account = _make_account({"theme": "dark", "hitl_email": {"default": False, "pipeline_overrides": {}}})
        self._session_gets(client, account)
        resp = client.put(_ENDPOINT, json={"default": True, "pipeline_overrides": {str(_PIPELINE_ID): True}})

        assert resp.status_code == 200
        assert account.preferences["theme"] == "dark"
        assert account.preferences["hitl_email"] == {
            "default": True,
            "pipeline_overrides": {str(_PIPELINE_ID): True},
        }

    def test_put_round_trip_response_echoes_persisted_preferences(self, client: TestClient) -> None:
        account = _make_account({"theme": "dark"})
        self._session_gets(client, account)
        resp = client.put(_ENDPOINT, json={"default": False, "pipeline_overrides": {str(_PIPELINE_ID): True}})

        assert resp.status_code == 200
        # The response carries ONLY the hitl_email block (other preference
        # keys like theme stay persisted but are never leaked here).
        assert resp.json() == {"default": False, "pipeline_overrides": {str(_PIPELINE_ID): True}}

    def test_put_defaults_body_fields(self, client: TestClient) -> None:
        account = _make_account({})
        self._session_gets(client, account)
        resp = client.put(_ENDPOINT, json={})

        assert resp.status_code == 200
        assert account.preferences == {"hitl_email": {"default": False, "pipeline_overrides": {}}}

    def test_put_rejects_non_uuid_override_key(self, client: TestClient) -> None:
        resp = client.put(_ENDPOINT, json={"default": True, "pipeline_overrides": {"not-a-uuid": True}})
        assert resp.status_code == 422

    def test_put_rejects_non_bool_override_value(self, client: TestClient) -> None:
        resp = client.put(_ENDPOINT, json={"default": True, "pipeline_overrides": {str(_PIPELINE_ID): "yes"}})
        assert resp.status_code == 422

    def test_put_rejects_non_bool_default(self, client: TestClient) -> None:
        resp = client.put(_ENDPOINT, json={"default": "yes"})
        assert resp.status_code == 422

    def test_put_account_not_found_returns_404(self, client: TestClient) -> None:
        self._session_gets(client, None)
        resp = client.put(_ENDPOINT, json={"default": True})
        assert resp.status_code == 404
