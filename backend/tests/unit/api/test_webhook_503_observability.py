"""Structured reason logging on the webhook/health 503 raise paths.

2026-09-04T14:48Z incident: ~8/10 GitHub pr-review webhook POSTs to
app.modulo.run failed with HTTP 503 and NO persisted cause — the fail-closed
raise sites logged either a bare log code or nothing, so the reason was lost
with the response. These tests lock in the contract that every 503 raise site
in webhooks.py and the /healthz/ready aggregation emits a structured record
(route, machine-readable reason code, exception class name, short safe
detail) BEFORE raising. Removing the helper call from a raise site fails the
corresponding test.
"""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.db_error_reporting import log_service_unavailable
from modulo.api.dependencies import (
    _get_engine,
    get_db_session,
    get_plan_context,
    get_system_db_session,
)
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.conftest import make_system_session_mock

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_EVENT_ID = uuid.uuid4()
_ERROR_REPORTING_LOGGER = "modulo.api.db_error_reporting"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _structured_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """All structured records emitted by the shared 503-reporting helper."""
    return [r for r in caplog.records if r.name == _ERROR_REPORTING_LOGGER]


def _apply_common_overrides() -> None:
    """Mirror the shared webhook-endpoint client fixture's dependency stubs."""
    principal = AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


def _make_failing_session(exc: Exception) -> AsyncMock:
    """AsyncMock session whose ``begin()`` transaction raises on entry.

    Mirrors the transaction-entry shape of the webhook routes: the raise
    surfaces from ``async with session.begin():`` so the route's typed
    ``except SQLAlchemyError`` clause (not the handle_db_errors decorator)
    converts it to the designed fail-closed 503 — the exact path a transient
    production database error takes.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=exc)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def failing_session_client() -> TestClient:
    """TestClient whose app DB session raises SQLAlchemyError on transaction begin."""
    mock_session = _make_failing_session(SQLAlchemyError("pool timeout"))
    mock_system_session = make_system_session_mock(trigger_org_id=_ORG_ID)

    async def override_session() -> AsyncMock:
        yield mock_session

    async def override_system_session() -> AsyncMock:
        yield mock_system_session

    _apply_common_overrides()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def working_session_client() -> TestClient:
    """TestClient whose app DB session behaves like the happy-path mock.

    The trigger read resolves a trigger with ``config_json = {}`` so the
    route-level HMAC validation is skipped and the delivery reaches the
    snapshot/engine stage of the transaction.
    """
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    trigger_mock = MagicMock()
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.active = True
    trigger_mock.config_json = {}
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.add = MagicMock()
    mock_system_session = make_system_session_mock(trigger_org_id=_ORG_ID)

    async def override_session() -> AsyncMock:
        yield mock_session

    async def override_system_session() -> AsyncMock:
        yield mock_system_session

    _apply_common_overrides()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_helper_emits_structured_record(caplog: pytest.LogCaptureFixture) -> None:
    """The helper is the single source of the structured 503 record: the
    message text AND the machine-readable ``service_unavailable`` extra must
    both carry route, reason, exception class, and detail."""
    with caplog.at_level(logging.WARNING):
        log_service_unavailable(
            "db_transient",
            SQLAlchemyError("connection reset by server"),
            route="webhooks.receive_webhook",
            detail="transient database error; delivery failed closed",
        )

    records = _structured_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    payload = record.__dict__["service_unavailable"]
    assert payload["route"] == "webhooks.receive_webhook"
    assert payload["reason"] == "db_transient"
    assert payload["exception_class"] == "SQLAlchemyError"
    assert payload["detail"] == "transient database error; delivery failed closed"
    assert "route=webhooks.receive_webhook" in record.getMessage()
    assert "reason=db_transient" in record.getMessage()


def test_helper_allows_missing_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Guards that fail closed without an exception (e.g. config gaps) still
    record route and reason; the exception class is None."""
    with caplog.at_level(logging.WARNING):
        log_service_unavailable(
            "system_bootstrap_degraded",
            route="webhooks.receive_webhook",
            detail="system database not provisioned",
        )

    records = _structured_records(caplog)
    assert len(records) == 1
    payload = records[0].__dict__["service_unavailable"]
    assert payload["exception_class"] is None
    assert payload["reason"] == "system_bootstrap_degraded"


def test_helper_default_level_is_error_with_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exception-backed records default to ERROR so they reach the only
    persisted/queryable sink: ErrorTrackingLogHandler ingests only records
    with levelno >= ERROR into error_events / the Error Dashboard."""
    with caplog.at_level(logging.WARNING):
        log_service_unavailable(
            "db_transient",
            SQLAlchemyError("connection reset by server"),
            route="webhooks.receive_webhook",
        )

    records = _structured_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_helper_default_level_is_warning_without_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Guard-style 503s raised without an exception keep the original
    WARNING severity (no traceback exists to persist)."""
    with caplog.at_level(logging.WARNING):
        log_service_unavailable(
            "system_bootstrap_degraded",
            route="webhooks.receive_webhook",
        )

    records = _structured_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def test_helper_explicit_level_overrides_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicit ``level`` argument wins over the exc-derived default."""
    with caplog.at_level(logging.WARNING):
        log_service_unavailable(
            "db_transient",
            SQLAlchemyError("pool timeout"),
            route="webhooks.receive_webhook",
            level=logging.WARNING,
        )

    records = _structured_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def test_receive_webhook_bootstrap_guard_logs_structured_reason(
    failing_session_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Degraded system engine -> 503, and the raise site must persist WHY
    (route + reason code) before raising. The record is logged at ERROR
    explicitly: with no ``exc`` the helper's derived default is WARNING,
    which ErrorTrackingLogHandler would NOT persist — an unprovisioned
    system DB is a full outage signal, not a routine guard."""
    with (
        patch("modulo.api.routes.webhooks.system_engine_is_fallback", return_value=True),
        caplog.at_level(logging.WARNING),
    ):
        resp = failing_session_client.post(
            f"/api/v1/triggers/{uuid.uuid4()}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 503
    records = _structured_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    payload = records[0].__dict__["service_unavailable"]
    assert payload["route"] == "webhooks.receive_webhook"
    assert payload["reason"] == "system_bootstrap_degraded"


def test_receive_webhook_db_transient_logs_structured_reason(
    failing_session_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient DB failure at transaction begin -> designed fail-closed
    503, with a persisted structured reason (db_transient + exception class)."""
    with caplog.at_level(logging.WARNING):
        resp = failing_session_client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 503
    records = _structured_records(caplog)
    assert len(records) == 1
    payload = records[0].__dict__["service_unavailable"]
    assert payload["route"] == "webhooks.receive_webhook"
    assert payload["reason"] == "db_transient"
    assert payload["exception_class"] == "SQLAlchemyError"
    assert payload["detail"] == "transient database error; webhook delivery failed closed"


def test_replay_webhook_db_transient_logs_structured_reason(
    failing_session_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Replay fail-closes 503 on a transient DB error with the same
    structured reason record, attributed to the replay route."""
    with caplog.at_level(logging.WARNING):
        resp = failing_session_client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{_EVENT_ID}",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 503
    records = _structured_records(caplog)
    assert len(records) == 1
    payload = records[0].__dict__["service_unavailable"]
    assert payload["route"] == "webhooks.replay_webhook"
    assert payload["reason"] == "db_transient"
    assert payload["exception_class"] == "SQLAlchemyError"


def test_receive_webhook_snapshot_lock_unavailable_logs_structured_reason(
    working_session_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The engine's snapshot advisory-lock exhaustion (FAR-527) fail-closes
    503 with a persisted snapshot_lock_unavailable reason record."""
    from modulo.core.exceptions import SnapshotLockNotAvailableError

    with (
        patch(
            "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
            new_callable=AsyncMock,
            side_effect=SnapshotLockNotAvailableError(),
        ),
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=MagicMock(id=uuid.uuid4()),
        ),
        patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks.set_rls_execution_context"),
        caplog.at_level(logging.WARNING),
    ):
        resp = working_session_client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 503
    records = _structured_records(caplog)
    assert len(records) == 1
    payload = records[0].__dict__["service_unavailable"]
    assert payload["route"] == "webhooks.receive_webhook"
    assert payload["reason"] == "snapshot_lock_unavailable"
    assert payload["exception_class"] == "SnapshotLockNotAvailableError"
    assert "snapshot lock unavailable" in payload["detail"]


async def test_cleanup_expired_db_transient_logs_structured_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cron-facing cleanup route fail-closes 503 on transient DB errors
    with the same structured reason record. Called directly (its
    ``require_permission`` dependency is outside the webhook TestClient
    fixtures) — the real handler body runs."""
    from modulo.api.routes.webhooks import cleanup_expired

    principal = TenantPrincipal(username="runner", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="runner")
    with caplog.at_level(logging.WARNING):
        failing_session = _make_failing_session(SQLAlchemyError("pool timeout"))
        with pytest.raises(HTTPException) as raised:
            await cleanup_expired(session=failing_session, principal=principal)

    assert raised.value.status_code == 503
    records = _structured_records(caplog)
    assert len(records) == 1
    payload = records[0].__dict__["service_unavailable"]
    assert payload["route"] == "webhooks.cleanup_expired"
    assert payload["reason"] == "db_transient"
    assert payload["exception_class"] == "SQLAlchemyError"


async def test_readiness_unavailable_logs_structured_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The /healthz/ready aggregation flips the response to 503 when a check
    reports unavailable, and must persist WHICH checks failed in the
    structured reason record."""
    from modulo.api.routes import health as health_routes

    ok = health_routes.CheckResult(status="ok")
    db_down = health_routes.CheckResult(status="unavailable", detail="database unreachable")
    with (
        patch.object(health_routes, "_check_database", new_callable=AsyncMock, return_value=db_down),
        patch.object(health_routes, "_check_redis", new_callable=AsyncMock, return_value=ok),
        patch.object(health_routes, "_check_checkpointer", new_callable=AsyncMock, return_value=ok),
        patch.object(health_routes, "_check_migrations", new_callable=AsyncMock, return_value=ok),
        patch.object(health_routes, "_check_saq_workers", new_callable=AsyncMock, return_value=ok),
        patch.object(health_routes, "_check_system_crons", new_callable=AsyncMock, return_value=ok),
        patch.object(health_routes, "_check_dispatcher_reconcile", new_callable=AsyncMock, return_value=ok),
        patch.object(health_routes, "_check_stale_run_recovery", new_callable=AsyncMock, return_value=ok),
        caplog.at_level(logging.WARNING),
    ):
        response = Response()
        result = await health_routes.readiness(response)

    assert response.status_code == 503
    assert result.status == "unavailable"
    records = _structured_records(caplog)
    assert len(records) == 1
    payload = records[0].__dict__["service_unavailable"]
    assert payload["route"] == "health.readiness"
    assert payload["reason"] == "readiness_check_unavailable"
    assert payload["exception_class"] is None
    assert "database" in payload["detail"]
