"""Unit tests for /api/v1/triggers/{id}/webhook endpoints.

All delivery attempts are logged as TriggerEvent rows regardless of outcome.
Verifies that the background task is properly enqueued but no real webhook fires.
"""

import time
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, create_access_token
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


def _auth_headers(role: str = "admin") -> dict[str, str]:
    settings = _make_settings()
    token = create_access_token(
        "testuser",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role=role,
    )
    return {"Authorization": f"Bearer {token}"}


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
    trigger_mock.active = True
    # config_json must be {} (not a truthy MagicMock) so the route-level HMAC
    # validation is skipped for the happy path — otherwise the route would run
    # HMAC against a MagicMock hmac_secret and 401 every test.
    trigger_mock.config_json = {}
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    session.execute = AsyncMock(return_value=execute_result)
    session.add = MagicMock()

    return session


def _make_hmac_session() -> AsyncMock:
    """Session whose trigger carries a real hmac_secret (route-level HMAC runs)."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    trigger_mock = MagicMock()
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.active = True
    trigger_mock.config_json = {"hmac_secret": "test-hmac-secret"}
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    session.execute = AsyncMock(return_value=execute_result)
    session.add = MagicMock()

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


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
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
    with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False):
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_receive_webhook_returns_202(client: TestClient) -> None:
    run_mock = _make_mock_run()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch(
            "modulo.api.routes.webhooks.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
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


def test_receive_webhook_records_error_event_on_dispatch_failure(client: TestClient) -> None:
    """A fail-fast enqueue failure is surfaced as an error_event (source='saq'),
    WITHOUT blocking the 202 response (plan F3d/F1 webhook dispatch bounds)."""
    run_mock = _make_mock_run()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch(
            "modulo.api.routes.webhooks.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueue_failed", None),
        ),
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks._ingest_webhook_dispatch_error", new_callable=AsyncMock) as ingest,
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000", "X-Modulo-Webhook-Secret": "test-hmac"},
        )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    ingest.assert_awaited_once()


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
    """Route-level HMAC validation: a bad signature against a trigger that
    configures an hmac_secret must 401 before the engine is reached."""
    session = _make_hmac_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": str(int(time.time())), "X-Modulo-Webhook-Secret": "sha256=bad"},
            )
        m.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db_session, None)

    assert resp.status_code == 401


def test_receive_webhook_paused_org_returns_202_paused(client: TestClient) -> None:
    """Paused org: 202 {"status": "paused"} with NO run_id, engine never called."""
    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": str(int(time.time()))},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body == {"status": "paused"}
    assert "run_id" not in body
    m.assert_not_called()
    dispatch.assert_not_called()


def test_replay_webhook_paused_org_returns_202_paused(client: TestClient) -> None:
    """Paused org replay: 202 {"status": "paused"}, no run_id, engine never called."""
    event_id = uuid.uuid4()
    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_auth_headers("admin"),
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body == {"status": "paused"}
    assert "run_id" not in body
    m.assert_not_called()
    dispatch.assert_not_called()


def test_receive_webhook_paused_org_missing_row_returns_202_without_event(client: TestClient) -> None:
    """Orphan trigger whose org row was HARD-deleted: a paused delivery returns
    202 {"status": "paused"} but does NOT attempt the TriggerEvent INSERT (the
    organisations FK would fail -> 503). Fail-closed, no crash."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()

    trigger_mock = MagicMock()
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.active = True
    trigger_mock.config_json = {}
    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger_mock
    org_missing = MagicMock()
    org_missing.scalar_one_or_none.return_value = None

    async def _execute(*args: object, **kwargs: object) -> MagicMock:
        sql = str(args[0]).lower()
        if "organisations" in sql:
            return org_missing
        return trigger_result

    session.execute = _execute

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        with (
            patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
            patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": str(int(time.time()))},
            )
    finally:
        app.dependency_overrides.pop(get_db_session, None)

    assert resp.status_code == 202
    assert resp.json() == {"status": "paused"}
    session.add.assert_not_called()
    m.assert_not_called()


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
        patch(
            "modulo.api.routes.webhooks.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_auth_headers("admin"),
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
            headers=_auth_headers("admin"),
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger event not found"


def test_webhook_unauthenticated_returns_4xx(client: TestClient) -> None:
    """An unauthenticated webhook to a trigger configured with an hmac_secret is
    rejected at the route level (401). HMAC-less triggers remain public by design."""
    session = _make_hmac_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        with patch("modulo.api.routes.webhooks.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": str(int(time.time()))},
            )
    finally:
        app.dependency_overrides.pop(get_db_session, None)
    assert resp.status_code in (401, 403)


# ── cleanup-expired (ADR 017: swept @ runner via trigger.cleanup) ─────────────


def _cleanup_client(role: str) -> TestClient:
    from modulo.auth.dependencies import get_current_tenant_user
    from modulo.auth.jwt import TenantPrincipal

    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role=role
    )
    return TestClient(app)


def test_cleanup_expired_runner_returns_200(client: TestClient) -> None:
    from modulo.auth.dependencies import get_current_tenant_user
    from modulo.auth.jwt import TenantPrincipal

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.cleanup_expired_dedup_hashes", new_callable=AsyncMock) as d,
        patch("modulo.api.routes.webhooks._trigger_engine.cleanup_expired_payloads", new_callable=AsyncMock) as p,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        d.return_value = 3
        p.return_value = 2
        app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="runner"
        )
        resp = client.post("/api/v1/triggers/cleanup-expired")
        app.dependency_overrides.pop(get_current_tenant_user, None)

    assert resp.status_code == 200
    assert resp.json() == {"dedup_hashes_deleted": 3, "payloads_deleted": 2}


def test_cleanup_expired_viewer_denied(client: TestClient) -> None:
    from modulo.auth.dependencies import get_current_tenant_user
    from modulo.auth.jwt import TenantPrincipal

    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="viewer", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="viewer"
    )
    try:
        resp = client.post("/api/v1/triggers/cleanup-expired")
    finally:
        app.dependency_overrides.pop(get_current_tenant_user, None)

    assert resp.status_code == 403
    assert "trigger.cleanup" in resp.json()["detail"]


def test_cleanup_expired_unauthenticated_returns_401(client: TestClient) -> None:
    from modulo.auth.dependencies import get_current_tenant_user, get_current_user

    client.app.dependency_overrides.pop(get_current_user, None)
    client.app.dependency_overrides.pop(get_current_tenant_user, None)
    try:
        resp = client.post("/api/v1/triggers/cleanup-expired")
    finally:
        client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )

    assert resp.status_code in (401, 403)
