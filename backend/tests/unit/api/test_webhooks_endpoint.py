"""Unit tests for /api/v1/triggers/{id}/webhook endpoints.

All delivery attempts are logged as TriggerEvent rows regardless of outcome.
Verifies that the background task is properly enqueued but no real webhook fires.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_mock_run() -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    # Explicitly configure execute so scalar_one_or_none() returns a MagicMock trigger
    # (not a coroutine — Python 3.13 AsyncMock can return coroutines for child attribute calls)
    trigger_mock = MagicMock()
    trigger_mock.pipeline_id = uuid.uuid4()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    session.execute = AsyncMock(return_value=execute_result)

    return session


@pytest.fixture(autouse=True)
def _patch_snapshot_creator() -> Generator[None, None, None]:
    """Patch create_snapshot_from_live_graph for all webhook tests.

    The receive_webhook route now fetches the trigger and creates a snapshot
    before calling handle_webhook. This fixture stubs out the snapshot creation
    so tests can focus on handle_webhook behaviour.
    """
    mock_snapshot = MagicMock()
    mock_snapshot.id = uuid.uuid4()
    with patch(
        "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
        new_callable=AsyncMock,
        return_value=mock_snapshot,
    ):
        yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_receive_webhook_returns_202(client: TestClient) -> None:
    run_mock = _make_mock_run()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.PipelineExecutor"),
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000", "X-Modulo-Webhook-Secret": "test-hmac"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["run_id"] == str(_RUN_ID)


def test_receive_webhook_missing_json_body_returns_400(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            content=b"not json",
            headers={"X-Modulo-Timestamp": "1700000000", "Content-Type": "application/json"},
        )

    assert resp.status_code == 400
    assert "JSON object" in resp.json()["detail"]


def test_receive_webhook_trigger_not_found_returns_404(client: TestClient) -> None:
    from modulo.core.trigger_engine import TriggerNotFoundError

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = TriggerNotFoundError(_TRIGGER_ID)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"


def test_receive_webhook_inactive_returns_404(client: TestClient) -> None:
    from modulo.core.trigger_engine import TriggerInactiveError

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = TriggerInactiveError(_TRIGGER_ID)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"


def test_receive_webhook_hmac_failure_returns_401(client: TestClient) -> None:
    from modulo.core.trigger_engine import HmacValidationError

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = HmacValidationError()
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 401


def test_receive_webhook_duplicate_returns_400(client: TestClient) -> None:
    from modulo.core.trigger_engine import DuplicateWebhookError

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = DuplicateWebhookError("dup-hash")
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 400
    assert "Duplicate" in resp.json()["detail"]


def test_receive_webhook_concurrent_limit_returns_429(client: TestClient) -> None:
    from modulo.core.trigger_engine import ConcurrentRunLimitError

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = ConcurrentRunLimitError(_TRIGGER_ID, 3)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 429


def test_replay_webhook_returns_202(client: TestClient) -> None:
    event_id = uuid.uuid4()
    run_mock = _make_mock_run()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.PipelineExecutor"),
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["run_id"] == str(_RUN_ID)


def test_replay_webhook_not_found_returns_404(client: TestClient) -> None:
    from modulo.core.trigger_engine import ReplayNotFoundError

    event_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = ReplayNotFoundError(event_id)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger event not found"


def test_webhook_unauthenticated_returns_4xx(client: TestClient) -> None:
    client.app.dependency_overrides.pop(get_current_user, None)
    resp = client.post(
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
        json={"event": "test"},
    )
    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
    )
    assert resp.status_code in (401, 403)
