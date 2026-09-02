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

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_system_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, create_access_token
from modulo.core.trigger_engine import TriggerNotFoundError
from modulo.settings import Settings, get_settings
from tests.unit.api.conftest import make_system_session_mock

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
    mock_system_session = make_system_session_mock(trigger_org_id=_ORG_ID)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_system_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
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
    configures an hmac_secret must 401 before the engine is reached. The
    secret lives on the trigger row, which the route loads via the SYSTEM
    session bootstrap (FAR-523), so the override targets the system session."""
    session = _make_hmac_session()
    system_session = make_system_session_mock(trigger_config={"hmac_secret": "test-hmac-secret"})

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
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
        app.dependency_overrides.pop(get_system_db_session, None)

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


def test_receive_webhook_guardrail_blocked_run_acks_422(client: TestClient) -> None:
    """FAR-213 webhook ack-after-validate: a run created guardrail-blocked
    (terminal eval_failed / error_code eval_blocked) is acked with a non-success
    422, NEVER a false 'accepted' — and no background dispatch fires."""
    run_mock = _make_mock_run()
    run_mock.error_code = "eval_blocked"
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000", "X-Modulo-Webhook-Secret": "test-hmac"},
        )

    assert resp.status_code == 422
    assert "guardrail" in resp.json()["detail"].lower()
    dispatch.assert_not_called()


def test_receive_webhook_blocked_run_without_eval_blocked_acks_202(client: TestClient) -> None:
    """A run whose error_code is NOT eval_blocked must still ack 'accepted' — the
    422 ack is reserved strictly for guardrail-blocked runs (FAR-213)."""
    run_mock = _make_mock_run()
    run_mock.error_code = "node_cancelled"
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000", "X-Modulo-Webhook-Secret": "test-hmac"},
        )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    dispatch.assert_called_once()


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


def test_receive_webhook_guardrail_blocked_returns_400_and_commits(client: TestClient) -> None:
    """A block-action guardrail at the trigger boundary maps to a 400 AND the
    transaction commits (the ``guardrail_blocked`` TriggerEvent + stored raw
    payload written by the engine survive the 4xx — reject-and-retry, not
    acked). The engine never returns a run, so no background dispatch fires."""
    from modulo.core.trigger_engine.pre_guardrail import GuardrailBlockedAtIntakeError

    session = _make_mock_session()
    begin_cm = session.begin.return_value

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
            patch("modulo.api.routes.webhooks.set_rls_org"),
        ):
            m.side_effect = GuardrailBlockedAtIntakeError("no-secrets: blocked payload", guardrail_name="no-secrets")
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": "1700000000"},
            )
    finally:
        app.dependency_overrides.pop(get_db_session, None)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "no-secrets: blocked payload"
    # The engine wrote the guardrail_blocked event + stored raw payload INSIDE
    # the transaction; the route catches in-transaction (paused pattern) so the
    # transaction COMMITS (aexit called with no exception → commit, not rollback).
    aexit = begin_cm.__aexit__
    assert aexit.await_count == 1
    exc_tuple = aexit.await_args.args
    assert exc_tuple == (None, None, None)
    dispatch.assert_not_called()


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


def test_receive_webhook_snapshot_lock_busy_returns_503_error(client: TestClient) -> None:
    """FAR-527: a busy per-pipeline snapshot advisory lock used to ack 202
    {"status": "queued"} while nothing was queued anywhere — a silent drop.
    The endpoint must surface an honest 503 so well-behaved webhook
    deliverers redeliver, and never fabricate a queued status."""
    from modulo.core.exceptions import SnapshotLockNotAvailableError

    with (
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            side_effect=SnapshotLockNotAvailableError("busy"),
        ),
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": str(int(time.time()))},
        )

    assert resp.status_code == 503
    body = resp.json()
    assert "snapshot lock unavailable" in body["detail"]
    assert "retry" in body["detail"].lower()
    assert body["title"] == "Service Unavailable"
    assert "run_id" not in body
    m.assert_not_called()
    dispatch.assert_not_called()


def test_replay_webhook_snapshot_lock_busy_returns_503_error(client: TestClient) -> None:
    """FAR-527: replay hitting the snapshot advisory lock returns an honest
    503 (never a fabricated queued status), engine never called."""
    from modulo.core.exceptions import SnapshotLockNotAvailableError

    event_id = uuid.uuid4()
    with (
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            side_effect=SnapshotLockNotAvailableError("busy"),
        ),
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_auth_headers("admin"),
        )

    assert resp.status_code == 503
    body = resp.json()
    assert "snapshot lock unavailable" in body["detail"]
    assert body["title"] == "Service Unavailable"
    assert "run_id" not in body
    m.assert_not_called()
    dispatch.assert_not_called()


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
    rejected at the route level (401). HMAC-less triggers remain public by design.
    The secret lives on the trigger loaded via the SYSTEM session bootstrap."""
    session = _make_hmac_session()
    system_session = make_system_session_mock(trigger_config={"hmac_secret": "test-hmac-secret"})

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
    try:
        with patch("modulo.api.routes.webhooks.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": str(int(time.time()))},
            )
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_system_db_session, None)
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


def test_receive_webhook_bootstrap_reads_trigger_via_system_session(client: TestClient) -> None:
    """Regression (FAR-523): the trigger bootstrap read must go through the
    system session, BEFORE any app-session RLS org context exists - a
    pre-context read of the org-scoped ``triggers`` table on the app session
    matches zero rows under production RLS (modulo_app is NOBYPASSRLS)."""
    from sqlalchemy.sql import Select

    system_mock = make_system_session_mock(trigger_org_id=_ORG_ID)
    app_session = _make_mock_session()
    app_tables: list[str] = []
    app_execute = app_session.execute

    async def _recording_execute(stmt: object, *args: object, **kwargs: object):
        if isinstance(stmt, Select):
            froms = stmt.get_final_froms()
            if froms:
                app_tables.append(getattr(froms[0], "name", ""))
        return await app_execute(stmt, *args, **kwargs)

    app_session.execute = _recording_execute

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    async def override_app() -> AsyncGenerator[AsyncMock, None]:
        yield app_session

    app.dependency_overrides[get_system_db_session] = override_system
    app.dependency_overrides[get_db_session] = override_app
    run_mock = _make_mock_run()
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.dispatch_run", new_callable=AsyncMock, return_value=("enqueued", "j")),
            patch("modulo.api.routes.webhooks.set_rls_org"),
        ):
            m.return_value = (run_mock, None, {})
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": "1700000000", "X-Modulo-Webhook-Secret": "test-hmac"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)
        app.dependency_overrides.pop(get_db_session, None)

    assert resp.status_code == 202
    # The system session ran EXACTLY the bootstrap: the trigger read, and
    # nothing else (the redundant pipeline org read was dropped with the
    # shared helper).
    system_tables = [c.args[0].get_final_froms()[0].name for c in system_mock.execute.await_args_list if c.args]
    assert system_tables == ["triggers"]
    # The app session ran NO pre-context entity reads of the org-scoped
    # trigger/pipeline tables (any such read 404s under production RLS).
    assert "triggers" not in app_tables
    assert "pipelines" not in app_tables


def test_receive_webhook_missing_trigger_returns_404(client: TestClient) -> None:
    """A trigger absent from the SYSTEM (instance-global) read is a real 404."""
    system_mock = make_system_session_mock(trigger_found=False)

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    try:
        with patch("modulo.api.routes.webhooks.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": "1700000000"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"


def test_receive_webhook_soft_deleted_trigger_returns_404(client: TestClient) -> None:
    """A SOFT-DELETED trigger must not accept deliveries: the bootstrap helper
    filters ``deleted_at IS NULL``, so the row reads as absent → 404. The
    system-session mock only "misses" the row when the executed statement
    actually carries the soft-delete filter — dropping the filter from the
    helper query fails this test (delivery would proceed)."""
    system_mock = make_system_session_mock(trigger_deleted=True)

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": str(int(time.time()))},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"
    m.assert_not_called()


def test_receive_webhook_other_org_trigger_returns_404_no_writes(client: TestClient) -> None:
    """Cross-tenant isolation (FAR-523): an authenticated org-A principal
    referencing an org-B trigger gets the same 404 as a missing trigger —
    fail closed, no cross-org enumeration. No snapshot is created, the engine
    is never reached, and nothing is written on the app session."""
    other_org = uuid.uuid4()
    system_mock = make_system_session_mock(trigger_org_id=other_org)
    app_session = _make_mock_session()

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    async def override_app() -> AsyncGenerator[AsyncMock, None]:
        yield app_session

    from modulo.api.dependencies import get_current_tenant_user_optional

    app.dependency_overrides[get_system_db_session] = override_system
    app.dependency_overrides[get_db_session] = override_app
    app.dependency_overrides[get_current_tenant_user_optional] = lambda: AuthenticatedPrincipal(
        username="tester", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
            patch("modulo.api.routes.webhooks.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": "1700000000"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_current_tenant_user_optional, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"
    m.assert_not_called()
    snap.assert_not_called()
    app_session.add.assert_not_called()
    # The bootstrap 404 aborted the transaction (aexit saw the exception ->
    # rollback), so nothing the route touched could have been persisted.
    aexit = app_session.begin.return_value.__aexit__
    assert aexit.await_count == 1
    assert aexit.await_args.args[0] is TriggerNotFoundError


def test_receive_webhook_degraded_system_engine_returns_503(client: TestClient) -> None:
    """When the modulo_system role is not provisioned the BYPASSRLS bootstrap
    would silently match zero rows and every delivery would 404. The route
    must refuse loudly with a 503 (log code webhooks.system_bootstrap_degraded)
    — distinguishable from a genuine 404."""
    with patch("modulo.api.routes.webhooks.system_engine_is_fallback", return_value=True):
        resp = client.post(
            f"/api/v1/triggers/{uuid.uuid4()}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": "1700000000"},
        )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "System database not provisioned; trigger delivery unavailable"


def test_receive_webhook_trigger_busy_records_delivery_then_acks(client: TestClient) -> None:
    """Concurrent same-trigger deliveries serialize on the engine's advisory
    lock. The loser is NOT executed and NOT auto-queued (the engine raises
    before any TriggerEvent and the main transaction rolls back) — the route
    must RECORD the busy delivery in a fresh transaction and only then ack
    202, so the delivery is never silently lost (2xx suppresses Slack-side
    retries BY DESIGN; replay is the recovery path)."""
    from modulo.api.trigger_busy import BUSY_ACK_DETAIL
    from modulo.core.trigger_engine import TriggerBusyError

    with (
        patch("modulo.api.routes.webhooks._trigger_engine.handle_webhook", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.record_busy_delivery", new_callable=AsyncMock) as record,
        patch("modulo.api.routes.webhooks.set_rls_org"),
    ):
        m.side_effect = TriggerBusyError(_TRIGGER_ID)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            json={"event": "test"},
            headers={"X-Modulo-Timestamp": str(int(time.time()))},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["detail"] == BUSY_ACK_DETAIL
    assert body.get("run_id") is None
    dispatch.assert_not_called()
    # The busy delivery was recorded (org + raw body + parsed payload) before
    # the ack was sent.
    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert kwargs["trigger_id"] == _TRIGGER_ID
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["trigger_type"] == "webhook"
    assert kwargs["raw_payload"] == {"event": "test"}
    assert kwargs["raw_body"].startswith(b'{"event"')
    assert len(kwargs["payload_hash"]) == 64


def test_receive_webhook_invalid_config_json_returns_400(client: TestClient) -> None:
    """A non-dict config_json (schema drift / manual edit) must 400 at the
    route, not AttributeError -> 500 on external ingress."""
    system_mock = make_system_session_mock(trigger_config=["not-a-dict"])

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    try:
        with patch("modulo.api.routes.webhooks.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/webhook",
                json={"event": "test"},
                headers={"X-Modulo-Timestamp": "1700000000"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Trigger configuration is invalid"
