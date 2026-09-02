"""Unit tests for POST /api/v1/triggers/{id}/webhook/replay/{event_id}.

Covers replay success, missing event (404), and the ADR 017 replay auth
contract: a principal must hold the ``run.trigger`` (runner) permission, and
an unauthenticated caller must present a valid HMAC signature over the stored
payload. Unauthenticated replay without HMAC is rejected (401).
"""

import hashlib
import hmac
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_system_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, create_access_token
from modulo.settings import Settings, get_settings
from tests.unit.api.conftest import make_system_session_mock

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_HMAC_SECRET = "test-hmac-secret"
_STORED_BODY = b'{"event": "replayed"}'

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_run() -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    return r


def _make_mock_session(*, trigger_config: dict | None = None, stored_payload: bool = True) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    trigger_mock = MagicMock()
    trigger_mock.id = _TRIGGER_ID
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.config_json = trigger_config

    pipeline_mock = MagicMock()
    pipeline_mock.id = trigger_mock.pipeline_id
    pipeline_mock.organisation_id = _ORG_ID

    payload_mock = MagicMock()
    payload_mock.raw_body = _STORED_BODY

    async def _execute_side_effect(stmt, *args, **kwargs):
        result = MagicMock()
        if isinstance(stmt, Select):
            froms = stmt.get_final_froms()
            table = getattr(froms[0], "name", "") if froms else ""
            if table == "triggers":
                result.scalar_one_or_none.return_value = trigger_mock
            elif table == "pipelines":
                result.scalar_one_or_none.return_value = pipeline_mock
            elif table == "webhook_payloads":
                result.scalar_one_or_none.return_value = payload_mock if stored_payload else None
            else:
                result.scalar_one_or_none.return_value = None
        return result

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    return session


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


def _hmac_headers(body: bytes = _STORED_BODY, secret: str = _HMAC_SECRET) -> dict[str, str]:
    ts = int(time.time())
    signature = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Modulo-Timestamp": str(ts), "X-Modulo-Webhook-Secret": signature}


@pytest.fixture(autouse=True)
def _patch_snapshot_creator() -> Generator[None, None, None]:
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
    mock_system_session = make_system_session_mock(trigger_config={"hmac_secret": _HMAC_SECRET})

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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_replay_webhook_returns_202(client: TestClient) -> None:
    """A principal with the runner-or-above role may replay (no HMAC needed)."""
    event_id = uuid.uuid4()
    run_mock = _make_mock_run()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.dispatch_run"),
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
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


def test_replay_webhook_runner_role_allowed(client: TestClient) -> None:
    """The exact minimum is ``runner`` — a runner principal may replay."""
    event_id = uuid.uuid4()
    run_mock = _make_mock_run()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.dispatch_run"),
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
    ):
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_auth_headers("runner"),
        )

    assert resp.status_code == 202


def test_replay_webhook_viewer_denied(client: TestClient) -> None:
    """A viewer principal is below the runner minimum and is denied (403)."""
    event_id = uuid.uuid4()
    resp = client.post(
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
        headers=_auth_headers("viewer"),
    )

    assert resp.status_code == 403
    assert "run.trigger" in resp.json()["detail"]


def test_replay_webhook_not_found_returns_404(client: TestClient) -> None:
    from modulo.core.trigger_engine import ReplayNotFoundError

    event_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
    ):
        m.side_effect = ReplayNotFoundError(event_id)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_auth_headers("admin"),
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger event not found"


def test_replay_webhook_unauthenticated_without_hmac_returns_401(client: TestClient) -> None:
    """An unauthenticated caller without a valid HMAC signature is rejected.

    This was the ADR 017 vulnerability: anyone who knew a trigger_id + event_id
    could re-create a run. Now the unauthenticated path requires valid HMAC.
    """
    event_id = uuid.uuid4()
    resp = client.post(
        f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
        headers={"X-Modulo-Timestamp": str(int(time.time()))},
    )

    assert resp.status_code == 401
    assert "HMAC" in resp.json()["detail"]


def test_replay_webhook_unauthenticated_with_valid_hmac_returns_202(client: TestClient) -> None:
    """An unauthenticated caller with a valid HMAC signature may replay.

    The trigger must have an ``hmac_secret`` configured; the signature covers
    the stored payload (``timestamp.body``), matching receive_webhook.
    """
    mock_session = _make_mock_session(trigger_config={"hmac_secret": _HMAC_SECRET})

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    try:
        event_id = uuid.uuid4()
        run_mock = _make_mock_run()
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.dispatch_run"),
            patch("modulo.api.routes.webhooks.set_rls_org"),
            patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
        ):
            m.return_value = (run_mock, None, {})
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
                headers=_hmac_headers(),
            )
        assert resp.status_code == 202
        assert resp.json()["run_id"] == str(_RUN_ID)
    finally:
        app.dependency_overrides[get_db_session] = None
        app.dependency_overrides.pop(get_db_session, None)


def test_replay_webhook_unauthenticated_bad_hmac_returns_401(client: TestClient) -> None:
    """An unauthenticated caller with a wrong HMAC signature is rejected."""
    mock_session = _make_mock_session(trigger_config={"hmac_secret": _HMAC_SECRET})

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    try:
        event_id = uuid.uuid4()
        ts = int(time.time())
        wrong = "sha256=" + hmac.new(b"wrong-secret", f"{ts}.".encode() + _STORED_BODY, hashlib.sha256).hexdigest()
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers={"X-Modulo-Timestamp": str(ts), "X-Modulo-Webhook-Secret": wrong},
        )
        assert resp.status_code == 401
        assert "HMAC" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def test_replay_webhook_unauthenticated_hmacless_trigger_denied(client: TestClient) -> None:
    """An unauthenticated replay against an HMAC-less trigger is denied (401).

    Replay is NOT public run-creation like receive_webhook — a trigger with no
    shared secret cannot authenticate an unauthenticated replay. The trigger
    (and its secret) is resolved on the SYSTEM session bootstrap, so the
    override targets ``get_system_db_session``.
    """
    mock_system_session = make_system_session_mock(trigger_config=None)

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_system_session

    app.dependency_overrides[get_system_db_session] = override_system_session
    try:
        event_id = uuid.uuid4()
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_hmac_headers(),
        )
        assert resp.status_code == 401
        assert "HMAC" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)


def test_replay_webhook_bootstrap_reads_trigger_via_system_session(client: TestClient) -> None:
    """Regression (FAR-523): the replay bootstrap read must go through the
    system session BEFORE any app-session RLS org context exists — a
    pre-context read of the org-scoped ``triggers`` table on the app session
    matches zero rows under production RLS (modulo_app is NOBYPASSRLS)."""
    system_mock = make_system_session_mock(trigger_config={"hmac_secret": _HMAC_SECRET})

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    event_id = uuid.uuid4()
    run_mock = _make_mock_run()
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.dispatch_run"),
            patch("modulo.api.routes.webhooks.set_rls_org"),
            patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
        ):
            m.return_value = (run_mock, None, {})
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
                headers=_auth_headers("admin"),
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 202
    tables = [c.args[0].get_final_froms()[0].name for c in system_mock.execute.await_args_list if c.args]
    assert "triggers" in tables


def test_replay_webhook_missing_trigger_returns_404(client: TestClient) -> None:
    """A trigger absent from the SYSTEM (instance-global) read is a real 404."""
    system_mock = make_system_session_mock(trigger_found=False)

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    try:
        resp = client.post(
            f"/api/v1/triggers/{uuid.uuid4()}/webhook/replay/{uuid.uuid4()}",
            headers=_auth_headers("admin"),
        )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"


def _make_org_aware_app_session(*, payload_org_id: uuid.UUID) -> AsyncMock:
    """App-session mock whose stored-payload lookup is org-faithful.

    The payload row "lives" in ``payload_org_id``. The lookup finds it only
    when the route's query does NOT constrain ``organisation_id`` (the
    regression condition this guards) or pins the payload's own org; pinning
    any OTHER org (what the route does for another org's trigger) misses
    exactly like the real RLS-scoped lookup.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    payload_mock = MagicMock()
    payload_mock.raw_body = _STORED_BODY
    payload_row = MagicMock()
    payload_row.scalar_one_or_none = MagicMock(return_value=payload_mock)
    empty_row = MagicMock()
    empty_row.scalar_one_or_none = MagicMock(return_value=None)

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        if isinstance(stmt, Select):
            froms = stmt.get_final_froms()
            table = getattr(froms[0], "name", "") if froms else ""
            if table == "webhook_payloads":
                params = stmt.compile().params
                org_param = next((v for k, v in params.items() if k.startswith("organisation_id")), None)
                if org_param is None or org_param == payload_org_id:
                    return payload_row
                return empty_row
        return empty_row

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    return session


def test_replay_other_org_event_unauthenticated_returns_404_no_disclosure(client: TestClient) -> None:
    """Cross-tenant isolation (FAR-523): an unauthenticated replay referencing
    ANOTHER org's event_id gets a bare 404 — the org-scoped stored-payload
    lookup misses (the session is pinned to the trigger's org), so the other
    org's raw payload is never disclosed and no run is created. If the org
    predicate were dropped from the lookup, this test FAILS (the payload would
    be "found" and the replay would proceed to 202)."""
    other_org = uuid.uuid4()
    system_mock = make_system_session_mock(trigger_config={"hmac_secret": _HMAC_SECRET}, trigger_org_id=other_org)
    # The stored payload belongs to org-A; the route pins the session to
    # org-B (derived from the trigger), so the lookup must miss.
    app_session = _make_org_aware_app_session(payload_org_id=_ORG_ID)

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    async def override_app() -> AsyncGenerator[AsyncMock, None]:
        yield app_session

    app.dependency_overrides[get_system_db_session] = override_system
    app.dependency_overrides[get_db_session] = override_app
    event_id = uuid.uuid4()
    try:
        with (
            patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.webhooks.set_rls_org"),
            patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/webhook/replay/{event_id}",
                headers=_hmac_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)
        app.dependency_overrides.pop(get_db_session, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger event not found"
    assert _STORED_BODY.decode() not in resp.text
    m.assert_not_called()


def test_replay_webhook_degraded_system_engine_returns_503(client: TestClient) -> None:
    """Replay shares receive_webhook's degraded-system-engine contract: 503
    with the distinct bootstrap-degraded failure, never a silent 404."""
    with patch("modulo.api.routes.webhooks.system_engine_is_fallback", return_value=True):
        resp = client.post(
            f"/api/v1/triggers/{uuid.uuid4()}/webhook/replay/{uuid.uuid4()}",
            headers=_auth_headers("admin"),
        )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "System database not provisioned; trigger delivery unavailable"


def test_replay_webhook_trigger_busy_records_delivery_then_acks(client: TestClient) -> None:
    """Replay serializes on the same engine advisory lock: the loser is NOT
    executed and NOT auto-queued (the engine raises before any TriggerEvent
    and the main transaction rolls back) — the route must RECORD the busy
    replay (a ``concurrency_limit_reached`` event carrying the original
    event's payload hash) and only then ack 202, never a false ack or 500."""
    from modulo.api.trigger_busy import BUSY_ACK_DETAIL
    from modulo.core.trigger_engine import TriggerBusyError

    event_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.webhooks._trigger_engine.replay_event", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.webhooks.record_busy_delivery", new_callable=AsyncMock) as record,
        patch("modulo.api.routes.webhooks.set_rls_org"),
        patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
    ):
        m.side_effect = TriggerBusyError(_TRIGGER_ID)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_auth_headers("admin"),
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["detail"] == BUSY_ACK_DETAIL
    assert body.get("run_id") is None
    dispatch.assert_not_called()
    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert kwargs["trigger_id"] == _TRIGGER_ID
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["trigger_type"] == "webhook"
    assert kwargs["source_event_id"] == event_id
    # A busy replay stores no NEW payload row — the original event's payload
    # already exists and remains the replayable artefact.
    assert kwargs.get("raw_body") is None


def test_replay_webhook_invalid_config_json_returns_400(client: TestClient) -> None:
    """Unauthenticated replay reads the trigger's config_json for the HMAC
    secret — a non-dict value must 400, not AttributeError -> 500."""
    system_mock = make_system_session_mock(trigger_config="bogus")

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    try:
        with patch("modulo.api.routes.webhooks.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/webhook/replay/{uuid.uuid4()}",
                headers={"X-Modulo-Timestamp": str(int(time.time()))},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Trigger configuration is invalid"


def test_replay_webhook_authenticated_invalid_config_json_returns_400(client: TestClient) -> None:
    """Authenticated replay hits the SAME config-shape hole: before the
    validation moved into the shared bootstrap helper, the isinstance guard
    only ran inside ``if principal is None:`` — a runner replaying a trigger
    with corrupt config_json got AttributeError -> 500. The helper now raises
    TriggerConfigInvalidError for every path → 400."""
    system_mock = make_system_session_mock(trigger_config="bogus")

    async def override_system() -> AsyncGenerator[AsyncMock, None]:
        yield system_mock

    app.dependency_overrides[get_system_db_session] = override_system
    try:
        with patch("modulo.api.routes.webhooks.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/webhook/replay/{uuid.uuid4()}",
                headers=_auth_headers("admin"),
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Trigger configuration is invalid"
