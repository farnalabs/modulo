"""Unit tests for error forwarder configuration API routes.

Tests all 3 endpoints (list, configure, test) with happy path, error paths,
edge cases, and feature gating.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.error_forwarder_config import _is_configured
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_UUID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_VALID_32 = "a" * 32
_FERNET_KEY = "KuV0vzf5ha7CJ3n4Dg_aqO6S4wBNJ31Q1fahdEYHHCo="


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_mock_forwarder(ok: bool = True) -> AsyncMock:
    fwd = AsyncMock()
    fwd.forward = AsyncMock(return_value=ok)
    return fwd


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_UUID,
        account_id=_USER_UUID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_UUID,
        account_id=_USER_UUID,
        org_role="viewer",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def no_org_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=None,
        account_id=_USER_UUID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def gated_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_UUID,
        account_id=_USER_UUID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = False
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_mock_config(forwarder_type: str = "sentry", **overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.forwarder_type = forwarder_type
    cfg.enabled = overrides.get("enabled", True)
    cfg.config_json = overrides.get("config_json", {"dsn": "https://key@sentry.io/1"})
    cfg.last_test_at = overrides.get("last_test_at")
    cfg.last_test_ok = overrides.get("last_test_ok")
    return cfg


def test_is_configured_rejects_unknown_forwarder_type() -> None:
    assert _is_configured("unknown", {"unexpected": "secret"}) is False


def test_is_configured_requires_known_forwarder_credentials() -> None:
    assert _is_configured("sentry", {"dsn": "https://key@sentry.io/1"}) is True
    assert _is_configured("sentry", {"dsn": ""}) is False


# ── GET /api/v1/errors/forwarders ──────────────────────────────────────────


class TestListForwarders:
    """Tests for GET /api/v1/errors/forwarders."""

    def test_list_returns_all_types(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars_mock)))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get("/api/v1/errors/forwarders")

        assert resp.status_code == 200
        body = resp.json()
        assert "forwarders" in body
        types = {f["forwarder_type"] for f in body["forwarders"]}
        assert types == {"sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"}

    def test_list_shows_configured_status(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        sentry_cfg = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[sentry_cfg])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars_mock)))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get("/api/v1/errors/forwarders")

        assert resp.status_code == 200
        body = resp.json()
        sentry = next(f for f in body["forwarders"] if f["forwarder_type"] == "sentry")
        assert sentry["configured"] is True
        assert sentry["enabled"] is True

    def test_list_shows_unconfigured_as_false(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars_mock)))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get("/api/v1/errors/forwarders")

        assert resp.status_code == 200
        body = resp.json()
        sentry = next(f for f in body["forwarders"] if f["forwarder_type"] == "sentry")
        assert sentry["configured"] is False
        assert sentry["enabled"] is False

    def test_list_returns_501_on_programming_error(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, "table not found"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get("/api/v1/errors/forwarders")

        assert resp.status_code == 501
        assert "migration" in resp.json()["detail"].lower()

    def test_list_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(side_effect=SQLAlchemyError("connection refused"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get("/api/v1/errors/forwarders")

        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.json()["detail"].lower()

    def test_list_returns_500_on_generic_error(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("unexpected"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get("/api/v1/errors/forwarders")

        assert resp.status_code == 500

    def test_list_returns_403_when_no_org(self, no_org_client: TestClient) -> None:
        resp = no_org_client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Organisation membership required"

    def test_list_returns_402_when_gated(self, gated_client: TestClient) -> None:
        resp = gated_client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 402
        assert "not available on your plan" in resp.json()["detail"].lower()


# ── PUT /api/v1/errors/forwarders/{forwarder_type} ─────────────────────────


class TestConfigureForwarder:
    """Tests for PUT /api/v1/errors/forwarders/{forwarder_type}."""

    def test_configure_new_forwarder(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=execute_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.put(
                "/api/v1/errors/forwarders/sentry",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}, "enabled": True},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["forwarder_type"] == "sentry"
        assert body["enabled"] is True
        mock_session.add.assert_called_once()

    def test_configure_existing_forwarder_partial_update(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", enabled=False, config_json={})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.put(
                "/api/v1/errors/forwarders/sentry",
                json={"enabled": True},
            )

        assert resp.status_code == 200
        assert existing.enabled is True

    def test_configure_unknown_type_returns_404(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/errors/forwarders/unknown",
            json={"config_json": {}},
        )
        assert resp.status_code == 404
        assert "unknown" in resp.json()["detail"].lower()

    def test_configure_requires_admin(self, viewer_client: TestClient) -> None:
        resp = viewer_client.put(
            "/api/v1/errors/forwarders/sentry",
            json={"config_json": {"dsn": "https://key@sentry.io/1"}},
        )
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    def test_configure_returns_501_on_programming_error(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, "table not found"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.put(
                "/api/v1/errors/forwarders/sentry",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 501
        assert "migration" in resp.json()["detail"].lower()

    def test_configure_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(side_effect=SQLAlchemyError("connection refused"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with patch("modulo.api.routes.error_forwarder_config.set_rls_org"):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.put(
                "/api/v1/errors/forwarders/sentry",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 503

    def test_configure_returns_403_when_no_org(self, no_org_client: TestClient) -> None:
        resp = no_org_client.put(
            "/api/v1/errors/forwarders/sentry",
            json={"config_json": {"dsn": "https://key@sentry.io/1"}},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Organisation membership required"

    def test_configure_returns_402_when_gated(self, gated_client: TestClient) -> None:
        resp = gated_client.put(
            "/api/v1/errors/forwarders/sentry",
            json={"config_json": {"dsn": "https://key@sentry.io/1"}},
        )
        assert resp.status_code == 402


# ── POST /api/v1/errors/forwarders/{forwarder_type}/test ───────────────────


class TestTestForwarder:
    """Tests for POST /api/v1/errors/forwarders/{forwarder_type}/test."""

    def test_test_connection_success(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=True)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "successfully" in body["message"].lower()

    def test_test_connection_failure(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=False)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False

    def test_test_connection_timeout(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        async def _timeout_forward(*args, **kwargs) -> bool:
            raise TimeoutError("timed out")

        mock_fwd = _make_mock_forwarder()
        mock_fwd.forward = _timeout_forward

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False

    def test_test_connection_forwarder_exception(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder()
        mock_fwd.forward = AsyncMock(side_effect=RuntimeError("network error"))

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False

    def test_test_forwarder_not_implemented(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars_mock)))
        mock_session.scalar_one_or_none = MagicMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=None),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "not found" in body["message"].lower()

    def test_test_unknown_type_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/errors/forwarders/unknown/test",
            json={"config_json": {}},
        )
        assert resp.status_code == 404
        assert "unknown" in resp.json()["detail"].lower()

    def test_test_connection_saves_result(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=True)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 200
        assert existing.last_test_ok is True
        assert existing.last_test_at is not None

    def test_test_connection_save_fails_returns_501(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry")
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=execute_result)
        mock_session.flush = AsyncMock(side_effect=ProgrammingError("stmt", {}, "table not found"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=True)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 501
        assert "migration" in resp.json()["detail"].lower()

    def test_test_returns_403_when_no_org(self, no_org_client: TestClient) -> None:
        resp = no_org_client.post(
            "/api/v1/errors/forwarders/sentry/test",
            json={"config_json": {"dsn": "https://key@sentry.io/1"}},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Organisation membership required"

    def test_test_returns_402_when_gated(self, gated_client: TestClient) -> None:
        resp = gated_client.post(
            "/api/v1/errors/forwarders/sentry/test",
            json={"config_json": {"dsn": "https://key@sentry.io/1"}},
        )
        assert resp.status_code == 402

    def test_test_with_incomplete_config_reads_saved_config(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://saved@sentry.io/1"})
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=scalar_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=True)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        forwarded_config = mock_fwd.forward.call_args[0][3]
        assert forwarded_config.get("dsn") == "https://saved@sentry.io/1"

    def test_test_connection_save_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        existing = _make_mock_config("sentry", config_json={"dsn": "https://key@sentry.io/1"})
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=execute_result)
        mock_session.flush = AsyncMock(side_effect=SQLAlchemyError("connection refused"))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=True)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/1"}},
            )

        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.json()["detail"].lower()

    def test_test_connection_no_saved_config_and_incomplete_request(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars_mock)))
        mock_session.scalar_one_or_none = MagicMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        mock_fwd = _make_mock_forwarder(ok=False)

        with (
            patch("modulo.api.routes.error_forwarder_config.set_rls_org"),
            patch("modulo.api.routes.error_forwarder_config.get_forwarder", return_value=mock_fwd),
        ):
            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        # forwarder was called with empty config
        forwarded_config = mock_fwd.forward.call_args[0][3]
        assert forwarded_config == {}
