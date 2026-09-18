"""Route-level coverage tests for the trigger endpoints (FAR-618).

Complements ``test_triggers_endpoint.py`` (CRUD + config happy paths),
``test_triggers.py`` (helper units) and ``test_trigger_config_secrets.py`` by
covering the per-route DB error-convention matrices (ProgrammingError->501,
SQLAlchemyError->503, generic Exception->500), the 404/400 guard branches
(unknown trigger, wrong trigger type, missing cron expression), the cron
config update field branches (expression/timezone/snapshot/input_template),
the manual test-trigger surfaces (snapshot failure 500, OrgDeletedError 409/404)
and trigger-event cursor pagination.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.exceptions import OrgDeletedError
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")

_PREFIX = "modulo.api.routes.triggers."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_trigger(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TRIGGER_ID)
    t.pipeline_id = overrides.get("pipeline_id", _PIPELINE_ID)
    t.organisation_id = _ORG_ID
    t.trigger_type = overrides.get("trigger_type", "cron")
    t.active = overrides.get("active", True)
    t.max_concurrent_runs = overrides.get("max_concurrent_runs", 1)
    t.daily_spend_limit = overrides.get("daily_spend_limit", Decimal("1.50"))
    t.cron_expression = overrides.get("cron_expression", "0 * * * *")
    t.cron_timezone = overrides.get("cron_timezone", "UTC")
    t.last_fired_at = overrides.get("last_fired_at")
    t.next_fire_at = overrides.get("next_fire_at")
    t.created_by = _USER_ID
    t.account_id = _USER_ID
    t.created_at = _NOW
    t.config_json = overrides.get("config_json", {})
    t.deleted_at = None
    return t


def _trigger_result(triggers: list[MagicMock]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=triggers)))
    r.scalar_one_or_none = MagicMock(return_value=triggers[0] if triggers else None)
    r.scalar_one = MagicMock(return_value=len(triggers))
    r.one_or_none = MagicMock(return_value=None)
    r.scalar = MagicMock(return_value=len(triggers))
    return r


_STREAK = {
    "enabled": False,
    "streak": 0,
    "threshold": 0,
    "state": "unconfigured",
    "deactivated_reason": None,
    "last_outcomes": [],
}


def _happy_patches() -> list:
    return [
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        patch(f"{_PREFIX}anchor_trigger_streak_epoch", new_callable=AsyncMock),
        patch(f"{_PREFIX}clear_trigger_streak_after_reenable", new_callable=AsyncMock),
        patch(f"{_PREFIX}get_trigger_streak_status", new=AsyncMock(return_value=dict(_STREAK))),
        patch(f"{_PREFIX}_count_ongoing_runs", new_callable=AsyncMock, return_value=0),
    ]


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_trigger_result([]))
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _session_assert_error_matrix(
    http: TestClient,
    session: AsyncMock,
    *,
    method: str,
    url: str,
    json_body: dict | None = None,
) -> None:
    """Inject each failure on the handler's own DB statement (session.execute)."""
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        session.execute = AsyncMock(side_effect=exc)
        ctxs = list(_happy_patches())
        for c in ctxs:
            c.__enter__()
        try:
            kwargs = {"json": json_body} if json_body is not None else {}
            resp = getattr(http, method.lower())(url, **kwargs)
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
            session.execute = _make_session().execute
        assert resp.status_code == expected, f"{method} {url} {exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# GET /triggers — error mapping
# ---------------------------------------------------------------------------


def test_list_triggers_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _session_assert_error_matrix(http, session, method="get", url="/api/v1/triggers")


def test_list_triggers_with_filters_returns_200(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get("/api/v1/triggers", params={"pipeline_id": str(_PIPELINE_ID), "trigger_type": "cron"})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# PATCH /triggers/{id}/cron
# ---------------------------------------------------------------------------


def test_update_cron_config_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        ctxs = list(_happy_patches())
        ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(side_effect=exc)))
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/cron", json={"cron_expression": "0 * * * *"})
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
        assert resp.status_code == expected, resp.text


def test_update_cron_config_happy_path_updates_all_fields(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    trigger = _make_trigger()
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=trigger)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/cron",
            json={
                "cron_expression": "30 2 * * *",
                "cron_timezone": "UTC",
                "snapshot_id": "snap-1",
                "input_template": {"a": 1},
            },
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cron_expression"] == "30 2 * * *"
    assert body["input_template"] == {"a": 1}
    assert body["next_fire_at"] is not None


def test_update_cron_config_timezone_without_expression_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(
        patch(
            f"{_PREFIX}_load_trigger_for_update",
            new=AsyncMock(return_value=_make_trigger(cron_expression=None)),
        )
    )
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/cron", json={"cron_timezone": "UTC"})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 422, resp.text
    assert "Cron expression is required" in resp.json()["detail"]


def test_update_cron_config_wrong_type_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(
        patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=_make_trigger(trigger_type="webhook")))
    )
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/cron", json={"cron_expression": "0 * * * *"})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 400, resp.text


def test_update_cron_config_reenable_clears_streak(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    trigger = _make_trigger(active=False)
    clear_mock = AsyncMock()
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}clear_trigger_streak_after_reenable", new=clear_mock))
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=trigger)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/cron", json={"active": True})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    clear_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET /triggers/{id}/cron/preview
# ---------------------------------------------------------------------------


def test_preview_cron_schedule_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _session_assert_error_matrix(http, session, method="get", url=f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview")


def test_preview_cron_schedule_unknown_trigger_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_preview_cron_schedule_without_expression_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(cron_expression=None)]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 400, resp.text
    assert "no cron expression" in resp.json()["detail"]


def test_preview_cron_schedule_returns_times(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger()]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview", params={"count": 3})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["next_fire_times"]) == 3


# ---------------------------------------------------------------------------
# PATCH /triggers/{id}/polling
# ---------------------------------------------------------------------------


def test_update_polling_config_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        ctxs = list(_happy_patches())
        ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(side_effect=exc)))
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/polling", json={"poll_query": "x"})
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
        assert resp.status_code == expected, resp.text


def test_update_polling_config_wrong_type_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=_make_trigger())))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/polling", json={"poll_query": "x"})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 400, resp.text


def test_update_polling_config_happy_path_schedules_and_merges(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    trigger = _make_trigger(trigger_type="polling", active=False, config_json={"existing": "keep"})
    engine = MagicMock()
    engine.schedule_polling_trigger = AsyncMock()
    clear_mock = AsyncMock()
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}clear_trigger_streak_after_reenable", new=clear_mock))
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=trigger)))
    ctxs.append(patch(f"{_PREFIX}TriggerEngine", return_value=engine))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling",
            json={
                "active": True,
                "connector_instance_id": str(uuid.uuid4()),
                "poll_query": "SELECT 1",
                "condition_expression": "x",
                "poll_interval_seconds": 120,
                "snapshot_id": "snap-2",
                "daily_spend_limit": 5,
            },
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is True
    assert body["config_json"]["existing"] == "keep"
    engine.schedule_polling_trigger.assert_awaited_once()
    clear_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# PATCH /triggers/{id}/ongoing
# ---------------------------------------------------------------------------


def test_update_ongoing_config_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        ctxs = list(_happy_patches())
        ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(side_effect=exc)))
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/ongoing", json={"target_runs": 3})
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
        assert resp.status_code == expected, resp.text


def test_update_ongoing_config_wrong_type_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=_make_trigger())))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(f"/api/v1/triggers/{_TRIGGER_ID}/ongoing", json={"target_runs": 3})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 400, resp.text


def test_update_ongoing_config_happy_path_bumps_next_fire(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    trigger = _make_trigger(
        trigger_type="ongoing",
        active=False,
        config_json={"scan_interval_seconds": 60},
        max_concurrent_runs=1,
    )
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=trigger)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/ongoing",
            json={"active": True, "target_runs": 4, "scan_interval_seconds": 300, "input_template": {"b": 2}},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_concurrent_runs"] == 4
    assert body["config_json"]["scan_interval_seconds"] == 300
    assert body["in_flight"] == 0
    assert body["next_fire_at"] is not None


# ---------------------------------------------------------------------------
# POST /triggers/{id}/polling/test
# ---------------------------------------------------------------------------


def test_test_polling_condition_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    body = {"connector_instance_id": str(uuid.uuid4()), "poll_query": "q"}
    _url = f"/api/v1/triggers/{_TRIGGER_ID}/polling/test"
    _session_assert_error_matrix(http, session, method="post", url=_url, json_body=body)


def test_test_polling_condition_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling/test",
            json={"connector_instance_id": str(uuid.uuid4()), "poll_query": "q"},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_test_polling_condition_wrong_type_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(trigger_type="cron")]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling/test",
            json={"connector_instance_id": str(uuid.uuid4()), "poll_query": "q"},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 400, resp.text


def test_test_polling_condition_happy_path_delegates_to_engine(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(trigger_type="polling")]))
    engine = MagicMock()
    engine.evaluate_condition = AsyncMock(return_value={"status": "matched", "records": []})
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}TriggerEngine", return_value=engine))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling/test",
            json={"connector_instance_id": str(uuid.uuid4()), "poll_query": "q"},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "matched"
    engine.evaluate_condition.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /pipelines/{pipeline_id}/triggers — create
# ---------------------------------------------------------------------------


def _create_flush_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        session.flush = AsyncMock(side_effect=exc)
        ctxs = list(_happy_patches())
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
                json={"trigger_type": "manual"},
            )
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
            session.flush = AsyncMock()
        assert resp.status_code == expected, resp.text


def test_create_trigger_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    _create_flush_assert_error_matrix(client)


def test_create_trigger_cron_fields_on_webhook_type_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "webhook", "cron_expression": "0 * * * *"},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 400, resp.text
    assert "Only cron triggers" in resp.json()["detail"]


def test_create_trigger_manual_happy_path_returns_201(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "manual", "daily_spend_limit": 2},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 201, resp.text
    assert resp.json()["trigger_type"] == "manual"


# ---------------------------------------------------------------------------
# PUT /triggers/{id} — update
# ---------------------------------------------------------------------------


def test_update_trigger_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        ctxs = list(_happy_patches())
        ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(side_effect=exc)))
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.put(f"/api/v1/triggers/{_TRIGGER_ID}", json={"active": True})
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
        assert resp.status_code == expected, resp.text


def test_update_trigger_clears_ongoing_spend_limit_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(
        patch(
            f"{_PREFIX}_load_trigger_for_update",
            new=AsyncMock(return_value=_make_trigger(trigger_type="ongoing")),
        )
    )
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.put(f"/api/v1/triggers/{_TRIGGER_ID}", json={"daily_spend_limit": None})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 422, resp.text
    assert "ongoing triggers require daily_spend_limit" in resp.json()["detail"]


def test_update_trigger_happy_path_returns_serialized(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    trigger = _make_trigger(trigger_type="cron", active=False)
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}_load_trigger_for_update", new=AsyncMock(return_value=trigger)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.put(f"/api/v1/triggers/{_TRIGGER_ID}", json={"active": True, "max_concurrent_runs": 3})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is True
    assert body["max_concurrent_runs"] == 3
    assert body["streak_status"] == _STREAK


# ---------------------------------------------------------------------------
# DELETE / POST restore — CRUD via modulo.db.crud.trigger
# ---------------------------------------------------------------------------


_CRUD_TRIGGER = "modulo.db.crud.trigger"


def test_delete_trigger_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        ctxs = list(_happy_patches())
        ctxs.append(patch(f"{_CRUD_TRIGGER}.soft_delete_trigger", new=AsyncMock(side_effect=exc)))
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.delete(f"/api/v1/triggers/{_TRIGGER_ID}")
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
        assert resp.status_code == expected, resp.text


def test_delete_trigger_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_CRUD_TRIGGER}.soft_delete_trigger", new=AsyncMock(return_value=None)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.delete(f"/api/v1/triggers/{_TRIGGER_ID}")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_restore_trigger_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        ctxs = list(_happy_patches())
        ctxs.append(patch(f"{_CRUD_TRIGGER}.restore_trigger", new=AsyncMock(side_effect=exc)))
        for c in ctxs:
            c.__enter__()
        try:
            resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/restore")
        finally:
            for c in reversed(ctxs):
                c.__exit__(None, None, None)
        assert resp.status_code == expected, resp.text


def test_restore_trigger_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_CRUD_TRIGGER}.restore_trigger", new=AsyncMock(return_value=None)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/restore")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_restore_trigger_ongoing_happy_path_reanchors(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    anchor_mock = AsyncMock()
    clear_mock = AsyncMock()
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}anchor_trigger_streak_epoch", new=anchor_mock))
    ctxs.append(patch(f"{_PREFIX}clear_trigger_streak_after_reenable", new=clear_mock))
    restore = _make_trigger(trigger_type="ongoing")
    ctxs.append(patch(f"{_CRUD_TRIGGER}.restore_trigger", new=AsyncMock(return_value=restore)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/restore")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    anchor_mock.assert_awaited_once()
    clear_mock.assert_awaited_once()
    assert resp.json()["next_fire_at"] is not None


# ---------------------------------------------------------------------------
# POST /triggers/{id}/toggle
# ---------------------------------------------------------------------------


def test_toggle_trigger_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _session_assert_error_matrix(http, session, method="post", url=f"/api/v1/triggers/{_TRIGGER_ID}/toggle")


def test_toggle_trigger_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_toggle_trigger_happy_path_flips_and_anchors(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(active=False)]))
    anchor_mock = AsyncMock()
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}anchor_trigger_streak_epoch", new=anchor_mock))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    anchor_mock.assert_awaited_once()
    assert resp.json()["active"] is True


# ---------------------------------------------------------------------------
# POST /triggers/{id}/test — manual test runs + domain error mapping
# ---------------------------------------------------------------------------


def test_test_trigger_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _session_assert_error_matrix(http, session, method="post", url=f"/api/v1/triggers/{_TRIGGER_ID}/test", json_body={})


def test_test_trigger_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/test", json={})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_test_trigger_manual_happy_path_creates_run(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    snapshot = MagicMock()
    snapshot.id = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(trigger_type="manual")]))
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}create_snapshot_from_live_graph", new=AsyncMock(return_value=snapshot)))
    ctxs.append(patch(f"{_PREFIX}create_run", new=AsyncMock(return_value=run)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/test", json={"payload": {"k": "v"}})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(run.id)
    assert body["status"] == "test_event_created"


def test_test_trigger_snapshot_failure_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(trigger_type="manual")]))
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}create_snapshot_from_live_graph", new=AsyncMock(return_value=None)))
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/test", json={})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 500, resp.text
    assert "Failed to create pipeline snapshot" in resp.json()["detail"]


@pytest.mark.parametrize(("deleted", "expected"), [(True, 409), (False, 404)], ids=["deleted-org", "missing-org"])
def test_test_trigger_org_deleted_mapping(
    client: tuple[TestClient, AsyncMock],
    deleted: bool,
    expected: int,
) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([_make_trigger(trigger_type="manual")]))
    ctxs = list(_happy_patches())
    ctxs.append(patch(f"{_PREFIX}create_snapshot_from_live_graph", new=AsyncMock(return_value=MagicMock())))
    ctxs.append(
        patch(
            f"{_PREFIX}create_run",
            new=AsyncMock(side_effect=OrgDeletedError(org_id=_ORG_ID, deleted=deleted)),
        )
    )
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.post(f"/api/v1/triggers/{_TRIGGER_ID}/test", json={})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /triggers/{id}/events — cursor pagination + filters
# ---------------------------------------------------------------------------


def _event_row(seq: int) -> MagicMock:
    e = MagicMock()
    e.id = uuid.UUID(f"00000000-0000-0000-0000-{seq:012d}")
    e.trigger_id = _TRIGGER_ID
    e.validation_result = "valid"
    e.received_at = _NOW
    e.created_at = _NOW
    e.run_id = None
    e.error_detail = None
    return e


def test_list_trigger_events_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _session_assert_error_matrix(http, session, method="get", url=f"/api/v1/triggers/{_TRIGGER_ID}/events")


def test_list_trigger_events_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_trigger_result([]))
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(f"/api/v1/triggers/{_TRIGGER_ID}/events")
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 404, resp.text


def test_list_trigger_events_happy_path_builds_next_cursor(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    # limit=2 + 1 extra row => has_more page with a next_cursor.
    rows = [_event_row(i) for i in range(3)]
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    session.execute = AsyncMock(return_value=result)
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(f"/api/v1/triggers/{_TRIGGER_ID}/events", params={"limit": 2})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    assert body["next_cursor"].endswith("_" + str(rows[1].id))


def test_list_trigger_events_single_page_has_no_cursor(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    rows = [_event_row(0)]
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    session.execute = AsyncMock(return_value=result)
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(
            f"/api/v1/triggers/{_TRIGGER_ID}/events",
            params={"limit": 20, "status": "valid", "cursor": "2025-01-01T00:00:00+00:00_" + str(uuid.uuid4())},
        )
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["next_cursor"] is None


# ---------------------------------------------------------------------------
# GET /pipelines/{id}/triggers — pipeline-scoped list
# ---------------------------------------------------------------------------


def test_list_pipeline_triggers_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _session_assert_error_matrix(http, session, method="get", url=f"/api/v1/pipelines/{_PIPELINE_ID}/triggers")


def test_list_pipeline_triggers_happy_path_with_type_filter(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    rows = [_make_trigger(trigger_type="cron"), _make_trigger(trigger_type="cron", id=uuid.uuid4())]
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    session.execute = AsyncMock(return_value=result)
    ctxs = list(_happy_patches())
    for c in ctxs:
        c.__enter__()
    try:
        resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers", params={"trigger_type": "cron"})
    finally:
        for c in reversed(ctxs):
            c.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["created_at"] == _NOW.isoformat()
