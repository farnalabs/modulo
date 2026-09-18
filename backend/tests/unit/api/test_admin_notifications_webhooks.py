"""Unit tests for admin notification webhook team scoping.

Covers the admin API surfacing of ``team_id`` on notification endpoints:
admins can create/update team-scoped webhooks, and unknown team references
are rejected with 422 instead of falling through to an FK ``IntegrityError``
(which previously surfaced as a 503).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_WEBHOOK_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
_FERNET_KEY = Fernet.generate_key().decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_endpoint(**overrides: object) -> MagicMock:
    ep = MagicMock()
    ep.id = overrides.get("id", _WEBHOOK_ID)
    ep.organisation_id = _ORG_ID
    ep.url = overrides.get("url", "https://hooks.example.com/notify")
    ep.secret_ciphertext = overrides.get("secret_ciphertext")
    ep.events = overrides.get("events", '["hitl_awaiting"]')
    ep.description = overrides.get("description")
    ep.auto_disabled = overrides.get("auto_disabled", False)
    ep.consecutive_dead_letter_count = overrides.get("consecutive_dead_letter_count", 0)
    ep.team_id = overrides.get("team_id")
    ep.disabled_at = overrides.get("disabled_at")
    ep.created_at = overrides.get("created_at", datetime.now(UTC))
    return ep


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _make_session_with_team(team_present: bool) -> AsyncMock:
    """Execute returns a truthy/falsy row for the kill-switch + team lookups."""
    session = _make_mock_session()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=MagicMock() if team_present else None)
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── POST /api/v1/admin/notifications ──────────────────────────────────


def test_create_webhook_with_team_id_returns_201(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=True)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.post(
                "/api/v1/admin/notifications",
                json={
                    "url": "https://hooks.example.com/team-alert",
                    "events": ["hitl_awaiting"],
                    "team_id": str(_TEAM_ID),
                },
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 201
    data = resp.json()
    assert data["team_id"] == str(_TEAM_ID)
    persisted_ep = session.add.call_args.args[0]
    assert persisted_ep.team_id == _TEAM_ID


def test_create_webhook_without_team_id_is_org_wide(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=True)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.post(
                "/api/v1/admin/notifications",
                json={"url": "https://hooks.example.com/org-alert"},
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 201
    assert resp.json()["team_id"] is None
    persisted_ep = session.add.call_args.args[0]
    assert persisted_ep.team_id is None


def test_create_webhook_with_unknown_team_returns_422(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=False)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.post(
                "/api/v1/admin/notifications",
                json={
                    "url": "https://hooks.example.com/team-alert",
                    "team_id": str(_TEAM_ID),
                },
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 422
    assert f"Unknown team id: {_TEAM_ID}" in resp.json()["detail"]
    assert session.add.call_args is None


def test_create_webhook_with_invalid_team_uuid_returns_422(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=True)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.post(
                "/api/v1/admin/notifications",
                json={
                    "url": "https://hooks.example.com/team-alert",
                    "team_id": "not-a-uuid",
                },
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 422


# ── PUT /api/v1/admin/notifications/{webhook_id} ──────────────────────


def test_update_webhook_reassigns_team_id(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=True)
        ep = _make_mock_endpoint(team_id=None)
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.put(
                f"/api/v1/admin/notifications/{_WEBHOOK_ID}",
                json={"team_id": str(_TEAM_ID)},
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    assert ep.team_id == _TEAM_ID
    assert resp.json()["team_id"] == str(_TEAM_ID)


def test_update_webhook_unknown_team_returns_422(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=False)
        ep = _make_mock_endpoint(team_id=None)
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.put(
                f"/api/v1/admin/notifications/{_WEBHOOK_ID}",
                json={"team_id": str(_TEAM_ID)},
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 422
    assert ep.team_id is None


def test_update_webhook_omitting_team_id_keeps_existing_scope(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_session_with_team(team_present=True)
        ep = _make_mock_endpoint(team_id=_TEAM_ID)
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.put(
                f"/api/v1/admin/notifications/{_WEBHOOK_ID}",
                json={"description": "renamed"},
            )
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    assert ep.team_id == _TEAM_ID
    assert resp.json()["team_id"] == str(_TEAM_ID)


# ── GET /api/v1/admin/notifications ───────────────────────────────────


def test_list_webhooks_echoes_team_id(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        result = MagicMock()
        result.scalars.return_value = [_make_mock_endpoint(team_id=_TEAM_ID)]
        session.execute = AsyncMock(return_value=result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.get("/api/v1/admin/notifications")
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["team_id"] == str(_TEAM_ID)


def test_get_webhook_echoes_team_id(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=_make_mock_endpoint(team_id=_TEAM_ID))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
        finally:
            client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    assert resp.json()["team_id"] == str(_TEAM_ID)


# ── Retry path SSRF fail-closed (FAR-517) ───────────────────────────────


async def test_retry_one_delivery_fails_closed_on_ssrf_rebind() -> None:
    """FAR-517: the manual/bulk retry POST must pin its client through
    pinned_async_client, so a saved webhook URL whose host re-resolves to a
    blocked internal address (169.254.169.254) fails closed — recorded as an
    error with no request issued — rather than POSTing to the unvalidated host."""
    from modulo.api.routes.admin_notifications import _retry_one_delivery

    ep = _make_mock_endpoint()
    delivery = MagicMock()
    delivery.event_type = "hitl_awaiting"
    delivery.endpoint_id = _WEBHOOK_ID
    delivery.attempt_count = 2

    async def _fake_pinned(_url: str) -> httpx.AsyncClient:
        raise ValueError(
            "URL hostname hooks.example.com resolves to a private/internal "
            "address (169.254.169.254). Add its CIDR to SSRF_ALLOW_PRIVATE_RANGES "
            "to allow this target, or use a public URL."
        )

    session = _make_mock_session()
    principal = TenantPrincipal(username="admin", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")

    with (
        patch("modulo.api.routes.admin_notifications.pinned_async_client", new=_fake_pinned),
        patch("modulo.api.routes.admin_notifications._record_delivery_error", new=AsyncMock()) as mock_record,
    ):
        resp, error = await _retry_one_delivery(session, principal, _make_settings(), delivery, ep)

    assert resp is None
    assert "169.254.169.254" in (error or "")
    mock_record.assert_awaited_once()


# ── Test-webhook path SSRF fail-closed (FAR-517) ────────────────────────


async def test_test_webhook_fails_closed_on_ssrf_rebind() -> None:
    """FAR-517: ``POST /{webhook_id}/test`` must pin its client through
    pinned_async_client, so a saved webhook URL whose host re-resolves to a
    blocked internal address (169.254.169.254) fails closed — no request is
    issued and no response body is echoed back to the caller — rather than
    POSTing to the unvalidated host with a plain client (validate-at-save-only
    leaves a DNS-rebinding window, and the route returns up to 500 chars of the
    response body, which would make it a readable SSRF primitive)."""
    from modulo.api.routes.admin_notifications import test_webhook

    ep = _make_mock_endpoint()
    session = _make_mock_session()
    session.get = AsyncMock(return_value=ep)
    principal = TenantPrincipal(username="admin", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")

    async def _fake_pinned(_url: str) -> httpx.AsyncClient:
        raise ValueError(
            "URL hostname hooks.example.com resolves to a private/internal "
            "address (169.254.169.254). Add its CIDR to SSRF_ALLOW_PRIVATE_RANGES "
            "to allow this target, or use a public URL."
        )

    unpinned_post = AsyncMock()

    with (
        patch("modulo.api.routes.admin_notifications.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.admin_notifications.pinned_async_client", new=_fake_pinned),
        patch.object(httpx.AsyncClient, "post", new=unpinned_post),
    ):
        result = await test_webhook(_WEBHOOK_ID, session=session, principal=principal, settings=_make_settings())

    assert result.success is False
    assert result.status_code is None
    assert result.response_body is None
    assert "169.254.169.254" in (result.error or "")
    unpinned_post.assert_not_awaited()


async def test_test_webhook_posts_through_pinned_client() -> None:
    """FAR-517 happy path: the test POST goes out on the pinned client (not a
    plain ``httpx.AsyncClient``), the pinned client is closed afterwards, and
    the endpoint's response is surfaced to the caller."""
    from modulo.api.routes.admin_notifications import test_webhook

    ep = _make_mock_endpoint()
    session = _make_mock_session()
    session.get = AsyncMock(return_value=ep)
    principal = TenantPrincipal(username="admin", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")

    pinned = MagicMock()
    pinned.post = AsyncMock(return_value=httpx.Response(status_code=200, text="pong"))
    pinned.aclose = AsyncMock()

    async def _fake_pinned(_url: str) -> MagicMock:
        return pinned

    with (
        patch("modulo.api.routes.admin_notifications.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.admin_notifications.pinned_async_client", new=_fake_pinned),
    ):
        result = await test_webhook(_WEBHOOK_ID, session=session, principal=principal, settings=_make_settings())

    assert result.success is True
    assert result.status_code == 200
    assert result.response_body == "pong"
    assert result.error is None
    pinned.post.assert_awaited_once()
    assert pinned.post.await_args.args[0] == ep.url
    pinned.aclose.assert_awaited_once()
