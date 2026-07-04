"""Tests for error tracking API error handling — ProgrammingError → 501, SQLAlchemyError → 503."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.auth.jwt import AuthenticatedPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PROGRAMMING_ERROR = ProgrammingError("mock", {}, None)
_SQLALCHEMY_ERROR = SQLAlchemyError("mock", {}, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session() -> MagicMock:
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
    return session


def _make_admin_app():
    app = FastAPI()

    from modulo.api.routes.errors import router as errors_router
    from modulo.api.routes.error_notification_rules import router as rules_router
    from modulo.api.routes.error_forwarder_config import router as forwarder_router

    app.include_router(errors_router)
    app.include_router(rules_router)
    app.include_router(forwarder_router)

    async def _override_user():
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
        )

    from modulo.api.dependencies import get_db_session
    from modulo.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = _override_user
    return app, get_db_session


INGEST_PAYLOAD = {
    "events": [
        {
            "level": "error",
            "message": "test error",
            "source": "frontend",
            "environment": "test",
        }
    ]
}

CREATE_RULE_PAYLOAD = {
    "name": "Test Rule",
    "condition_level": "error",
    "condition_min_count": 1,
    "action_type": "in_app",
    "cooldown_seconds": 300,
}


# ---------------------------------------------------------------------------
# errors.py — ProgrammingError → 501
# ---------------------------------------------------------------------------


class TestErrorsProgrammingError:
    def _make_app(self):
        app, get_db_session = _make_admin_app()

        async def _override_db():
            session = _make_mock_session()
            begin_cm = AsyncMock()
            begin_cm.__aenter__.side_effect = _PROGRAMMING_ERROR
            begin_cm.__aexit__.return_value = False
            session.begin.return_value = begin_cm
            return session

        app.dependency_overrides[get_db_session] = _override_db
        return TestClient(app)

    def test_ingest_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/ingest", json=INGEST_PAYLOAD,
                           headers={"X-Modulo-Error-Token": "test"})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_ingest_public_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/ingest/public", json=INGEST_PAYLOAD)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_list_groups_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 501

    def test_get_group_detail_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.get(f"/api/v1/errors/{uuid.uuid4()}")
        assert resp.status_code == 501

    def test_patch_group_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.patch(f"/api/v1/errors/{uuid.uuid4()}", json={"status": "resolved"})
        assert resp.status_code == 501

    def test_list_events_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.get(f"/api/v1/errors/{uuid.uuid4()}/events")
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# errors.py — SQLAlchemyError → 503
# ---------------------------------------------------------------------------


class TestErrorsSQLAlchemyError:
    def _make_app(self):
        app, get_db_session = _make_admin_app()

        async def _override_db():
            session = _make_mock_session()
            begin_cm = AsyncMock()
            begin_cm.__aenter__.side_effect = _SQLALCHEMY_ERROR
            begin_cm.__aexit__.return_value = False
            session.begin.return_value = begin_cm
            return session

        app.dependency_overrides[get_db_session] = _override_db
        return TestClient(app)

    def test_ingest_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/ingest", json=INGEST_PAYLOAD,
                           headers={"X-Modulo-Error-Token": "test"})
        assert resp.status_code == 503

    def test_ingest_public_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/ingest/public", json=INGEST_PAYLOAD)
        assert resp.status_code == 503

    def test_list_groups_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 503

    def test_get_group_detail_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.get(f"/api/v1/errors/{uuid.uuid4()}")
        assert resp.status_code == 503

    def test_patch_group_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.patch(f"/api/v1/errors/{uuid.uuid4()}", json={"status": "resolved"})
        assert resp.status_code == 503

    def test_list_events_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.get(f"/api/v1/errors/{uuid.uuid4()}/events")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# error_notification_rules.py — ProgrammingError → 501
# ---------------------------------------------------------------------------


class TestNotificationRulesProgrammingError:
    def _make_app(self):
        app, get_db_session = _make_admin_app()

        async def _override_db():
            session = _make_mock_session()
            begin_cm = AsyncMock()
            begin_cm.__aenter__.side_effect = _PROGRAMMING_ERROR
            begin_cm.__aexit__.return_value = False
            session.begin.return_value = begin_cm
            return session

        app.dependency_overrides[get_db_session] = _override_db
        return TestClient(app)

    def test_list_rules_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.get("/api/v1/errors/notification-rules")
        assert resp.status_code == 501

    def test_create_rule_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/notification-rules", json=CREATE_RULE_PAYLOAD)
        assert resp.status_code == 501

    def test_update_rule_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.put(f"/api/v1/errors/notification-rules/{uuid.uuid4()}", json={"name": "Updated"})
        assert resp.status_code == 501

    def test_delete_rule_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.delete(f"/api/v1/errors/notification-rules/{uuid.uuid4()}")
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# error_notification_rules.py — SQLAlchemyError → 503
# ---------------------------------------------------------------------------


class TestNotificationRulesSQLAlchemyError:
    def _make_app(self):
        app, get_db_session = _make_admin_app()

        async def _override_db():
            session = _make_mock_session()
            begin_cm = AsyncMock()
            begin_cm.__aenter__.side_effect = _SQLALCHEMY_ERROR
            begin_cm.__aexit__.return_value = False
            session.begin.return_value = begin_cm
            return session

        app.dependency_overrides[get_db_session] = _override_db
        return TestClient(app)

    def test_list_rules_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.get("/api/v1/errors/notification-rules")
        assert resp.status_code == 503

    def test_create_rule_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/notification-rules", json=CREATE_RULE_PAYLOAD)
        assert resp.status_code == 503

    def test_update_rule_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.put(f"/api/v1/errors/notification-rules/{uuid.uuid4()}", json={"name": "Updated"})
        assert resp.status_code == 503

    def test_delete_rule_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.delete(f"/api/v1/errors/notification-rules/{uuid.uuid4()}")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# error_forwarder_config.py — ProgrammingError → 501
# ---------------------------------------------------------------------------


class TestForwarderConfigProgrammingError:
    def _make_app(self):
        app, get_db_session = _make_admin_app()

        async def _override_db():
            session = _make_mock_session()
            begin_cm = AsyncMock()
            begin_cm.__aenter__.side_effect = _PROGRAMMING_ERROR
            begin_cm.__aexit__.return_value = False
            session.begin.return_value = begin_cm
            return session

        app.dependency_overrides[get_db_session] = _override_db
        return TestClient(app)

    def test_list_forwarders_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 501

    def test_configure_forwarder_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.put("/api/v1/errors/forwarders/sentry",
                          json={"enabled": True, "config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 501

    def test_test_forwarder_programming_error_returns_501(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/forwarders/sentry/test",
                           json={"config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# error_forwarder_config.py — SQLAlchemyError → 503
# ---------------------------------------------------------------------------


class TestForwarderConfigSQLAlchemyError:
    def _make_app(self):
        app, get_db_session = _make_admin_app()

        async def _override_db():
            session = _make_mock_session()
            begin_cm = AsyncMock()
            begin_cm.__aenter__.side_effect = _SQLALCHEMY_ERROR
            begin_cm.__aexit__.return_value = False
            session.begin.return_value = begin_cm
            return session

        app.dependency_overrides[get_db_session] = _override_db
        return TestClient(app)

    def test_list_forwarders_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 503

    def test_configure_forwarder_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.put("/api/v1/errors/forwarders/sentry",
                          json={"enabled": True, "config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 503

    def test_test_forwarder_sqlalchemy_error_returns_503(self):
        client = self._make_app()
        resp = client.post("/api/v1/errors/forwarders/sentry/test",
                           json={"config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 503
