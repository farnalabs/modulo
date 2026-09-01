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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_system_db_session
from modulo.api.main import app
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_SECRET = "my-slack-signing-secret"
_CHALLENGE = "3eZbrw1aBm2rZgRNFdxV2598559m"


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


def _make_trigger_session() -> AsyncMock:
    """Session whose trigger carries a signing_secret so route-level signature
    validation runs against the real secret."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

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
    return session


def _make_system_session(*, trigger_found: bool = True) -> AsyncMock:
    """System-session mock for the bootstrap trigger/org resolution.

    The bootstrap trigger read (which carries the signing secret) runs on the
    system session BEFORE any app-session RLS org context exists (FAR-523).
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    trigger_result = MagicMock()
    if trigger_found:
        trigger_mock = MagicMock()
        trigger_mock.pipeline_id = uuid.uuid4()
        trigger_mock.active = True
        trigger_mock.trigger_type = "slack_app_mention"
        trigger_mock.config_json = {"signing_secret": _SECRET}
        trigger_result.scalar_one_or_none.return_value = trigger_mock
    else:
        trigger_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=trigger_result)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_trigger_session()
    mock_system_session = _make_system_session()

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

    with (
        patch("modulo.api.routes.slack.handle_app_mention", new_callable=AsyncMock) as m,
        patch(
            "modulo.api.routes.slack.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
        patch("modulo.api.routes.slack.set_rls_org"),
        patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
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
    m.assert_awaited_once()


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
        patch("modulo.api.routes.slack._org_row_exists", new_callable=AsyncMock, return_value=True),
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
    system_session = _make_system_session(trigger_found=False)

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
        patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
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
