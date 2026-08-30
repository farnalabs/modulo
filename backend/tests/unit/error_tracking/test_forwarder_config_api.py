"""Tests for error forwarder configuration API — list, configure, test."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

from modulo.auth.jwt import AuthenticatedPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_session_mock() -> MagicMock:
    session = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__.return_value = session
    begin_cm.__aexit__.return_value = None
    session.begin.return_value = begin_cm

    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    exec_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=exec_result)
    session.flush = AsyncMock()
    session.get = AsyncMock(return_value=None)
    return session


class _AllFeatures:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


def _make_app(org_role: str = "admin") -> FastAPI:
    app = FastAPI()

    from modulo.api.routes.error_forwarder_config import router as forwarder_config_router

    app.include_router(forwarder_config_router)

    async def _override_user():
        return AuthenticatedPrincipal(
            username=org_role,
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role=org_role,
        )

    async def _override_db():
        return _make_session_mock()

    from modulo.api.dependencies import get_db_session, get_plan_context
    from modulo.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db

    async def _override_plan_context() -> _AllFeatures:
        return _AllFeatures()

    app.dependency_overrides[get_plan_context] = _override_plan_context
    return app


class TestListForwarders:
    def test_returns_all_types_no_configs(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        resp = client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 200
        data = resp.json()
        assert "forwarders" in data
        types = {f["forwarder_type"] for f in data["forwarders"]}
        assert types == {"sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"}
        for fwd in data["forwarders"]:
            assert fwd["enabled"] is False
            assert fwd["configured"] is False
            assert fwd["last_test_ok"] is None


class TestConfigureForwarder:
    def test_creates_new_config(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        resp = client.put(
            "/api/v1/errors/forwarders/sentry",
            json={"enabled": True, "config_json": {"dsn": "https://key@sentry.io/123"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["forwarder_type"] == "sentry"
        assert data["enabled"] is True
        assert "dsn" in data["config_summary"]
        assert data["config_summary"]["dsn"] == "\u2022\u2022\u2022\u2022\u2022\u2022"

    def test_unknown_type_returns_404(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        resp = client.put("/api/v1/errors/forwarders/unknown", json={"enabled": True})
        assert resp.status_code == 404

    def test_non_admin_returns_403(self):
        from fastapi.testclient import TestClient

        client = TestClient(_make_app(org_role="viewer"))
        resp = client.put("/api/v1/errors/forwarders/sentry", json={"enabled": True})
        assert resp.status_code == 403


class TestTestConnection:
    def test_test_connection_success(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with patch(
            "modulo.api.routes.error_forwarder_config.get_forwarder",
        ) as mock_get:
            fwd_instance = AsyncMock()
            fwd_instance.forward.return_value = True
            mock_get.return_value = fwd_instance

            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@sentry.io/123"}},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_test_connection_failure(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with patch(
            "modulo.api.routes.error_forwarder_config.get_forwarder",
        ) as mock_get:
            fwd_instance = AsyncMock()
            fwd_instance.forward.return_value = False
            mock_get.return_value = fwd_instance

            resp = client.post(
                "/api/v1/errors/forwarders/datadog/test",
                json={"config_json": {"api_key": "test-key"}},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False

    def test_unknown_type_returns_404(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/v1/errors/forwarders/unknown/test", json={})
        assert resp.status_code == 404


class TestTestConnectionSsrfGuard:
    def test_sentry_dsn_to_private_host_rejected(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with patch(
            "modulo.api.routes.error_forwarder_config.get_forwarder",
        ) as mock_get:
            fwd_instance = AsyncMock()
            mock_get.return_value = fwd_instance

            resp = client.post(
                "/api/v1/errors/forwarders/sentry/test",
                json={"config_json": {"dsn": "https://key@127.0.0.1/123"}},
            )

        assert resp.status_code == 422
        assert "SSRF check failed" in resp.json()["detail"]
        fwd_instance.forward.assert_not_awaited()

    def test_datadog_site_rejected_when_guard_fails(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with (
            patch("modulo.api.routes.error_forwarder_config.get_forwarder") as mock_get,
            patch(
                "modulo.api.routes.error_forwarder_config.validate_outbound_url_async",
                new=AsyncMock(side_effect=ValueError("targets a private/internal network address")),
            ),
        ):
            fwd_instance = AsyncMock()
            mock_get.return_value = fwd_instance

            resp = client.post(
                "/api/v1/errors/forwarders/datadog/test",
                json={"config_json": {"api_key": "key", "site": "datadoghq.com"}},
            )

        assert resp.status_code == 422
        assert "SSRF check failed" in resp.json()["detail"]
        fwd_instance.forward.assert_not_awaited()


class TestConfigSummaryMasking:
    def test_sensitive_keys_are_masked(self):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        resp = client.put(
            "/api/v1/errors/forwarders/datadog",
            json={"config_json": {"api_key": "dd-key-123", "site": "datadoghq.com"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_summary"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
        assert data["config_summary"]["site"] == "datadoghq.com"
