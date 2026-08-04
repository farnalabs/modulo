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

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, create_access_token
from modulo.settings import Settings, get_settings

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


@pytest.fixture()
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
    shared secret cannot authenticate an unauthenticated replay.
    """
    mock_session = _make_mock_session(trigger_config=None)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    try:
        event_id = uuid.uuid4()
        resp = client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook/replay/{event_id}",
            headers=_hmac_headers(),
        )
        assert resp.status_code == 401
        assert "HMAC" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_db_session, None)
