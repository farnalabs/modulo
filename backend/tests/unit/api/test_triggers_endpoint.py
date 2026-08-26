"""Unit tests for /api/v1/triggers endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_mock_trigger(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TRIGGER_ID)
    t.pipeline_id = overrides.get("pipeline_id", _PIPELINE_ID)
    t.organisation_id = _ORG_ID
    t.trigger_type = overrides.get("trigger_type", "cron")
    t.active = overrides.get("active", True)
    t.max_concurrent_runs = overrides.get("max_concurrent_runs", 1)
    t.daily_spend_limit = overrides.get("daily_spend_limit")
    t.cron_expression = overrides.get("cron_expression", "0 * * * *")
    t.cron_timezone = overrides.get("cron_timezone", "UTC")
    t.last_fired_at = overrides.get("last_fired_at")
    t.next_fire_at = overrides.get("next_fire_at")
    t.created_by = _USER_ID
    t.created_at = _NOW
    t.config_json = overrides.get("config_json", {})
    return t


def _make_trigger_result(triggers: list[MagicMock]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=triggers)))
    r.scalar_one_or_none = MagicMock(return_value=triggers[0] if triggers else None)
    r.scalar_one = MagicMock(return_value=len(triggers))
    # The list route's org-state read (trigger pause) uses ``one_or_none`` —
    # default to "no org row" => not paused.
    r.one_or_none = MagicMock(return_value=None)
    return r


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_make_trigger_result([]))
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    def override_settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
        )

    app.dependency_overrides[get_settings] = override_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_triggers_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["trigger_type"] == "cron"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_includes_pause_state(client: TestClient) -> None:
    """List response carries top-level triggers_paused / paused_at from a fresh
    org column-level read. The read is (triggers_paused, triggers_paused_at,
    status) — the full predicate the create_run gate applies."""
    trigger = _make_mock_trigger()
    with patch("modulo.api.routes.triggers.set_rls_org"):
        session = _make_mock_session()
        trigger_result = _make_trigger_result([trigger])
        org_result = MagicMock()
        org_result.one_or_none.return_value = (True, _NOW, "active")
        # Execute order: require_permission authz read, count, rows, org-state.
        calls = iter([trigger_result, trigger_result, trigger_result, org_result])

        async def _execute(*args: object, **kwargs: object) -> MagicMock:
            return next(calls)

        session.execute = _execute

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["triggers_paused"] is True
    assert body["paused_at"] == _NOW.isoformat()
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_paused_via_non_active_org_status(client: TestClient) -> None:
    """A suspended/deleted org (status != 'active', triggers_paused column
    False) must surface as triggers_paused=True — the SAME predicate the gate
    uses (org_row_is_paused), so the banner and toggle match server truth."""
    trigger = _make_mock_trigger()
    with patch("modulo.api.routes.triggers.set_rls_org"):
        session = _make_mock_session()
        trigger_result = _make_trigger_result([trigger])
        org_result = MagicMock()
        org_result.one_or_none.return_value = (False, None, "suspended")
        calls = iter([trigger_result, trigger_result, trigger_result, org_result])

        async def _execute(*args: object, **kwargs: object) -> MagicMock:
            return next(calls)

        session.execute = _execute

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["triggers_paused"] is True
    assert body["paused_at"] is None
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_empty_returns_200(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_unauthenticated_returns_4xx(client: TestClient) -> None:
    client.app.dependency_overrides.pop(get_current_user, None)
    resp = client.get("/api/v1/triggers")
    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    assert resp.status_code in (401, 403)


def test_update_cron_config_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value=None),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=_NOW),
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/cron",
            json={"cron_expression": "0 */2 * * *", "active": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cron_expression"] == "0 */2 * * *"
    assert body["active"] is True
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_cron_config_non_cron_returns_400(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="manual")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/cron",
            json={"cron_expression": "0 * * * *"},
        )

    assert resp.status_code == 400
    assert "Only cron triggers" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_cron_config_invalid_cron_returns_422(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value="bad cron"),
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/cron",
            json={"cron_expression": "invalid"},
        )

    assert resp.status_code == 422
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_cron_config_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{uuid.uuid4()}/cron",
            json={"cron_expression": "0 * * * *"},
        )

    assert resp.status_code == 404
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_preview_cron_schedule_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{uuid.uuid4()}/cron/preview")

    assert resp.status_code == 404
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


# ---------------------------------------------------------------------------
# New endpoint tests
# ---------------------------------------------------------------------------


def test_create_trigger_returns_201(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value=None),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=_NOW),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "cron", "cron_expression": "0 * * * *"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["trigger_type"] == "cron"
    assert body["active"] is True
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_create_trigger_invalid_cron_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value="bad cron"),
    ):
        session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "cron", "cron_expression": "invalid"},
        )

    assert resp.status_code == 422
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_delete_trigger_returns_204(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.db.crud.trigger.soft_delete_trigger", return_value=trigger),
    ):
        resp = client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")

    assert resp.status_code == 204


def test_delete_trigger_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.db.crud.trigger.soft_delete_trigger", return_value=None),
    ):
        resp = client.delete(f"/api/v1/triggers/{uuid.uuid4()}")

    assert resp.status_code == 404


def test_restore_trigger_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.db.crud.trigger.restore_trigger", return_value=trigger),
    ):
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/restore")
    assert resp.status_code == 200
    assert resp.json()["trigger_type"] == "cron"


def test_restore_trigger_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.db.crud.trigger.restore_trigger", return_value=None),
    ):
        resp = client.post(f"/api/v1/triggers/{uuid.uuid4()}/restore")
    assert resp.status_code == 404


def test_toggle_trigger_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(active=True)
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle", json={})

    assert resp.status_code == 200
    assert resp.json()["active"] is False
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_trigger_events_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = _TRIGGER_ID
    event.validation_result = "test"
    event.received_at = _NOW
    event.created_at = _NOW
    event.run_id = None
    event.error_detail = None

    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        # require_permission kill-switch read consumes the first execute
        authz_result = MagicMock()
        authz_result.scalar_one_or_none = MagicMock(return_value=None)
        # First call loads trigger, second call loads events
        trigger_result = _make_trigger_result([trigger])
        event_result = MagicMock()
        event_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[event])))
        session.execute = AsyncMock(side_effect=[authz_result, trigger_result, event_result])

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/events")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == "test"
    assert body["next_cursor"] is None
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_trigger_events_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{uuid.uuid4()}/events")

    assert resp.status_code == 404
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="cron")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value=None),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=_NOW),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"max_concurrent_runs": 5, "active": False},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_preserves_secret_when_masked_config_round_tripped(client: TestClient) -> None:
    """A read-modify-write round trip through the masking API must NOT persist
    the literal SENSITIVE_VALUE_MASK as the stored secret."""
    trigger = _make_mock_trigger(trigger_type="webhook", config_json={"hmac_secret": "real-secret"})
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"config_json": {"hmac_secret": SENSITIVE_VALUE_MASK}},
        )

    assert resp.status_code == 200
    assert trigger.config_json["hmac_secret"] == "real-secret"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_updates_secret_with_new_value(client: TestClient) -> None:
    """A genuinely new secret value must still be written through."""
    trigger = _make_mock_trigger(trigger_type="webhook", config_json={"hmac_secret": "old-secret"})
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"config_json": {"hmac_secret": "new-secret"}},
        )

    assert resp.status_code == 200
    assert trigger.config_json["hmac_secret"] == "new-secret"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_null_secret_clears_key(client: TestClient) -> None:
    """An explicit null for a sensitive key clears it from the stored config."""
    trigger = _make_mock_trigger(trigger_type="webhook", config_json={"hmac_secret": "real-secret"})
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"config_json": {"hmac_secret": None}},
        )

    assert resp.status_code == 200
    assert "hmac_secret" not in trigger.config_json
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_merges_masked_secret_but_updates_other_keys(client: TestClient) -> None:
    """The exact production scenario: PUTting back a masked config with a
    non-secret change must preserve the stored secret while applying the other
    key's update."""
    trigger = _make_mock_trigger(
        trigger_type="webhook",
        config_json={"hmac_secret": "real-secret", "work_item_ref_paths": [".agents"]},
    )
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"config_json": {"hmac_secret": SENSITIVE_VALUE_MASK, "work_item_ref_paths": ["backend", "frontend"]}},
        )

    assert resp.status_code == 200
    assert trigger.config_json["hmac_secret"] == "real-secret"
    assert trigger.config_json["work_item_ref_paths"] == ["backend", "frontend"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_test_trigger_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="manual")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.create_snapshot_from_live_graph", new_callable=AsyncMock),
        patch("modulo.api.routes.triggers.create_run", new_callable=AsyncMock),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/test",
            json={"payload": {"test": True}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "test_event_created"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_test_trigger_storage_exhausted_returns_503(client: TestClient) -> None:
    """Real test-trigger path raises StorageExhaustedError -> 503 (FAR-426).

    ``test_trigger`` is wrapped by ``@handle_db_errors``, whose broad
    ``except Exception`` would otherwise swallow the re-raised
    StorageExhaustedError before Starlette's handler could map it. The
    decorator now re-raises it, so the 503 ``storage_exhausted`` contract fires
    on the real new-run path (not just the runs endpoint).
    """
    trigger = _make_mock_trigger(trigger_type="manual")
    snapshot = MagicMock()
    snapshot.id = uuid.uuid4()

    capacity_settings = SimpleNamespace(
        db_capacity_mode="fixed",
        db_capacity_bypass=False,
        db_capacity_hard_stop_pct=98.0,
    )
    over_hard_stop = {
        "capacity_percent": 99.0,
        "mode": "fixed",
        "alert_level": "full",
        "used_bytes": 99_000_000,
        "capacity_bytes": 100_000_000,
    }

    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch(
            "modulo.api.routes.triggers.create_snapshot_from_live_graph",
            new=AsyncMock(return_value=snapshot),
        ),
        patch("modulo.db.crud.run._ensure_org_not_deleted", new=AsyncMock()),
        patch("modulo.db.capacity.get_settings", return_value=capacity_settings),
        patch(
            "modulo.db.capacity.db_capacity_status",
            new=AsyncMock(return_value=over_hard_stop),
        ),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/test",
            json={"payload": {"test": True}},
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 503
    assert resp.json().get("type") == "urn:problem:modulo:storage_exhausted"


def test_list_pipeline_triggers_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["trigger_type"] == "cron"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_cron_config_sets_input_template(client: TestClient) -> None:
    trigger = _make_mock_trigger()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value=None),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=_NOW),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/cron",
            json={"input_template": {"topic": "security", "severity": "high"}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["input_template"] == {"topic": "security", "severity": "high"}
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_preview_cron_schedule_returns_fire_times(client: TestClient) -> None:
    trigger = _make_mock_trigger(cron_expression="0 * * * *")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview?count=5")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["next_fire_times"]) == 5
    assert body["cron_expression"] == "0 * * * *"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_preview_cron_schedule_no_expression_returns_400(client: TestClient) -> None:
    trigger = _make_mock_trigger(cron_expression=None)
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview")

    assert resp.status_code == 400
    assert "no cron expression" in resp.json()["detail"].lower()
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


# ---------------------------------------------------------------------------
# daily_spend_limit exposure
# ---------------------------------------------------------------------------


def test_create_trigger_with_daily_spend_limit_returns_201(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.Trigger") as mock_trigger_cls,
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([]))
        mock_trigger = mock_trigger_cls.return_value
        mock_trigger.id = _TRIGGER_ID
        mock_trigger.pipeline_id = _PIPELINE_ID
        mock_trigger.trigger_type = "polling"
        mock_trigger.active = True
        mock_trigger.max_concurrent_runs = 1
        mock_trigger.daily_spend_limit = Decimal("25.5")
        mock_trigger.config_json = {}
        mock_trigger.cron_expression = None
        mock_trigger.cron_timezone = None
        mock_trigger.last_fired_at = None
        mock_trigger.next_fire_at = None

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "polling", "daily_spend_limit": 25.5},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["daily_spend_limit"] == 25.5
    _, kwargs = mock_trigger_cls.call_args
    assert kwargs["daily_spend_limit"] == Decimal("25.5")
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_create_trigger_negative_daily_spend_limit_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "polling", "daily_spend_limit": -5},
        )

    assert resp.status_code == 422
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_daily_spend_limit_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="polling")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"daily_spend_limit": 50},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["daily_spend_limit"] == 50.0
    assert trigger.daily_spend_limit == Decimal(50)
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_trigger_clears_daily_spend_limit_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="polling", daily_spend_limit=Decimal(50))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"daily_spend_limit": None},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["daily_spend_limit"] is None
    assert trigger.daily_spend_limit is None
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_polling_config_daily_spend_limit_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="polling")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling",
            json={"daily_spend_limit": 10},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["daily_spend_limit"] == 10.0
    assert trigger.daily_spend_limit == Decimal(10)
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_polling_config_clears_daily_spend_limit_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="polling", daily_spend_limit=Decimal(10))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling",
            json={"daily_spend_limit": None},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["daily_spend_limit"] is None
    assert trigger.daily_spend_limit is None
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_polling_config_interval_below_60_returns_422(client: TestClient) -> None:
    """FAR-169: poll_interval_seconds < 60 must be rejected (the scheduler ticks
    every 60s, so sub-60 intervals are misleading — the effective cadence is
    always >= 60s)."""
    trigger = _make_mock_trigger(trigger_type="polling")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling",
            json={"poll_interval_seconds": 30},
        )

    assert resp.status_code == 422
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_polling_config_interval_59_returns_422(client: TestClient) -> None:
    """FAR-169: the floor is exactly 60 — 59 is still rejected."""
    trigger = _make_mock_trigger(trigger_type="polling")
    session = _make_mock_session()
    session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    client.app.dependency_overrides[get_db_session] = override_session
    resp = client.patch(
        f"/api/v1/triggers/{_TRIGGER_ID}/polling",
        json={"poll_interval_seconds": 59},
    )

    assert resp.status_code == 422
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_polling_config_interval_60_returns_200(client: TestClient) -> None:
    """FAR-169: the 60s floor is the inclusive lower bound — 60 is accepted."""
    trigger = _make_mock_trigger(trigger_type="polling")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling",
            json={"poll_interval_seconds": 60},
        )

    assert resp.status_code == 200
    assert trigger.config_json["poll_interval_seconds"] == 60
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_returns_daily_spend_limit(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="polling", daily_spend_limit=Decimal("15.25"))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["daily_spend_limit"] == 15.25
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_pipeline_triggers_returns_daily_spend_limit(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="polling", daily_spend_limit=Decimal("7.5"))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["daily_spend_limit"] == 7.5
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


# ---------------------------------------------------------------------------
# ongoing trigger type (FAR-158)
# ---------------------------------------------------------------------------


def _pipeline_cap(cap: int):
    from types import SimpleNamespace

    return SimpleNamespace(max_concurrent_runs=cap, is_break_glass=False)


def _ongoing_create_payload(**overrides):
    payload = {
        "trigger_type": "ongoing",
        "active": True,
        "max_concurrent_runs": 3,
        "daily_spend_limit": 25.0,
        "config_json": {"scan_interval_seconds": 120, "input_template": {"topic": "x"}},
    }
    payload.update(overrides)
    return payload


def test_create_ongoing_without_spend_limit_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=_pipeline_cap(10))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json=_ongoing_create_payload(daily_spend_limit=None),
        )

    assert resp.status_code == 422
    assert "daily_spend_limit" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_create_ongoing_target_above_20_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json=_ongoing_create_payload(max_concurrent_runs=21),
        )

    assert resp.status_code == 422
    assert "between 1 and 20" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_create_ongoing_target_above_pipeline_cap_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=_pipeline_cap(2))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json=_ongoing_create_payload(max_concurrent_runs=5),
        )

    assert resp.status_code == 422
    assert "cannot exceed" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_create_ongoing_scan_interval_below_60_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=_pipeline_cap(10))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json=_ongoing_create_payload(config_json={"scan_interval_seconds": 30}),
        )

    assert resp.status_code == 422
    assert "at least 60" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_create_ongoing_valid_returns_201_with_next_fire_at(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=_pipeline_cap(10))
        session.execute = AsyncMock(return_value=_make_trigger_result([]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json=_ongoing_create_payload(),
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["trigger_type"] == "ongoing"
    assert body["next_fire_at"] is not None, "a fresh ongoing trigger must fire on the first tick"
    assert body["max_concurrent_runs"] == 3
    assert body["daily_spend_limit"] == 25.0
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_ongoing_cannot_clear_spend_limit_returns_422(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal(25))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"daily_spend_limit": None},
        )

    assert resp.status_code == 422
    assert "clearing it is not allowed" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_ongoing_target_above_pipeline_cap_returns_422(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="ongoing", max_concurrent_runs=3, daily_spend_limit=Decimal(25))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))
        session.get = AsyncMock(return_value=_pipeline_cap(5))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"max_concurrent_runs": 20},
        )

    assert resp.status_code == 422
    assert "cannot exceed" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_update_ongoing_merges_config_json(client: TestClient) -> None:
    trigger = _make_mock_trigger(
        trigger_type="ongoing",
        max_concurrent_runs=3,
        daily_spend_limit=Decimal(25),
        config_json={"scan_interval_seconds": 300, "input_template": {"a": 1}},
    )
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))
        session.get = AsyncMock(return_value=_pipeline_cap(10))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"config_json": {"input_template": {"b": 2}}},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    merged = body["config_json"]
    assert merged["scan_interval_seconds"] == 300, "PUT must merge config, never wipe the cadence"
    assert merged["input_template"] == {"b": 2}
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_patch_ongoing_updates_scan_interval_and_recomputes_next_fire_at(client: TestClient) -> None:
    trigger = _make_mock_trigger(
        trigger_type="ongoing",
        max_concurrent_runs=3,
        daily_spend_limit=Decimal(25),
        config_json={"scan_interval_seconds": 60},
        next_fire_at=_NOW,
    )
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/ongoing",
            json={"scan_interval_seconds": 300},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert trigger.config_json["scan_interval_seconds"] == 300
    assert body["next_fire_at"] is not None
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_patch_ongoing_invalid_interval_returns_422(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal(25))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/ongoing",
            json={"scan_interval_seconds": 30},
        )

    assert resp.status_code == 422
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_patch_ongoing_on_non_ongoing_returns_400(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="cron")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(
            f"/api/v1/triggers/{_TRIGGER_ID}/ongoing",
            json={"scan_interval_seconds": 300},
        )

    assert resp.status_code == 400
    assert "Only ongoing triggers" in resp.json()["detail"]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_toggle_on_ongoing_resets_next_fire_at(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="ongoing", active=False, daily_spend_limit=Decimal(25))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle", json={})

    assert resp.status_code == 200
    assert resp.json()["active"] is True
    assert trigger.next_fire_at is not None, "an ongoing trigger turned back ON must fire on the next tick"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_includes_in_flight_for_ongoing(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal(25))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["in_flight"] == 1
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


# ---------------------------------------------------------------------------
# FAR-191 — streak_status surfacing + operator re-enable
# ---------------------------------------------------------------------------


def _full_streak_status(**overrides: object) -> dict[str, object]:
    status: dict[str, object] = {
        "enabled": True,
        "streak": 4,
        "threshold": 5,
        "state": "ok",
        "deactivated_reason": None,
        "last_outcomes": [
            {
                "run_id": "run-1",
                "classification": "no_delivery",
                "reason": "no_work",
                "completed_at": "2026-08-01T00:00:00Z",
            }
        ],
    }
    status.update(overrides)
    return status


def test_list_triggers_includes_full_streak_status_for_ongoing(client: TestClient) -> None:
    """The trigger list carries the full streak_status for an ongoing trigger:
    current streak / threshold / state / deactivation reason / last-N outcomes."""
    trigger = _make_mock_trigger(trigger_type="ongoing", active=False, daily_spend_limit=Decimal(25))
    streak_status = _full_streak_status(streak=5, state="deactivated", deactivated_reason="no_delivery_streak")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ) as get_status,
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["streak_status"] == streak_status
    get_status.assert_awaited_once()
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_triggers_streak_status_uniform_shape_for_non_ongoing(client: TestClient) -> None:
    """FIX 5 — non-ongoing triggers get the SAME uniform 6-key streak_status
    shape as ongoing ones: ``get_trigger_streak_status`` is always called and
    its base (``{enabled: false, streak: 0, threshold: 0, state:
    'unconfigured', deactivated_reason: null, last_outcomes: []}``) is returned
    with NO streak-engine query (the reader short-circuits before querying)."""
    trigger = _make_mock_trigger(trigger_type="cron")
    base_status = {
        "enabled": False,
        "streak": 0,
        "threshold": 0,
        "state": "unconfigured",
        "deactivated_reason": None,
        "last_outcomes": [],
    }
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=base_status,
        ) as get_status,
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["streak_status"] == base_status
    get_status.assert_awaited_once()
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


class _TrackingBegin:
    """A real async context manager that tracks whether ``begin()`` is active —
    used to prove the list path runs its streak reads INSIDE the transaction."""

    active = False

    async def __aenter__(self) -> Self:
        _TrackingBegin.active = True
        return self

    async def __aexit__(self, *args: object) -> bool:
        _TrackingBegin.active = False
        return False


def test_list_triggers_streak_reads_run_inside_rls_transaction(client: TestClient) -> None:
    """FIX 2 — the list path must compute streak_status INSIDE the RLS
    transaction. ``SET LOCAL app.organisation_id`` is transaction-scoped; on
    strict-RLS Postgres a streak read AFTER commit sees zero rows and a
    deactivated trigger silently reports state 'ok'. The streak read must be
    observed while ``begin()`` is active."""
    trigger = _make_mock_trigger(trigger_type="ongoing", active=False, daily_spend_limit=Decimal(25))
    streak_status = _full_streak_status(streak=5, state="deactivated", deactivated_reason="no_delivery_streak")

    async def _streak_status(*args: object, **kwargs: object) -> dict[str, object]:
        assert _TrackingBegin.active, "streak read must happen inside the RLS transaction"
        return streak_status

    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            side_effect=_streak_status,
        ),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))
        session.begin = MagicMock(return_value=_TrackingBegin())

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["streak_status"] == streak_status
    assert _TrackingBegin.active is False, "the transaction must have closed after the response"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_pipeline_triggers_includes_streak_status(client: TestClient) -> None:
    """The pipeline-scoped trigger list surfaces streak_status too."""
    trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal(25))
    streak_status = _full_streak_status()
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["streak_status"] == streak_status
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_list_pipeline_triggers_streak_reads_run_inside_rls_transaction(client: TestClient) -> None:
    """FIX 2 (round 2) — the pipeline-scoped trigger list must compute
    streak_status + in_flight INSIDE the RLS transaction, exactly like the
    fixed ``/triggers`` list. ``SET LOCAL app.organisation_id`` is
    transaction-scoped; on strict-RLS Postgres a streak read AFTER commit sees
    zero rows and a deactivated trigger silently reports state 'ok' with no
    deactivated badge / Re-enable button. The read must be observed while
    ``begin()`` is active."""
    trigger = _make_mock_trigger(trigger_type="ongoing", active=False, daily_spend_limit=Decimal(25))
    streak_status = _full_streak_status(streak=5, state="deactivated", deactivated_reason="no_delivery_streak")

    async def _streak_status(*args: object, **kwargs: object) -> dict[str, object]:
        assert _TrackingBegin.active, "streak read must happen inside the RLS transaction"
        return streak_status

    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            side_effect=_streak_status,
        ),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))
        session.begin = MagicMock(return_value=_TrackingBegin())

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers")

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["streak_status"] == streak_status
    assert item["active"] is False, "a deactivated trigger must keep its active=False in the response"
    assert _TrackingBegin.active is False, "the transaction must have closed after the response"
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_toggle_deactivated_trigger_viewer_returns_403(client: TestClient) -> None:
    """Re-enabling a deactivated trigger is an operator action: a viewer (or any
    non-operator) is denied 403 at the permission gate — re-enable must never be
    reachable by a runner/viewer (FAR-191 spec item 3)."""
    trigger = _make_mock_trigger(trigger_type="ongoing", active=False, daily_spend_limit=Decimal(25))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="viewer", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="viewer"
        )
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle", json={})

    assert resp.status_code == 403
    assert "operator" in resp.json()["detail"]
    client.app.dependency_overrides[get_current_user] = app.dependency_overrides[get_current_user]
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_toggle_deactivated_trigger_operator_reenables(client: TestClient) -> None:
    """Re-enabling a deactivated ongoing trigger (operator) succeeds, re-anchors
    the streak epoch, clears the FAR-158 config-failure Redis counter, and the
    response surfaces the refreshed (reset) streak_status."""
    trigger = _make_mock_trigger(trigger_type="ongoing", active=False, daily_spend_limit=Decimal(25))
    streak_status = _full_streak_status(streak=0, state="ok", deactivated_reason=None, last_outcomes=[])
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.anchor_trigger_streak_epoch", new_callable=AsyncMock) as anchor,
        patch("modulo.api.routes.triggers.clear_trigger_streak_after_reenable", new_callable=AsyncMock) as clear,
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["streak_status"] == streak_status
    anchor.assert_awaited_once()
    clear.assert_awaited_once()
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_restore_trigger_returns_streak_status(client: TestClient) -> None:
    """The restore (re-enable) response surfaces streak_status for ongoing
    triggers — the operator sees the reset streak immediately."""
    trigger = _make_mock_trigger(trigger_type="ongoing", active=True, daily_spend_limit=Decimal(25))
    streak_status = _full_streak_status(streak=0, state="ok")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.db.crud.trigger.restore_trigger", return_value=trigger),
        patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ),
    ):
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/restore")

    assert resp.status_code == 200
    assert resp.json()["streak_status"] == streak_status


def test_trigger_type_regex_accepts_ongoing_and_agent_signal() -> None:
    from pydantic import ValidationError

    from modulo.api.routes.triggers import TriggerCreate

    assert TriggerCreate(trigger_type="ongoing").trigger_type == "ongoing"
    assert TriggerCreate(trigger_type="agent_signal").trigger_type == "agent_signal"
    with pytest.raises(ValidationError):
        TriggerCreate(trigger_type="not_a_trigger")
