"""Tests for error tracking API error handling — ProgrammingError → 501, SQLAlchemyError → 503."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.auth.jwt import AuthenticatedPrincipal
from tests.unit.api.plan_stubs import all_features

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PROGRAMMING_ERROR = ProgrammingError("mock", {}, None)
_SQLALCHEMY_ERROR = SQLAlchemyError("mock", {}, None)


@pytest.fixture(autouse=True)
def _reset_error_state(monkeypatch: pytest.MonkeyPatch):
    from modulo.api.routes import errors as errors_module

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key-that-is-long-enough")
    monkeypatch.setenv("FERNET_KEY", "unit-test-fernet-key-that-is-long-enough")
    monkeypatch.setenv("REDIS_URL", "")
    errors_module._key_store = None
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
    session.get = AsyncMock(return_value=None)
    return session


def _override_user():
    async def _inner():
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
        )

    return _inner


def _override_plan_context():
    from modulo.api.dependencies import get_plan_context

    async def _inner():
        return all_features()

    return {get_plan_context: _inner}


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


# ===========================================================================
# App factories
# ===========================================================================


def _make_errors_app():
    import modulo.api.routes.errors as mod

    app = FastAPI()
    app.include_router(mod.router)
    session = _make_mock_session()

    async def _override_db():
        return session

    from modulo.api.dependencies import get_db_session
    from modulo.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = _override_user()
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides.update(_override_plan_context())
    return app


def _make_rules_app():
    import modulo.api.routes.error_notification_rules as mod

    app = FastAPI()
    app.include_router(mod.router)
    session = _make_mock_session()

    async def _override_db():
        return session

    from modulo.api.dependencies import get_db_session as get_db
    from modulo.auth.dependencies import get_current_user as get_user

    app.dependency_overrides[get_user] = _override_user()
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides.update(_override_plan_context())
    return app


def _make_fwd_app():
    import modulo.api.routes.error_forwarder_config as mod

    app = FastAPI()
    app.include_router(mod.router)
    session = _make_mock_session()

    async def _override_db():
        return session

    from modulo.api.dependencies import get_db_session as get_db
    from modulo.auth.dependencies import get_current_user as get_user

    app.dependency_overrides[get_user] = _override_user()
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides.update(_override_plan_context())
    return app


# ===========================================================================
# Route definitions: (name, app_factory, mock_target, request_builder, need_group_mock)
# The request_builder returns a (client, response) tuple so we can inspect it.
# ===========================================================================

_EP = "modulo.api.routes.errors"
_RP = "modulo.api.routes.error_notification_rules"
_FP = "modulo.api.routes.error_forwarder_config"

_ERROR_ENDPOINTS: list[tuple[str, object, str, object, bool, bool, bool]] = [
    (
        "ingest",
        _make_errors_app,
        f"{_EP}._service.ingest_batch",
        lambda c: c.post("/api/v1/errors/ingest", json=INGEST_PAYLOAD, headers={"X-Modulo-Error-Token": "test"}),
        False,
        True,
        True,
    ),
    (
        "ingest_public",
        _make_errors_app,
        f"{_EP}._service.ingest_batch",
        lambda c: c.post("/api/v1/errors/ingest/public", json=INGEST_PAYLOAD),
        False,
        False,
        True,
    ),
    ("list_groups", _make_errors_app, f"{_EP}.get_error_groups", lambda c: c.get("/api/v1/errors"), False, False, True),
    (
        "get_group_detail",
        _make_errors_app,
        f"{_EP}.get_error_group",
        lambda c: c.get(f"/api/v1/errors/{uuid.uuid4()}"),
        False,
        False,
        True,
    ),
    (
        "patch_group",
        _make_errors_app,
        f"{_EP}.update_error_group",
        lambda c: c.patch(f"/api/v1/errors/{uuid.uuid4()}", json={"status": "resolved"}),
        False,
        False,
        True,
    ),
    (
        "list_events",
        _make_errors_app,
        f"{_EP}.get_error_events_by_group",
        lambda c: c.get(f"/api/v1/errors/{uuid.uuid4()}/events"),
        True,
        False,
        True,
    ),
    (
        "list_rules",
        _make_rules_app,
        f"{_RP}.select",
        lambda c: c.get("/api/v1/errors/notification-rules"),
        False,
        False,
        False,
    ),
    (
        "create_rule",
        _make_rules_app,
        f"{_RP}.select",
        lambda c: c.post("/api/v1/errors/notification-rules", json=CREATE_RULE_PAYLOAD),
        False,
        False,
        False,
    ),
    (
        "update_rule",
        _make_rules_app,
        f"{_RP}.select",
        lambda c: c.put(f"/api/v1/errors/notification-rules/{uuid.uuid4()}", json={"name": "Updated"}),
        False,
        False,
        False,
    ),
    (
        "delete_rule",
        _make_rules_app,
        f"{_RP}.select",
        lambda c: c.delete(f"/api/v1/errors/notification-rules/{uuid.uuid4()}"),
        False,
        False,
        False,
    ),
    (
        "list_forwarders",
        _make_fwd_app,
        f"{_FP}.select",
        lambda c: c.get("/api/v1/errors/forwarders"),
        False,
        False,
        False,
    ),
    (
        "configure_forwarder",
        _make_fwd_app,
        f"{_FP}.select",
        lambda c: c.put(
            "/api/v1/errors/forwarders/sentry",
            json={"enabled": True, "config_json": {"dsn": "https://key@sentry.io/123"}},
        ),
        False,
        False,
        False,
    ),
    (
        "test_forwarder",
        _make_fwd_app,
        f"{_FP}.select",
        lambda c: c.post(
            "/api/v1/errors/forwarders/sentry/test", json={"config_json": {"dsn": "https://key@sentry.io/123"}}
        ),
        False,
        False,
        False,
    ),
]


class TestErrorDatabaseErrors:
    """Parametrized: 13 endpoints with 2 error types = 26 cases collapsed."""

    @pytest.mark.parametrize(
        ("idx", "error_type", "expected_status", "detail_check"),
        [
            pytest.param(i, "programming", 501, "migrations", id=f"{_ERROR_ENDPOINTS[i][0]}_501")
            for i in range(len(_ERROR_ENDPOINTS))
        ]
        + [
            pytest.param(i, "sqlalchemy", 503, None, id=f"{_ERROR_ENDPOINTS[i][0]}_503")
            for i in range(len(_ERROR_ENDPOINTS))
        ],
    )
    def test_error_returns_expected(
        self, idx: int, error_type: str, expected_status: int, detail_check: str | None
    ) -> None:
        name, app_factory, mock_target, request_builder, need_group_mock, needs_hmac, is_async = _ERROR_ENDPOINTS[idx]

        app = app_factory()
        error = _PROGRAMMING_ERROR if error_type == "programming" else _SQLALCHEMY_ERROR
        mock_type = AsyncMock if is_async else MagicMock

        hmac_patch = None
        if needs_hmac:
            from modulo.api.routes.errors import _get_key_store

            store = _get_key_store()
            hmac_patch = patch.object(store, "verify_hmac", AsyncMock(return_value=True))
            hmac_patch.start()

        try:
            if need_group_mock:
                mock_group = MagicMock()
                mock_group.fingerprint = "test-fp"
                with (
                    patch(f"{_EP}.get_error_group", AsyncMock(return_value=mock_group)),
                    patch(mock_target, mock_type(side_effect=error)),
                ):
                    client = TestClient(app)
                    resp = request_builder(client)
            else:
                with patch(mock_target, mock_type(side_effect=error), create=True):
                    client = TestClient(app)
                    resp = request_builder(client)
        finally:
            if hmac_patch:
                hmac_patch.stop()

        assert resp.status_code == expected_status, f"{name}: expected {expected_status}, got {resp.status_code}"
        if detail_check:
            assert detail_check in resp.json()["detail"].lower()
