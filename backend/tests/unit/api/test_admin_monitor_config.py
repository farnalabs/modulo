"""Unit tests for /api/v1/admin/monitor-config endpoints."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _admin_principal(role: str = "admin") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_ADMIN_ID if role == "admin" else _VIEWER_ID,
        org_role=role,
        is_system_admin=(role == "admin"),
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_client(session: AsyncMock, principal: AuthenticatedPrincipal) -> AsyncClient:
    from modulo.api.main import app

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: principal
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_entry(value: dict[str, Any] | None) -> MagicMock:
    entry = MagicMock()
    entry.value = value
    return entry


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    from modulo.api.main import app

    app.dependency_overrides.clear()


class TestGetMonitorConfig:
    @pytest.mark.anyio
    async def test_admin_gets_default_config_when_unset(self):
        session = _make_mock_session()
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backends"] == ["builtin"]
        assert body["sentry"] is None
        assert body["datadog_rum"] is None
        assert body["grafana_faro"] is None

    @pytest.mark.anyio
    async def test_admin_gets_stored_config_merged_with_defaults(self):
        session = _make_mock_session()
        stored = {
            "backends": ["sentry", "builtin"],
            "sentry": {"dsn": "https://key@sentry.io/1"},
            "datadog_rum": None,
            "grafana_faro": None,
        }
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            return_value=_make_entry(stored),
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backends"] == ["sentry", "builtin"]
        assert body["sentry"] == {"dsn": "https://key@sentry.io/1"}

    @pytest.mark.anyio
    async def test_non_admin_gets_403(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal(role="viewer")) as client:
            resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_missing_credentials_gets_403(self):
        session = _make_mock_session()
        from modulo.api.dependencies import get_db_session
        from modulo.api.main import app

        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        app.dependency_overrides[get_db_session] = lambda: session
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_programming_error_returns_501(self):
        session = _make_mock_session()
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", "params", Exception("boom")),
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code == 501
        assert "migration" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_sqlalchemy_error_returns_503(self):
        session = _make_mock_session()
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("connection lost"),
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code == 503

    @pytest.mark.anyio
    async def test_unexpected_error_returns_500(self):
        session = _make_mock_session()
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.get("/api/v1/admin/monitor-config")
        assert resp.status_code == 500


class TestSetMonitorConfig:
    @pytest.mark.anyio
    async def test_admin_updates_config(self):
        session = _make_mock_session()
        payload = {
            "backends": ["datadog_rum"],
            "datadog_rum": {"clientToken": "tok"},
            "sentry": None,
            "grafana_faro": None,
        }

        async def fake_set_config(session, key, value, updated_by=None):
            return _make_entry(value)

        with patch(
            "modulo.api.routes.admin_monitor_config.update_config",
            new_callable=AsyncMock,
            side_effect=fake_set_config,
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.put(
                    "/api/v1/admin/monitor-config",
                    json=payload,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["backends"] == ["datadog_rum"]
        assert body["datadog_rum"] == {"clientToken": "tok"}

    @pytest.mark.anyio
    async def test_sentry_enabled_without_dsn_rejected_422(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal()) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": ["sentry"], "sentry": {}},
            )
        assert resp.status_code == 422
        assert "dsn" in str(resp.json()["detail"])

    @pytest.mark.anyio
    async def test_sentry_enabled_with_null_config_rejected_422(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal()) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": ["sentry"], "sentry": None},
            )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_datadog_enabled_without_client_token_rejected_422(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal()) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": ["datadog_rum"], "datadog_rum": {"site": "datadoghq.com"}},
            )
        assert resp.status_code == 422
        assert "clientToken" in str(resp.json()["detail"])

    @pytest.mark.anyio
    async def test_grafana_enabled_without_url_rejected_422(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal()) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": ["grafana_faro"], "grafana_faro": {"apiKey": "k"}},
            )
        assert resp.status_code == 422
        assert "url" in str(resp.json()["detail"])

    @pytest.mark.anyio
    async def test_sentry_enabled_with_dsn_accepted(self):
        session = _make_mock_session()
        payload = {
            "backends": ["sentry", "builtin"],
            "sentry": {"dsn": "https://key@sentry.io/1"},
        }

        async def fake_set_config(session, key, value, updated_by=None):
            return _make_entry(value)

        with patch(
            "modulo.api.routes.admin_monitor_config.update_config",
            new_callable=AsyncMock,
            side_effect=fake_set_config,
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.put(
                    "/api/v1/admin/monitor-config",
                    json=payload,
                )
        assert resp.status_code == 200
        assert resp.json()["backends"] == ["sentry", "builtin"]

    @pytest.mark.anyio
    async def test_builtin_only_config_still_accepted(self):
        session = _make_mock_session()

        async def fake_set_config(session, key, value, updated_by=None):
            return _make_entry(value)

        with patch(
            "modulo.api.routes.admin_monitor_config.update_config",
            new_callable=AsyncMock,
            side_effect=fake_set_config,
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.put(
                    "/api/v1/admin/monitor-config",
                    json={"backends": ["builtin"]},
                )
        assert resp.status_code == 200
        assert resp.json()["backends"] == ["builtin"]

    @pytest.mark.anyio
    async def test_unknown_backend_rejected_422(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal()) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": ["nope"]},
            )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_empty_backends_rejected_422(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal()) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": []},
            )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_non_admin_gets_403(self):
        session = _make_mock_session()
        async with _make_client(session, _admin_principal(role="viewer")) as client:
            resp = await client.put(
                "/api/v1/admin/monitor-config",
                json={"backends": ["builtin"]},
            )
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_programming_error_returns_501(self):
        session = _make_mock_session()
        with patch(
            "modulo.api.routes.admin_monitor_config.update_config",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", "params", Exception("boom")),
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.put(
                    "/api/v1/admin/monitor-config",
                    json={"backends": ["builtin"]},
                )
        assert resp.status_code == 501

    @pytest.mark.anyio
    async def test_sqlalchemy_error_returns_503(self):
        session = _make_mock_session()
        with patch(
            "modulo.api.routes.admin_monitor_config.update_config",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("connection lost"),
        ):
            async with _make_client(session, _admin_principal()) as client:
                resp = await client.put(
                    "/api/v1/admin/monitor-config",
                    json={"backends": ["builtin"]},
                )
        assert resp.status_code == 503
