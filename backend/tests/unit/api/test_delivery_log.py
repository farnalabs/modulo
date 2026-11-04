"""Unit tests for delivery log endpoints.

Tests the per-webhook and global delivery log list endpoints,
filtering, pagination, and manual retry.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_WEBHOOK_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_DELIVERY_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 44,
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
    ep.created_by = _USER_ID
    ep.created_at = overrides.get("created_at", datetime.now(UTC))
    ep.disabled_at = overrides.get("disabled_at")
    return ep


def _make_mock_delivery(**overrides: object) -> MagicMock:
    dl = MagicMock()
    dl.id = overrides.get("id", _DELIVERY_ID)
    dl.organisation_id = _ORG_ID
    dl.endpoint_id = overrides.get("endpoint_id", _WEBHOOK_ID)
    dl.event_type = overrides.get("event_type", "hitl_awaiting")
    dl.status = overrides.get("status", "delivered")
    dl.attempt_count = overrides.get("attempt_count", 1)
    dl.response_code = overrides.get("response_code", 200)
    dl.last_error = overrides.get("last_error")
    dl.response_body = overrides.get("response_body")
    dl.created_at = overrides.get("created_at", datetime.now(UTC))
    return dl


def _make_mock_session() -> AsyncMock:
    # Contract-correct double: the permission dependency's authz-enforce read
    # calls ``result.scalar_one_or_none()`` (SYNC on a real Result), and the RLS
    # preamble reads ``in_transaction()``/``get_bind()``/``add()`` synchronously.
    # A bare AsyncMock turns every one of those into a never-awaited coroutine.
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
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


# ── Per-webhook delivery list ──────────────────────────────────────


def test_list_deliveries_empty(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        ep = _make_mock_endpoint()
        session.get = AsyncMock(return_value=ep)

        list_result = MagicMock()
        list_result.scalars.return_value = []
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        executed_queries: list[str] = []

        def execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            sql = str(args[0] if args else kwargs.get("query", ""))
            executed_queries.append(sql)
            if "count(" in sql.lower():
                return count_result
            return list_result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    body = resp.json()
    assert not body["items"]
    assert body["next_cursor"] is None
    assert body["total"] == 0


def test_list_deliveries_returns_logs(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        ep = _make_mock_endpoint()
        session.get = AsyncMock(return_value=ep)

        dl = _make_mock_delivery(
            status="delivered",
            response_code=200,
        )
        list_result = MagicMock()
        list_result.scalars.return_value = [dl]
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        def execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            sql = str(args[0] if args else kwargs.get("query", ""))
            if "count(" in sql.lower():
                return count_result
            return list_result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == str(_DELIVERY_ID)
    assert item["status"] == "delivered"
    assert item["response_code"] == 200
    assert item["endpoint_url"] == "https://hooks.example.com/notify"


def test_list_deliveries_status_filter(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        ep = _make_mock_endpoint()
        session.get = AsyncMock(return_value=ep)

        list_result = MagicMock()
        list_result.scalars.return_value = []
        count_result = MagicMock()
        count_result.scalar.return_value = 0

        def execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            sql = str(args[0] if args else kwargs.get("query", ""))
            if "count(" in sql.lower():
                return count_result
            return list_result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries?status=failed")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200


def test_list_deliveries_not_found(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/admin/notifications/{uuid.uuid4()}/deliveries")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


# ── Global delivery list ───────────────────────────────────────────


def test_list_all_deliveries_global(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        dl = _make_mock_delivery(status="failed", response_code=500, last_error="Server error")

        list_result = MagicMock()
        list_result.all.return_value = [(dl, "https://hooks.example.com/notify")]
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        def execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            sql = str(args[0] if args else kwargs.get("query", ""))
            if "count(" in sql.lower():
                return count_result
            return list_result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/admin/notifications/deliveries")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["status"] == "failed"
    assert item["endpoint_url"] == "https://hooks.example.com/notify"
    assert item["last_error"] == "Server error"


def test_list_all_deliveries_date_filter(client: TestClient) -> None:
    """A bare ``to`` date must be inclusive of the whole day.

    The bound is an EXCLUSIVE next-day timestamp (``created_at < date + 1 day``),
    not ``created_at <= midnight``. This assertion fails without the end-of-day
    fix: the old predicate compiled to ``created_at <= 2026-09-30 00:00:00+00:00``
    and therefore excluded every delivery recorded during 2026-09-30 itself.
    Both the list and the count path must apply the same bounds.
    """
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()

        list_result = MagicMock()
        list_result.all.return_value = []
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        delivery_queries: list[str] = []

        def execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            statement = args[0] if args else kwargs.get("query", "")
            sql = str(statement)
            if "notification_delivery_log" in sql.lower():
                delivery_queries.append(
                    str(
                        statement.compile(  # type: ignore[union-attr]
                            dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True},
                        )
                    )
                )
            if "count(" in sql.lower():
                return count_result
            return list_result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/admin/notifications/deliveries?from=2026-09-01&to=2026-09-30")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    # One list query and one count query, each carrying both date bounds.
    assert len(delivery_queries) == 2
    for sql in delivery_queries:
        assert "created_at >= '2026-09-01" in sql
        # The upper bound is exclusive and lands on the NEXT day.
        upper_clause = sql.split("created_at <", 1)[1]
        assert "2026-10-01" in upper_clause
        assert "created_at <= " not in sql


def test_parse_delivery_date_normalises_naive_values_to_utc() -> None:
    """An offset-less date/datetime is UTC, never host-local time."""
    from modulo.api.routes.admin_notifications import _parse_delivery_date

    assert _parse_delivery_date("2026-09-30", "to date") == datetime(2026, 9, 30, tzinfo=UTC)
    assert _parse_delivery_date("2026-09-30T12:00:00", "to date") == datetime(2026, 9, 30, 12, tzinfo=UTC)
    # An explicit offset is preserved rather than overwritten.
    assert _parse_delivery_date("2026-09-30T12:00:00+02:00", "to date").utcoffset() == timedelta(hours=2)


def test_parse_delivery_upper_bound_treats_bare_date_as_end_of_day() -> None:
    """A bare ``to`` date becomes an exclusive next-day bound."""
    from modulo.api.routes.admin_notifications import _parse_delivery_upper_bound

    bound, inclusive = _parse_delivery_upper_bound("2026-09-30", "to date")
    assert bound == datetime(2026, 10, 1, tzinfo=UTC)
    assert inclusive is False

    # An explicit timestamp keeps its exact instant and stays inclusive.
    bound, inclusive = _parse_delivery_upper_bound("2026-09-30T12:00:00", "to date")
    assert bound == datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert inclusive is True


def test_list_all_deliveries_bad_date_returns_422(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/admin/notifications/deliveries?from=not-a-date")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 422


# ── Manual retry ───────────────────────────────────────────────────


def test_retry_delivery_not_found(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/admin/notifications/{uuid.uuid4()}/deliveries/{uuid.uuid4()}/retry",
            json={},
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


def test_retry_delivery_delivery_not_found(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        session = _make_mock_session()
        ep = _make_mock_endpoint()
        session.get = AsyncMock()
        session.get.side_effect = [ep, None]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{uuid.uuid4()}/retry",
            json={},
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


# ── Auth guard ─────────────────────────────────────────────────────


def test_delivery_log_requires_auth(client: TestClient) -> None:
    client.app.dependency_overrides.pop(get_current_user, None)
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    assert resp.status_code in (401, 403)


def test_delivery_log_requires_admin_role(client: TestClient) -> None:
    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    assert resp.status_code == 403


def test_run_stalled_is_available_event() -> None:
    """run_stalled must be subscribable for webhooks, like run_failed (FAR-98)."""
    from modulo.api.routes.admin_notifications import AVAILABLE_EVENTS

    assert "run_stalled" in AVAILABLE_EVENTS


def test_webhook_create_accepts_run_stalled() -> None:
    """An org can subscribe a webhook to run_stalled without a 422."""
    from modulo.api.routes.admin_notifications import WebhookCreate

    # The model's URL validator resolves DNS; this test is about the event
    # vocabulary, so stub the SSRF floor for its example host.
    with patch("modulo.api.routes.admin_notifications.validate_outbound_url"):
        model = WebhookCreate(
            url="https://hooks.example.com/notify",
            events=["run_stalled"],
        )
    assert model.events == ["run_stalled"]
