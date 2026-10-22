"""Unit tests for POST /api/v1/triggers/{id}/slack.

Covers:
* URL verification challenge handshake (echo challenge back).
* app_mention event delivery → 202 accepted + background dispatch.
* signature failures → 401.
* malformed payload → 400.
* paused org → 202 {"status": "paused"}.
* trigger not found → 404.
"""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.api.dependencies import _get_engine, get_db_session, get_system_db_session
from modulo.api.main import app
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.trigger_engine import (
    DuplicateWebhookError,
    PipelineRateLimitError,
    TriggerConfigInvalidError,
    TriggerInactiveError,
    TriggerNotFoundError,
)
from modulo.core.trigger_engine.slack_app_mention import (
    SlackAppMentionParseError,
    SlackEventTypeError,
    SlackSignatureError,
    SlackTimestampExpiredError,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.conftest import _ProvisionedSystemSettings, make_system_session_mock
from tests.unit.api.mock_session import configure_mock_session, configure_rls_preamble

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_SECRET = "my-slack-signing-secret"
_CHALLENGE = "3eZbrw1aBm2rZgRNFdxV2598559m"


@pytest.fixture(autouse=True)
def _stub_audit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1516: stub the fresh ``audit_session`` the ``audited_system(...)``
    dependency writes on (a real engine — no database in the unit tier); the
    dependency itself still runs."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def _factory() -> AsyncGenerator[AsyncMock, None]:
        yield session

    monkeypatch.setattr("modulo.core.audit_coverage._shared_session_factory", lambda: _factory)


@pytest.fixture(autouse=True)
def _system_engine_isolated(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """FAR-1569: keep the route's 503 system-engine gate deterministic per test.

    ``receive_slack_event`` refuses with 503 (``slack.system_bootstrap_degraded``)
    when ``system_engine_is_fallback()`` reads True. That flag is a process
    global written as a side effect of the FIRST ``get_or_create_system_engine()``
    call, so one degraded initialisation poisons every later Slack test in the
    session, and its settings reading is whichever fixture happened to be
    installed at that moment.

    Two leaks are closed here, both by resetting the three system-engine
    globals around each test and presenting a provisioned system URL for the
    duration of the test:

    * a state left behind by an earlier test/package;
    * the api conftest's own ``_provisioned_system_engine`` patch being lost
      for this module when pytest revisits the directory through a different
      collector node (autouse fixtures from a conftest are keyed to the node
      that first loaded it) - a module fixture is registered against this
      module, so it always applies.

    Teardown leaves the globals CLEARED rather than restoring a snapshot: a
    cached fallback engine is exactly the pollution being fixed.
    """
    from modulo.api import dependencies as _deps

    _deps._SYSTEM_ASYNC_ENGINE = None
    _deps._SYSTEM_SESSION_FACTORY = None
    _deps._SYSTEM_ENGINE_IS_FALLBACK = False
    monkeypatch.setattr(_deps, "get_settings", lambda: _ProvisionedSystemSettings())
    try:
        yield
    finally:
        _deps._SYSTEM_ASYNC_ENGINE = None
        _deps._SYSTEM_SESSION_FACTORY = None
        _deps._SYSTEM_ENGINE_IS_FALLBACK = False


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        redis_url="",
    )


def _slack_sig(body: bytes, secret: str, timestamp: str) -> str:
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _lock_engine_stub() -> MagicMock:
    """Stub for the DEDICATED NullPool lock engine (FAR-1287).

    The snapshot's advisory lock is resolved by ``_dedicated_lock_engine`` and
    drawn from that engine — never from the session's pool — so a test whose
    session reaches ``create_snapshot_from_live_graph`` must stub the resolver.
    """
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = [lock_result, MagicMock()]  # try-lock, then unlock
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = AsyncMock(return_value=lock_conn)
    return engine


def _make_trigger_session() -> AsyncMock:
    """Session whose trigger carries a signing_secret so route-level signature
    validation runs against the real secret."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # FAR-1629: AsyncSession.in_transaction() is sync - a bare AsyncMock
    # auto-creates it as an async child, leaving an un-awaited coroutine when
    # _ensure_active_transaction (db/rls.py) calls it.
    session.in_transaction = MagicMock(return_value=True)
    # FAR-1287: create_snapshot_from_live_graph allocates the snapshot version
    # inside a ``session.begin_nested()`` SAVEPOINT. A bare AsyncMock's
    # begin_nested() returns a plain coroutine, which is not an async context
    # manager, so the route would 500 with TypeError. Stub it the same way
    # ``begin`` is stubbed above.
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)

    trigger_mock = MagicMock()
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.active = True
    trigger_mock.trigger_type = "slack_app_mention"
    trigger_mock.config_json = {"signing_secret": _SECRET}
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    session.execute = AsyncMock(return_value=execute_result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    # FAR-1287: the route creates the pipeline snapshot before dispatch; without
    # a bound AsyncEngine the snapshot creation refuses to start (RuntimeError),
    # so wire the derivation contract even though tests stub the resolver.
    session.bind = _lock_engine_stub()
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_trigger_session()
    mock_system_session = make_system_session_mock(trigger_config={"signing_secret": _SECRET}, trigger_org_id=_ORG_ID)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_system_session

    async def override_optional_principal() -> AuthenticatedPrincipal | None:
        return AuthenticatedPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_system_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    from modulo.api.dependencies import get_current_tenant_user_optional

    app.dependency_overrides[get_current_tenant_user_optional] = override_optional_principal
    with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _event_body() -> bytes:
    return json.dumps(
        {
            "token": "verification-token",
            "team_id": "T12345",
            "api_app_id": "A12345",
            "event_id": "Ev1234567890",
            "event_time": 1234567890,
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "user": "U12345",
                "text": "<@U012345> please process this",
                "ts": "1234567890.000001",
                "channel": "C12345",
            },
        }
    ).encode()


def _headers(ts: str, body: bytes, secret: str = _SECRET) -> dict[str, str]:
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _slack_sig(body, secret, ts),
    }


def test_challenge_handshake_returns_challenge(client: TestClient) -> None:
    body = json.dumps({"type": "url_verification", "challenge": _CHALLENGE}).encode()
    ts = str(int(time.time()))
    with patch("modulo.api.routes.slack.set_rls_org"):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": _CHALLENGE}


def test_challenge_bad_signature_returns_401(client: TestClient) -> None:
    body = json.dumps({"type": "url_verification", "challenge": _CHALLENGE}).encode()
    ts = str(int(time.time()))
    with patch("modulo.api.routes.slack.set_rls_org"):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": "v0=deadbeef",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Slack signature verification failed"


def test_app_mention_delivery_returns_202(client: TestClient) -> None:
    body = _event_body()
    ts = str(int(time.time()))
    run_mock = MagicMock()
    run_mock.id = _RUN_ID
    # FAR-1141 / ADR-042: the ack is a claim-ready run surface. The column is
    # always present on a real row (FAR-1566 removed the coercion that used to
    # rescue a stand-in which never set it), so this one sets it explicitly.
    run_mock.execution_origin = None

    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch(
            "modulo.api.routes.slack.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.api.routes.slack.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
    ):
        snap_mock = MagicMock()
        snap_mock.id = uuid.uuid4()
        snap.return_value = snap_mock
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    body_json = resp.json()
    assert body_json["status"] == "accepted"
    assert body_json["run_id"] == str(_RUN_ID)
    # Present as a key with the run's own origin — never omitted.
    assert body_json["execution_origin"] is None
    m.assert_awaited_once()


def test_app_mention_ack_carries_dispatched_execution_origin(client: TestClient) -> None:
    """A dispatched run's Slack ack must read ``dispatched``, never
    indistinguishably from a run Modulo executed itself."""
    body = _event_body()
    ts = str(int(time.time()))
    run_mock = MagicMock()
    run_mock.id = _RUN_ID
    run_mock.execution_origin = "dispatched"

    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch(
            "modulo.api.routes.slack.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.api.routes.slack.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
    ):
        snap_mock = MagicMock()
        snap_mock.id = uuid.uuid4()
        snap.return_value = snap_mock
        m.return_value = (run_mock, None, {})
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    assert resp.json()["execution_origin"] == "dispatched"


def test_app_mention_bad_signature_returns_401(client: TestClient) -> None:
    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": "v0=badbadbad",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 401
    m.assert_not_called()


def test_app_mention_missing_signature_returns_401(client: TestClient) -> None:
    body = _event_body()
    ts = str(int(time.time()))
    with patch("modulo.api.routes.slack.set_rls_org"):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={"X-Slack-Request-Timestamp": ts, "Content-Type": "application/json"},
        )
    assert resp.status_code == 401


def test_stale_timestamp_returns_400(client: TestClient) -> None:
    body = _event_body()
    stale_ts = str(int(time.time()) - 600)
    with patch("modulo.api.routes.slack.set_rls_org"):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(stale_ts, body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "replay window" in resp.json()["detail"]


def test_missing_json_body_returns_400(client: TestClient) -> None:
    ts = str(int(time.time()))
    body = b"not json"
    with patch("modulo.api.routes.slack.set_rls_org"):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "JSON object" in resp.json()["detail"]


def test_paused_org_returns_202_paused(client: TestClient) -> None:
    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack._dispatch_slack_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.api.routes._trigger_events.org_row_exists", new_callable=AsyncMock, return_value=True),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    assert resp.json() == {"status": "paused"}
    m.assert_not_called()
    dispatch.assert_not_called()


def test_trigger_not_found_returns_404(client: TestClient) -> None:
    """A trigger absent from the SYSTEM (instance-global) bootstrap read is a
    real 404 (FAR-523: the bootstrap runs on the system session)."""
    body = _event_body()
    ts = str(int(time.time()))
    system_session = make_system_session_mock(trigger_found=False)

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_system_db_session] = override_system_session
    try:
        with patch("modulo.api.routes.slack.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{_TRIGGER_ID}/slack",
                content=body,
                headers={**_headers(ts, body), "Content-Type": "application/json"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"


def test_app_mention_duplicate_event_returns_400(client: TestClient) -> None:
    body = _event_body()
    ts = str(int(time.time()))
    from modulo.core.trigger_engine import DuplicateWebhookError

    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.api.routes.slack.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
    ):
        snap_mock = MagicMock()
        snap_mock.id = uuid.uuid4()
        snap.return_value = snap_mock
        m.side_effect = DuplicateWebhookError("hash")
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Duplicate Slack event"


def test_app_mention_other_org_trigger_returns_404_no_run(client: TestClient) -> None:
    """Cross-tenant isolation (FAR-523): an org-A principal referencing an
    org-B trigger gets the same 404 as a missing trigger — fail closed, no
    cross-org enumeration, and no run is created."""
    other_org = uuid.uuid4()
    system_session = make_system_session_mock(trigger_config={"signing_secret": _SECRET}, trigger_org_id=other_org)

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    from modulo.api.dependencies import get_current_tenant_user_optional

    app.dependency_overrides[get_system_db_session] = override_system_session
    app.dependency_overrides[get_current_tenant_user_optional] = lambda: AuthenticatedPrincipal(
        username="attacker", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    body = _event_body()
    ts = str(int(time.time()))
    try:
        with (
            patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
            patch("modulo.api.routes.slack.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/slack",
                content=body,
                headers={**_headers(ts, body), "Content-Type": "application/json"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)
        app.dependency_overrides.pop(get_current_tenant_user_optional, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger not found"
    m.assert_not_called()


def test_receive_slack_event_degraded_system_engine_returns_503(client: TestClient) -> None:
    """When the modulo_system role is not provisioned the bootstrap would
    silently 404 every delivery. The route must refuse loudly with a 503
    (log code slack.system_bootstrap_degraded) — distinguishable from a 404."""
    with patch("modulo.api.routes.slack.system_engine_is_fallback", return_value=True):
        body = _event_body()
        ts = str(int(time.time()))
        resp = client.post(
            f"/api/v1/triggers/{uuid.uuid4()}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "System database not provisioned; trigger delivery unavailable"


def test_app_mention_trigger_busy_records_delivery_then_acks(client: TestClient) -> None:
    """Concurrent same-trigger slack deliveries serialize on the engine's
    advisory lock. The loser is NOT executed and NOT auto-queued (the engine
    raises before any TriggerEvent and the main transaction rolls back) — the
    route must RECORD the busy delivery and only then ack 202. Slack
    suppresses retries on 2xx BY DESIGN, so without the recording the
    delivery would be silently lost."""
    from modulo.api.trigger_busy import BUSY_ACK_DETAIL
    from modulo.core.trigger_engine import TriggerBusyError

    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack.record_busy_delivery", new_callable=AsyncMock) as record,
        patch("modulo.api.routes.slack.set_rls_org"),
        # FAR-1287: the route snapshots the pipeline before dispatch, and the
        # snapshot's advisory lock is resolved onto a dedicated engine.
        patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=_lock_engine_stub()),
    ):
        m.side_effect = TriggerBusyError(_TRIGGER_ID)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    body_json = resp.json()
    assert body_json["status"] == "queued"
    assert body_json["detail"] == BUSY_ACK_DETAIL
    assert body_json.get("run_id") is None
    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert kwargs["trigger_id"] == _TRIGGER_ID
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["trigger_type"] == "slack_app_mention"
    assert len(kwargs["payload_hash"]) == 64


def test_app_mention_rate_limited_records_event_then_returns_429(client: TestClient) -> None:
    """FAR-1916 slack mirror: a delivery refused by the pipeline's rate limit
    returns 429 AND the ``rate_limited`` TriggerEvent + raw payload survive the
    refusal.

    The engine writes the ``rate_limited`` event INSIDE the main transaction at
    both raise sites, so that write rolls back with the unwound transaction —
    a rate-limited app_mention was refused but invisible. The route now
    re-records the refusal (with the raw body) in a FRESH transaction AFTER the
    unwind, exactly like the webhook receive path. This test runs the REAL
    post-unwind recorder against a fake fresh session and asserts the persisted
    event + payload rows — not merely that a helper was called."""
    from modulo.db.models.trigger_event import TriggerEvent
    from modulo.db.models.webhook import WebhookPayload

    fresh_session = AsyncMock()
    fresh_begin_cm = AsyncMock()
    fresh_begin_cm.__aenter__ = AsyncMock(return_value=None)
    fresh_begin_cm.__aexit__ = AsyncMock(return_value=False)
    fresh_session.begin = MagicMock(return_value=fresh_begin_cm)
    fresh_session.in_transaction = MagicMock(return_value=True)
    fresh_session.add = MagicMock()

    @asynccontextmanager
    async def _fresh_factory() -> AsyncGenerator[AsyncMock, None]:
        yield fresh_session

    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.api.trigger_busy.async_sessionmaker", return_value=_fresh_factory),
        patch("modulo.api.trigger_busy.get_or_create_engine", return_value=MagicMock()),
        patch("modulo.api.trigger_busy.get_settings", return_value=_make_settings()),
        patch("modulo.api.trigger_busy.set_rls_org", new_callable=AsyncMock),
        # FAR-1287: the route snapshots the pipeline before dispatch, and the
        # snapshot's advisory lock is resolved onto a dedicated engine.
        patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=_lock_engine_stub()),
    ):
        m.side_effect = PipelineRateLimitError(uuid.uuid4(), "k", 3, 60)
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    # The 429 contract is preserved exactly — detail, and NO Retry-After (the
    # rate-limit refusal has never carried one).
    assert resp.status_code == 429, resp.text
    assert "rate limit exceeded" in resp.json()["detail"]
    assert "Retry-After" not in resp.headers

    # The refusal SURVIVED in the fresh transaction opened after the unwind:
    # exactly one rate_limited TriggerEvent, scoped to trigger + org, carrying
    # a hash that matches the stored raw payload row.
    added = [call.args[0] for call in fresh_session.add.call_args_list]
    events = [obj for obj in added if isinstance(obj, TriggerEvent)]
    assert len(events) == 1
    recorded_event = events[0]
    assert recorded_event.validation_result == "rate_limited"
    assert recorded_event.trigger_id == _TRIGGER_ID
    assert recorded_event.organisation_id == _ORG_ID
    payloads = [obj for obj in added if isinstance(obj, WebhookPayload)]
    assert len(payloads) == 1
    assert payloads[0].raw_body == body
    assert recorded_event.raw_payload_hash == hashlib.sha256(body).hexdigest()
    # The fresh transaction COMMITTED — begin() exited cleanly (no rollback).
    assert fresh_begin_cm.__aexit__.await_count == 1
    assert fresh_begin_cm.__aexit__.await_args.args == (None, None, None)


def test_app_mention_archived_pipeline_returns_409(client: TestClient) -> None:
    """FAR-1528: an archived/soft-deleted pipeline refuses the delivery with
    409 at the create_run choke point - never a generic 500."""
    from modulo.core.exceptions import PipelineNotRunnableError

    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack.set_rls_org"),
        # FAR-1287: the route snapshots the pipeline before dispatch, and the
        # snapshot's advisory lock is resolved onto a dedicated engine.
        patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=_lock_engine_stub()),
    ):
        m.side_effect = PipelineNotRunnableError(pipeline_id=uuid.uuid4(), state="archived")
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"]


def test_app_mention_snapshot_lock_busy_returns_503(client: TestClient) -> None:
    """FAR-527 parity: a busy per-pipeline snapshot advisory lock on the Slack
    app-mention path must surface an honest, retryable 503 — never a generic
    500 (matches the webhook + MCP trigger paths)."""
    from modulo.core.exceptions import SnapshotLockNotAvailableError

    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack.set_rls_org"),
        patch(
            "modulo.api.routes.slack.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            side_effect=SnapshotLockNotAvailableError("busy"),
        ),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "snapshot lock unavailable" in detail
    assert "retry" in detail.lower()
    # The engine is never reached once the snapshot cannot be created.
    m.assert_not_called()


def test_app_mention_invalid_config_json_returns_400(client: TestClient) -> None:
    """A non-dict config_json on the trigger must 400 at the route, not
    AttributeError -> 500 on external ingress."""
    system_session = make_system_session_mock(trigger_config="bogus")

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_system_db_session] = override_system_session
    body = _event_body()
    ts = str(int(time.time()))
    try:
        with patch("modulo.api.routes.slack.set_rls_org"):
            resp = client.post(
                f"/api/v1/triggers/{uuid.uuid4()}/slack",
                content=body,
                headers={**_headers(ts, body), "Content-Type": "application/json"},
            )
    finally:
        app.dependency_overrides.pop(get_system_db_session, None)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Trigger configuration is invalid"


def test_paused_org_missing_row_skips_event_write(client: TestClient) -> None:
    """Orphan-org guard on the paused path (S3776 split, changed-lines
    coverage): when the org row was HARD-deleted the paused TriggerEvent
    INSERT is skipped (organisations FK) — still an honest 202 paused ack."""
    body = _event_body()
    ts = str(int(time.time()))
    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.slack._dispatch_slack_run", new_callable=AsyncMock) as dispatch,
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.api.routes._trigger_events.org_row_exists", new_callable=AsyncMock, return_value=False),
    ):
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/slack",
            content=body,
            headers={**_headers(ts, body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    assert resp.json() == {"status": "paused"}
    m.assert_not_called()
    dispatch.assert_not_called()


async def test_ack_slack_busy_delivery_unresolvable_org_refuses_429() -> None:
    """Defensive arm (S3776 split, changed-lines coverage): a busy ack with
    no resolved org must refuse with 429 + Retry-After, never a false 2xx."""
    from modulo.api.routes.slack import _ack_slack_busy_delivery

    with pytest.raises(HTTPException) as exc_info:
        await _ack_slack_busy_delivery(trigger_id=_TRIGGER_ID, org_id=None, raw_body=b"{}")

    assert exc_info.value.status_code == 429
    headers = exc_info.value.headers
    assert headers is not None
    assert headers["Retry-After"] == "5"


async def test_record_slack_rate_limit_refusal_unresolvable_org_skips_recording() -> None:
    """Defensive arm (changed-lines coverage): a rate-limit refusal with no
    resolved org has no tenant to scope the event to, so the recorder skips the
    write (best-effort, logged) rather than persist an unscoped row or raise.

    ``org_id`` is always resolved by the time the engine can raise
    ``PipelineRateLimitError``; the route still raises its unchanged 429 after
    the helper returns — this only pins the helper's defensive arm."""
    from modulo.api.routes.slack import _record_slack_rate_limit_refusal

    with patch("modulo.api.routes.slack._record_refused_delivery", new_callable=AsyncMock) as record:
        await _record_slack_rate_limit_refusal(
            trigger_id=_TRIGGER_ID,
            org_id=None,
            raw_body=b"{}",
            raw_payload={},
        )

    record.assert_not_awaited()


def _dispatch_trigger_mock() -> MagicMock:
    trigger = MagicMock()
    trigger.id = _TRIGGER_ID
    trigger.pipeline_id = uuid.uuid4()
    trigger.active = True
    trigger.config_json = {"signing_secret": _SECRET}
    return trigger


@pytest.mark.parametrize(
    ("side_effect", "expected_status"),
    [
        (TriggerNotFoundError(trigger_id=_TRIGGER_ID), 404),
        (TriggerInactiveError(_TRIGGER_ID), 404),
        (TriggerConfigInvalidError(trigger_id=_TRIGGER_ID), 400),
        (SlackTimestampExpiredError("stale"), 400),
        (SlackSignatureError("bad"), 401),
        (SlackEventTypeError("unsupported envelope"), 400),
        (SlackAppMentionParseError("malformed"), 400),
        (DuplicateWebhookError("hash"), 400),
    ],
    ids=[
        "trigger_not_found",
        "trigger_inactive",
        "trigger_config_invalid",
        "slack_timestamp_expired",
        "slack_signature",
        "slack_event_type",
        "slack_app_mention_parse",
        "duplicate_webhook",
    ],
)
async def test_run_slack_pipeline_maps_dispatch_family_errors(side_effect: Exception, expected_status: int) -> None:
    """S3776 split invariant: ``_run_slack_pipeline`` maps the typed errors
    the engine can raise mid-dispatch to the SAME statuses the original
    single route ladder produced — the error→status mapping is unchanged by
    the split (each arm a verbatim subsequence of the original ladder).

    ``PipelineRateLimitError`` is DELIBERATELY excluded from this mapping:
    it must propagate out of the in-transaction helper so the route's outer
    ladder can re-record the refusal post-unwind before raising the 429
    (FAR-1916 slack mirror) — see
    ``test_run_slack_pipeline_propagates_rate_limit_for_post_unwind_record``.
    """
    from modulo.api.routes.slack import _run_slack_pipeline

    session = _make_trigger_session()
    with (
        patch("modulo.api.routes.slack.ensure_triggers_resumable", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock, side_effect=side_effect),
        patch("modulo.api.routes.slack.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
    ):
        snap_mock = MagicMock()
        snap_mock.id = uuid.uuid4()
        snap.return_value = snap_mock
        with pytest.raises(HTTPException) as exc_info:
            await _run_slack_pipeline(
                session=session,
                trigger=_dispatch_trigger_mock(),
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                raw_body=b"{}",
                raw_payload={},
                slack_signature="v0=bad",
                slack_timestamp="1",
            )

    assert exc_info.value.status_code == expected_status


async def test_run_slack_pipeline_propagates_rate_limit_for_post_unwind_record() -> None:
    """FAR-1916 slack mirror: ``_run_slack_pipeline`` must PROPAGATE the
    engine's ``PipelineRateLimitError`` (never convert it to a 429 inside the
    transaction).

    The engine writes its ``rate_limited`` TriggerEvent INSIDE the main
    transaction; converting the refusal to an ``HTTPException`` here would
    unwind that transaction with the event lost and put the route's outer
    ladder out of reach, so the post-unwind re-record could never run. This
    pins the propagation contract directly."""
    from modulo.api.routes.slack import _run_slack_pipeline

    session = _make_trigger_session()
    rate_limit_error = PipelineRateLimitError(uuid.uuid4(), None, 1, 60)
    with (
        patch("modulo.api.routes.slack.ensure_triggers_resumable", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock, side_effect=rate_limit_error),
        patch("modulo.api.routes.slack.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
    ):
        snap_mock = MagicMock()
        snap_mock.id = uuid.uuid4()
        snap.return_value = snap_mock
        with pytest.raises(PipelineRateLimitError) as exc_info:
            await _run_slack_pipeline(
                session=session,
                trigger=_dispatch_trigger_mock(),
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                raw_body=b"{}",
                raw_payload={},
                slack_signature="v0=bad",
                slack_timestamp="1",
            )

    assert exc_info.value is rate_limit_error


async def test_verify_slack_delivery_maps_inactive_trigger_to_404() -> None:
    """S3776 split invariant: ``_verify_slack_delivery`` maps the bootstrap
    ladder's ``TriggerInactiveError`` -> 404, exactly as the original inline
    ladder did (the bootstrap helper and the engine's mid-dispatch re-read can
    both raise it, so both ladders keep the arm)."""
    from modulo.api.routes.slack import _verify_slack_delivery

    with (
        patch(
            "modulo.api.routes.slack.load_trigger_and_org_global",
            new_callable=AsyncMock,
            side_effect=TriggerInactiveError(_TRIGGER_ID),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _verify_slack_delivery(
            # See the encrypted-secret test: the real RLS preamble needs a
            # contract-correct (sync ``in_transaction``/``get_bind``) session.
            session=configure_rls_preamble(AsyncMock()),
            system_session=AsyncMock(),
            request=MagicMock(),
            trigger_id=_TRIGGER_ID,
            principal=None,
            raw_body=b"{}",
            slack_signature="v0=bad",
            slack_timestamp="1",
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Trigger not found"


async def test_verify_slack_delivery_missing_signing_secret_returns_401() -> None:
    """S3776 split changed-lines coverage: a well-formed config with NO signing
    secret (``{}``, not the invalid non-dict shape) must fail closed with 401
    before any snapshot/dedup work — never reach the engine unauthenticated."""
    from modulo.api.routes.slack import _verify_slack_delivery

    trigger = MagicMock()
    trigger.config_json = {}
    with (
        patch(
            "modulo.api.routes.slack.load_trigger_and_org_global",
            new_callable=AsyncMock,
            return_value=(trigger, _ORG_ID),
        ),
        patch("modulo.api.routes.slack.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.set_rls_execution_context", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _verify_slack_delivery(
            # See the encrypted-secret test: the real RLS preamble needs a
            # contract-correct (sync ``in_transaction``/``get_bind``) session.
            session=configure_rls_preamble(AsyncMock()),
            system_session=AsyncMock(),
            request=MagicMock(),
            trigger_id=_TRIGGER_ID,
            principal=None,
            raw_body=b"{}",
            slack_signature="v0=bad",
            slack_timestamp="1",
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Slack signature verification failed"


async def test_create_slack_snapshot_none_returns_500_and_skips_pause_check() -> None:
    """S3776 split changed-lines coverage: an INACTIVE trigger skips the org
    pause pre-check, and a ``None`` snapshot fails closed with 500 rather than
    dispatching against a missing snapshot."""
    from modulo.api.routes.slack import _create_slack_snapshot

    trigger = MagicMock()
    trigger.active = False
    trigger.pipeline_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.slack.ensure_triggers_resumable", new_callable=AsyncMock) as ensure,
        patch(
            "modulo.api.routes.slack.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _create_slack_snapshot(session=AsyncMock(), trigger=trigger, trigger_id=_TRIGGER_ID, org_id=_ORG_ID)

    ensure.assert_not_called()
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to create pipeline snapshot for slack trigger"


# ---------------------------------------------------------------------------
# _verify_slack_delivery — encrypted signing_secret at rest (FAR-1732)
# ---------------------------------------------------------------------------


def _fernet_token(secret: str, key: str | None = None) -> str:
    import base64

    from cryptography.fernet import Fernet

    key = key or base64.urlsafe_b64encode(b"s" * 32).decode()
    return Fernet(key.encode()).encrypt(secret.encode()).decode()


def _fake_settings_with_key(fernet_key: str) -> MagicMock:
    settings = MagicMock()
    settings.fernet_key = fernet_key
    return settings


async def test_verify_slack_delivery_encrypted_signing_secret_verifies() -> None:
    """FAR-1732/1735: a Fernet-token signing_secret decrypts via the shared
    bounded resolver with the REAL decode helper (no decode mock — the
    patch target is ``modulo.api.routes.slack.get_settings``, the name the
    route actually binds, so the fail-closed/decrypt plumbing is genuinely
    exercised) and the signature check runs against the DECRYPTED secret —
    never against the raw ciphertext blob."""
    import base64

    from modulo.api.routes.slack import _verify_slack_delivery

    valid_key = base64.urlsafe_b64encode(b"s" * 32).decode()
    token = _fernet_token(_SECRET, valid_key)
    trigger = MagicMock()
    trigger.pipeline_id = uuid.uuid4()
    trigger.config_json = {"signing_secret": token}
    raw_body = _event_body()
    ts = str(int(time.time()))

    with (
        patch(
            "modulo.api.routes.slack.load_trigger_and_org_global",
            new_callable=AsyncMock,
            return_value=(trigger, _ORG_ID),
        ),
        patch("modulo.api.routes.slack.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.set_rls_execution_context", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.get_settings", return_value=_fake_settings_with_key(valid_key)),
    ):
        result_trigger, org_id = await _verify_slack_delivery(
            # ``decode_stored_secret_scoped`` re-applies the RLS org on THIS
            # session via the REAL ``set_rls_org``, whose ``in_transaction()`` /
            # ``get_bind()`` reads are SYNC — a bare AsyncMock orphans a
            # never-awaited coroutine per call.
            session=configure_rls_preamble(AsyncMock()),
            system_session=AsyncMock(),
            request=MagicMock(),
            trigger_id=_TRIGGER_ID,
            principal=None,
            raw_body=raw_body,
            slack_signature=_slack_sig(raw_body, _SECRET, ts),
            slack_timestamp=ts,
        )

    assert result_trigger is trigger
    assert org_id == _ORG_ID


async def test_verify_slack_delivery_undecryptable_token_fails_closed() -> None:
    """FAR-1732/1735: a stored value shaped like a Fernet token that CANNOT be
    decrypted under the configured key must fail closed (401) — never fall
    back to comparing Slack's signature against ciphertext-as-secret. The
    REAL decode helper raises ``DecryptionError`` here (no decode mock)."""
    import base64

    from modulo.api.routes.slack import _verify_slack_delivery

    other_key = base64.urlsafe_b64encode(b"x" * 32).decode()
    configured_key = base64.urlsafe_b64encode(b"s" * 32).decode()
    token = _fernet_token(_SECRET, other_key)
    trigger = MagicMock()
    trigger.config_json = {"signing_secret": token}
    with (
        patch(
            "modulo.api.routes.slack.load_trigger_and_org_global",
            new_callable=AsyncMock,
            return_value=(trigger, _ORG_ID),
        ),
        patch("modulo.api.routes.slack.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.set_rls_execution_context", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.get_settings", return_value=_fake_settings_with_key(configured_key)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _verify_slack_delivery(
            # See the encrypted-secret test: the real RLS preamble needs a
            # contract-correct (sync ``in_transaction``/``get_bind``) session.
            session=configure_rls_preamble(AsyncMock()),
            system_session=AsyncMock(),
            request=MagicMock(),
            trigger_id=_TRIGGER_ID,
            principal=None,
            raw_body=b"{}",
            slack_signature="v0=whatever",
            slack_timestamp="1",
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Slack signature verification failed"


async def test_verify_slack_delivery_legacy_plaintext_accepted_with_dated_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1735 bounded shim: a NON-token stored secret is legacy pre-0306
    plaintext — accepted so pre-migration deployments keep delivering, but
    ONLY with a loud dated warning (``trigger_secret.legacy_plaintext``), so
    a future plaintext write is surfaced in the logs, never silently
    accepted. The REAL decode helper is used (no decode mock): a plaintext
    value is returned unchanged by the resolver, it does NOT raise."""
    import logging as _logging

    from modulo.api.routes.slack import _verify_slack_delivery

    trigger = MagicMock()
    trigger.pipeline_id = uuid.uuid4()
    trigger.config_json = {"signing_secret": _SECRET}
    raw_body = _event_body()
    ts = str(int(time.time()))

    with (
        patch(
            "modulo.api.routes.slack.load_trigger_and_org_global",
            new_callable=AsyncMock,
            return_value=(trigger, _ORG_ID),
        ),
        patch("modulo.api.routes.slack.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.slack.set_rls_execution_context", new_callable=AsyncMock),
        caplog.at_level(_logging.WARNING, logger="modulo.core.trigger_secrets"),
    ):
        result_trigger, org_id = await _verify_slack_delivery(
            # ``decode_stored_secret_scoped`` re-applies the RLS org on THIS
            # session via the REAL ``set_rls_org``, whose ``in_transaction()`` /
            # ``get_bind()`` reads are SYNC — a bare AsyncMock orphans a
            # never-awaited coroutine per call.
            session=configure_rls_preamble(AsyncMock()),
            system_session=AsyncMock(),
            request=MagicMock(),
            trigger_id=_TRIGGER_ID,
            principal=None,
            raw_body=raw_body,
            slack_signature=_slack_sig(raw_body, _SECRET, ts),
            slack_timestamp=ts,
        )

    assert result_trigger is trigger
    assert org_id == _ORG_ID
    assert "trigger_secret.legacy_plaintext" in caplog.text
    assert "signing_secret" in caplog.text
