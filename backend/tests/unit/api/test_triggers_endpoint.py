"""Unit tests for /api/v1/triggers endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

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
    t.cron_expression = overrides.get("cron_expression", "0 * * * *")
    t.cron_timezone = overrides.get("cron_timezone", "UTC")
    t.last_fired_at = overrides.get("last_fired_at", None)
    t.next_fire_at = overrides.get("next_fire_at", None)
    t.created_by = _USER_ID
    t.created_at = _NOW
    t.config_json = overrides.get("config_json", {})
    return t


def _make_trigger_result(triggers: list[MagicMock]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=triggers)))
    r.scalar_one_or_none = MagicMock(return_value=triggers[0] if triggers else None)
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_make_trigger_result([]))
    return session


@pytest.fixture()
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
        username="testuser", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
    )
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
        username="testuser", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
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
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")

    assert resp.status_code == 204
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


def test_delete_trigger_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/triggers/{uuid.uuid4()}")

    assert resp.status_code == 404
    client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]


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
        # First call loads trigger, second call loads events
        trigger_result = _make_trigger_result([trigger])
        event_result = MagicMock()
        event_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[event])))
        session.execute = AsyncMock(side_effect=[trigger_result, event_result])

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


def test_test_trigger_returns_200(client: TestClient) -> None:
    trigger = _make_mock_trigger(trigger_type="manual")
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", new_callable=AsyncMock),
        patch("modulo.db.crud.run.create_run", new_callable=AsyncMock),
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
