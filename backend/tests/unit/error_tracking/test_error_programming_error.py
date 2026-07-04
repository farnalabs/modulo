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


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    from modulo.api.routes import errors as errors_module
    errors_module._public_rate_limit.clear()
    errors_module._public_daily_event_count.clear()


def _make_mock_session():
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
    session.add = MagicMock()
    return session


def _make_errors_override(session):
    async def _override_db():
        return session

    async def _override_user():
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
        )
    return _override_db, _override_user


ERRORS_APP = FastAPI()
from modulo.api.routes.errors import router as errors_router
ERRORS_APP.include_router(errors_router)
from modulo.api.dependencies import get_db_session as err_get_db
from modulo.auth.dependencies import get_current_user as err_get_user


RULES_APP = FastAPI()
from modulo.api.routes.error_notification_rules import router as rules_router
RULES_APP.include_router(rules_router)
from modulo.api.dependencies import get_db_session as rule_get_db
from modulo.auth.dependencies import get_current_user as rule_get_user


FWD_APP = FastAPI()
from modulo.api.routes.error_forwarder_config import router as fwd_router
FWD_APP.include_router(fwd_router)
from modulo.api.dependencies import get_db_session as fwd_get_db
from modulo.auth.dependencies import get_current_user as fwd_get_user


INGEST_PAYLOAD = {
    "events": [{
        "level": "error", "message": "test error",
        "source": "frontend", "environment": "test",
    }]
}

CREATE_RULE_PAYLOAD = {
    "name": "Test Rule", "condition_level": "error",
    "condition_min_count": 1, "action_type": "in_app", "cooldown_seconds": 300,
}


# ---------------------------------------------------------------------------
# errors.py
# ---------------------------------------------------------------------------


def test_ingest_programming_error_returns_501():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors._service.ingest_batch",
               AsyncMock(side_effect=_PROGRAMMING_ERROR)):
        from modulo.api.routes.errors import _get_key_store
        store = _get_key_store()
        with patch.object(store, "verify_hmac", AsyncMock(return_value=True)):
            client = TestClient(app)
            resp = client.post("/api/v1/errors/ingest", json=INGEST_PAYLOAD,
                               headers={"X-Modulo-Error-Token": "test"})
            assert resp.status_code == 501
            assert "migrations" in resp.json()["detail"].lower()

    app.dependency_overrides.clear()


def test_ingest_sqlalchemy_error_returns_503():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors._service.ingest_batch",
               AsyncMock(side_effect=_SQLALCHEMY_ERROR)):
        from modulo.api.routes.errors import _get_key_store
        store = _get_key_store()
        with patch.object(store, "verify_hmac", AsyncMock(return_value=True)):
            client = TestClient(app)
            resp = client.post("/api/v1/errors/ingest", json=INGEST_PAYLOAD,
                               headers={"X-Modulo-Error-Token": "test"})
            assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_ingest_public_programming_error_returns_501():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors._service.ingest_batch",
               AsyncMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.post("/api/v1/errors/ingest/public", json=INGEST_PAYLOAD)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    app.dependency_overrides.clear()


def test_ingest_public_sqlalchemy_error_returns_503():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors._service.ingest_batch",
               AsyncMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.post("/api/v1/errors/ingest/public", json=INGEST_PAYLOAD)
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_list_groups_programming_error_returns_501():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors.get_error_groups",
               AsyncMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_list_groups_sqlalchemy_error_returns_503():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors.get_error_groups",
               AsyncMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_get_group_detail_programming_error_returns_501():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors.get_error_group",
               AsyncMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.get(f"/api/v1/errors/{uuid.uuid4()}")
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_get_group_detail_sqlalchemy_error_returns_503():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors.get_error_group",
               AsyncMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.get(f"/api/v1/errors/{uuid.uuid4()}")
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_patch_group_programming_error_returns_501():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors.update_error_group",
               AsyncMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.patch(f"/api/v1/errors/{uuid.uuid4()}", json={"status": "resolved"})
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_patch_group_sqlalchemy_error_returns_503():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    with patch("modulo.api.routes.errors.update_error_group",
               AsyncMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.patch(f"/api/v1/errors/{uuid.uuid4()}", json={"status": "resolved"})
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_list_events_programming_error_returns_501():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    mock_group = MagicMock()
    mock_group.fingerprint = "test-fp"
    with patch("modulo.api.routes.errors.get_error_group",
               AsyncMock(return_value=mock_group)):
        with patch("modulo.api.routes.errors.get_error_events_by_group",
                   AsyncMock(side_effect=_PROGRAMMING_ERROR)):
            client = TestClient(app)
            resp = client.get(f"/api/v1/errors/{uuid.uuid4()}/events")
            assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_list_events_sqlalchemy_error_returns_503():
    app = ERRORS_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[err_get_user] = override_user
    app.dependency_overrides[err_get_db] = override_db

    mock_group = MagicMock()
    mock_group.fingerprint = "test-fp"
    with patch("modulo.api.routes.errors.get_error_group",
               AsyncMock(return_value=mock_group)):
        with patch("modulo.api.routes.errors.get_error_events_by_group",
                   AsyncMock(side_effect=_SQLALCHEMY_ERROR)):
            client = TestClient(app)
            resp = client.get(f"/api/v1/errors/{uuid.uuid4()}/events")
            assert resp.status_code == 503

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# error_notification_rules.py
# ---------------------------------------------------------------------------


def test_list_rules_programming_error_returns_501():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.get("/api/v1/errors/notification-rules")
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_list_rules_sqlalchemy_error_returns_503():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.get("/api/v1/errors/notification-rules")
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_create_rule_programming_error_returns_501():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.post("/api/v1/errors/notification-rules", json=CREATE_RULE_PAYLOAD)
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_create_rule_sqlalchemy_error_returns_503():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.post("/api/v1/errors/notification-rules", json=CREATE_RULE_PAYLOAD)
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_update_rule_programming_error_returns_501():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.put(f"/api/v1/errors/notification-rules/{uuid.uuid4()}", json={"name": "Updated"})
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_update_rule_sqlalchemy_error_returns_503():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.put(f"/api/v1/errors/notification-rules/{uuid.uuid4()}", json={"name": "Updated"})
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_delete_rule_programming_error_returns_501():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.delete(f"/api/v1/errors/notification-rules/{uuid.uuid4()}")
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_delete_rule_sqlalchemy_error_returns_503():
    app = RULES_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[rule_get_user] = override_user
    app.dependency_overrides[rule_get_db] = override_db

    with patch("modulo.api.routes.error_notification_rules.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.delete(f"/api/v1/errors/notification-rules/{uuid.uuid4()}")
        assert resp.status_code == 503

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# error_forwarder_config.py
# ---------------------------------------------------------------------------


def test_list_forwarders_programming_error_returns_501():
    app = FWD_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[fwd_get_user] = override_user
    app.dependency_overrides[fwd_get_db] = override_db

    with patch("modulo.api.routes.error_forwarder_config.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_list_forwarders_sqlalchemy_error_returns_503():
    app = FWD_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[fwd_get_user] = override_user
    app.dependency_overrides[fwd_get_db] = override_db

    with patch("modulo.api.routes.error_forwarder_config.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.get("/api/v1/errors/forwarders")
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_configure_forwarder_programming_error_returns_501():
    app = FWD_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[fwd_get_user] = override_user
    app.dependency_overrides[fwd_get_db] = override_db

    with patch("modulo.api.routes.error_forwarder_config.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.put("/api/v1/errors/forwarders/sentry",
                          json={"enabled": True, "config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_configure_forwarder_sqlalchemy_error_returns_503():
    app = FWD_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[fwd_get_user] = override_user
    app.dependency_overrides[fwd_get_db] = override_db

    with patch("modulo.api.routes.error_forwarder_config.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.put("/api/v1/errors/forwarders/sentry",
                          json={"enabled": True, "config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_test_forwarder_programming_error_returns_501():
    app = FWD_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[fwd_get_user] = override_user
    app.dependency_overrides[fwd_get_db] = override_db

    with patch("modulo.api.routes.error_forwarder_config.select",
               MagicMock(side_effect=_PROGRAMMING_ERROR)):
        client = TestClient(app)
        resp = client.post("/api/v1/errors/forwarders/sentry/test",
                           json={"config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 501

    app.dependency_overrides.clear()


def test_test_forwarder_sqlalchemy_error_returns_503():
    app = FWD_APP
    session = _make_mock_session()
    override_db, override_user = _make_errors_override(session)
    app.dependency_overrides[fwd_get_user] = override_user
    app.dependency_overrides[fwd_get_db] = override_db

    with patch("modulo.api.routes.error_forwarder_config.select",
               MagicMock(side_effect=_SQLALCHEMY_ERROR)):
        client = TestClient(app)
        resp = client.post("/api/v1/errors/forwarders/sentry/test",
                           json={"config_json": {"dsn": "https://key@sentry.io/123"}})
        assert resp.status_code == 503

    app.dependency_overrides.clear()
