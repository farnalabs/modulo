"""Unit tests: trigger route handlers return 503 on SQLAlchemyError."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_mock_session(exc_side_effect: type[Exception] | None = SQLAlchemyError("mock", "mock", "mock")) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    if exc_side_effect is not None:
        session.execute = AsyncMock(side_effect=exc_side_effect)
    else:
        session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def engine_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# === triggers.py handler tests (each route returns 503 on SQLAlchemyError) ===


def test_list_triggers_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/triggers")
    assert resp.status_code == 503


def test_update_cron_config_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(f"/api/v1/triggers/{_TRIGGER_ID}/cron", json={"cron_expression": "0 * * * *"})
    assert resp.status_code == 503


def test_preview_cron_schedule_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/cron/preview")
    assert resp.status_code == 503


def test_update_polling_config_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.TriggerEngine.schedule_polling_trigger"),
    ):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.patch(f"/api/v1/triggers/{_TRIGGER_ID}/polling", json={"poll_interval_seconds": 60})
    assert resp.status_code == 503


def test_test_polling_condition_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.TriggerEngine.evaluate_condition", new_callable=AsyncMock),
    ):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/polling/test",
            json={"connector_instance_id": str(uuid.uuid4()), "poll_query": "SELECT 1"},
        )
    assert resp.status_code == 503


def test_create_trigger_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(None)
    session.flush = AsyncMock(side_effect=SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers", json={"trigger_type": "manual"})
    assert resp.status_code == 503


def test_update_trigger_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(f"/api/v1/triggers/{_TRIGGER_ID}", json={"active": False})
    assert resp.status_code == 503


def test_delete_trigger_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")
    assert resp.status_code == 503


def test_toggle_trigger_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/toggle")
    assert resp.status_code == 503


def test_test_trigger_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(f"/api/v1/triggers/{_TRIGGER_ID}/test", json={"payload": {}})
    assert resp.status_code == 503


def test_list_trigger_events_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/triggers/{_TRIGGER_ID}/events")
    assert resp.status_code == 503


def test_list_pipeline_triggers_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/triggers")
    assert resp.status_code == 503


# === admin_triggers.py tests ===


def test_admin_list_trigger_events_sqlalchemy_error(client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.admin_triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/admin/trigger-events")
    assert resp.status_code == 503


def test_admin_list_trigger_events_count_sqlalchemy_error(client: TestClient) -> None:
    """SQLAlchemyError on the count query outside the main session.begin() block."""
    session = _make_mock_session(None)
    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    session.execute = AsyncMock(return_value=execute_result)
    session.execute.side_effect = [execute_result, SQLAlchemyError("mock", "mock", "mock")]
    with patch("modulo.api.routes.admin_triggers.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/admin/trigger-events")
    assert resp.status_code == 503


# === webhooks.py tests ===


def test_receive_webhook_sqlalchemy_error(engine_client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with (
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook"),
    ):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        engine_client.app.dependency_overrides[get_db_session] = override_session
        resp = engine_client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"test": True},
            headers={"X-Modulo-Timestamp": "1000000000", "X-Modulo-Webhook-Secret": "test"},
        )
    assert resp.status_code == 503


def test_replay_webhook_sqlalchemy_error(engine_client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with (
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event"),
    ):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        engine_client.app.dependency_overrides[get_db_session] = override_session
        resp = engine_client.post(f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{uuid.uuid4()}")
    assert resp.status_code == 503


def test_cleanup_expired_sqlalchemy_error(engine_client: TestClient) -> None:
    session = _make_mock_session(SQLAlchemyError("mock", "mock", "mock"))
    with patch("modulo.api.routes.webhooks.set_rls_org"):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        engine_client.app.dependency_overrides[get_db_session] = override_session
        resp = engine_client.post("/api/v1/triggers/cleanup-expired")
    assert resp.status_code == 503
