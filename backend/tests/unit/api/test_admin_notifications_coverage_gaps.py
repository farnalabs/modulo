"""Targeted coverage tests for ``modulo.api.routes.admin_notifications`` (FAR-573).

Complements test_admin_notifications_webhooks.py (team scoping) by exercising
every webhook CRUD endpoint, the delivery-log listers (filters + cursor
pagination), the test/retry flows (SSRF rejection, signing, transport
failure, dead-letter bump), and the repo's DB error mapping convention
(ProgrammingError -> 501, SQLAlchemyError -> 503, HTTPException passthrough,
Exception -> 500).

Unit tier: sessions are AsyncMock doubles, HTTP and crypto seams are patched,
payloads use the module's real request models.
"""

import hashlib
import hmac
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

import modulo.api.routes.admin_notifications as admin_notifications
from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_WEBHOOK_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_DELIVERY_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_FERNET_KEY = Fernet.generate_key().decode()

_DB_ERROR_PARAMS = [
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _result(
    *,
    scalar: int = 0,
    scalar_one: int = 0,
    scalar_one_or_none: object = None,
    rows: list | None = None,
    scalars: list | None = None,
) -> MagicMock:
    r = MagicMock()
    r.scalar = MagicMock(return_value=scalar)
    r.scalar_one = MagicMock(return_value=scalar_one)
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.all = MagicMock(return_value=rows if rows is not None else [])
    sc = MagicMock()
    sc.all = MagicMock(return_value=scalars if scalars is not None else [])
    sc.__iter__.return_value = iter(scalars if scalars is not None else [])
    r.scalars = MagicMock(return_value=sc)
    r.first = MagicMock(return_value=None)
    return r


def _make_session(results: list[MagicMock] | None = None) -> AsyncMock:
    """RLS-capable session; ``set_rls_org`` uses the sqlite info-dict path."""
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.refresh = AsyncMock()
    session.get = AsyncMock(return_value=None)
    if results is None:
        session.execute = AsyncMock(return_value=_result())
    else:
        queue = list(results)

        async def _execute(*_a: object, **_kw: object) -> MagicMock:
            return queue.pop(0) if len(queue) > 1 else queue[0]

        session.execute = AsyncMock(side_effect=_execute)
    return session


def _endpoint(**overrides: object) -> MagicMock:
    ep = MagicMock()
    ep.id = overrides.get("id", _WEBHOOK_ID)
    ep.organisation_id = overrides.get("organisation_id", _ORG_ID)
    ep.url = overrides.get("url", "https://hooks.example.com/notify")
    ep.secret_ciphertext = overrides.get("secret_ciphertext")
    ep.events = overrides.get("events", '["hitl_awaiting"]')
    ep.description = overrides.get("description", "hook")
    ep.auto_disabled = overrides.get("auto_disabled", False)
    ep.consecutive_dead_letter_count = overrides.get("consecutive_dead_letter_count", 0)
    ep.team_id = overrides.get("team_id")
    ep.disabled_at = overrides.get("disabled_at")
    ep.created_at = overrides.get("created_at", datetime.now(UTC))
    return ep


def _delivery(**overrides: object) -> MagicMock:
    d = MagicMock()
    d.id = overrides.get("id", _DELIVERY_ID)
    d.endpoint_id = overrides.get("endpoint_id", _WEBHOOK_ID)
    d.organisation_id = _ORG_ID
    d.event_type = overrides.get("event_type", "run_failed")
    d.status = overrides.get("status", "failed")
    d.attempt_count = overrides.get("attempt_count", 1)
    d.response_code = overrides.get("response_code", 500)
    d.last_error = overrides.get("last_error", "boom")
    d.response_body = overrides.get("response_body")
    d.created_at = overrides.get("created_at", datetime(2025, 6, 1, tzinfo=UTC))
    return d


def _http_response(status_code: int = 200, text: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = status_code < 400
    resp.text = text
    return resp


def _http_client(post_side_effect: object = None) -> MagicMock:
    client = MagicMock()
    if post_side_effect is None:
        client.post = AsyncMock(return_value=_http_response())
    else:
        client.post = AsyncMock(side_effect=post_side_effect)
    client.aclose = AsyncMock()
    client.timeout = None
    return client


def _build_client(session: AsyncMock, role: str = "admin") -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    return TestClient(app)


@pytest.fixture
def api() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    client = _build_client(session, role="admin")
    yield client, session
    app.dependency_overrides.clear()


# --- GET "" (list webhooks) ---


def test_list_webhooks_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    results = [_result(), _result(scalars=[_endpoint()])]  # 1st: authz kill-switch read

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if len(results) > 1 else results[0]

    session.execute = AsyncMock(side_effect=_execute)
    resp = client.get("/api/v1/admin/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["url"] == "https://hooks.example.com/notify"
    assert data[0]["has_secret"] is False


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_list_webhooks_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.get("/api/v1/admin/notifications")
    assert resp.status_code == expected


def test_list_webhooks_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.get("/api/v1/admin/notifications")
    assert resp.status_code == 500


# --- POST "" (create webhook) + validators ---


def test_create_webhook_with_secret(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    resp = client.post(
        "/api/v1/admin/notifications",
        json={"url": "https://hooks.example.com/new", "secret": "s3cret", "events": ["hitl_awaiting"]},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["has_secret"] is True
    persisted = session.add.call_args.args[0]
    assert persisted.secret_ciphertext is not None


def test_create_webhook_unknown_team_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = client.post(
        "/api/v1/admin/notifications",
        json={
            "url": "https://hooks.example.com/new",
            "events": ["hitl_awaiting"],
            "team_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422


def test_create_webhook_known_team_201(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=MagicMock()))
    resp = client.post(
        "/api/v1/admin/notifications",
        json={
            "url": "https://hooks.example.com/new",
            "events": ["hitl_awaiting"],
            "team_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 201


def test_create_webhook_invalid_url_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.post("/api/v1/admin/notifications", json={"url": "ftp://not-http", "events": []})
    assert resp.status_code == 422


def test_create_webhook_invalid_event_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.post("/api/v1/admin/notifications", json={"url": "https://x.example.com", "events": ["bogus"]})
    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_create_webhook_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    # NOTE: the authz kill-switch read (require_permission) swallows a raised
    # --- GET/PUT/DELETE /{webhook_id} ---
    # --- POST /{webhook_id}/test ---
    session.flush = AsyncMock(side_effect=exc)
    resp = client.post("/api/v1/admin/notifications", json={"url": "https://x.example.com", "events": []})
    assert resp.status_code == expected


# --- POST /{webhook_id}/re-enable ---


def test_get_webhook_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(_WEBHOOK_ID)


def test_get_webhook_not_found_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=None)
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 404


def test_get_webhook_cross_org_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint(organisation_id=uuid.uuid4()))
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_get_webhook_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=exc)
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == expected


def test_get_webhook_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 500


def test_update_webhook_all_fields(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    ep = _endpoint()
    session.get = AsyncMock(return_value=ep)
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=MagicMock()))
    resp = client.put(
        f"/api/v1/admin/notifications/{_WEBHOOK_ID}",
        json={
            "url": "https://hooks.example.com/updated",
            "secret": "new-secret",
            "events": ["run_failed"],
            "description": "updated",
            "team_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 200
    assert ep.url == "https://hooks.example.com/updated"
    assert ep.secret_ciphertext is not None
    assert ep.events == ["run_failed"]


def test_update_webhook_unknown_team_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = client.put(
        f"/api/v1/admin/notifications/{_WEBHOOK_ID}",
        json={"team_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_update_webhook_invalid_url_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.put(f"/api/v1/admin/notifications/{_WEBHOOK_ID}", json={"url": "nope"})
    assert resp.status_code == 422


def test_update_webhook_invalid_event_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.put(f"/api/v1/admin/notifications/{_WEBHOOK_ID}", json={"events": ["bogus"]})
    assert resp.status_code == 422


def test_update_webhook_not_found_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=None)
    resp = client.put(f"/api/v1/admin/notifications/{_WEBHOOK_ID}", json={"description": "x"})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_update_webhook_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=exc)
    resp = client.put(f"/api/v1/admin/notifications/{_WEBHOOK_ID}", json={"description": "x"})
    assert resp.status_code == expected


def test_update_webhook_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.put(f"/api/v1/admin/notifications/{_WEBHOOK_ID}", json={"description": "x"})
    assert resp.status_code == 500


def test_delete_webhook_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    resp = client.delete(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 204


def test_delete_webhook_not_found_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=None)
    resp = client.delete(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_delete_webhook_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=exc)
    resp = client.delete(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == expected


def test_delete_webhook_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.delete(f"/api/v1/admin/notifications/{_WEBHOOK_ID}")
    assert resp.status_code == 500


# --- GET /{webhook_id}/deliveries ---


def test_test_webhook_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    client_http = _http_client()
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=client_http))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["status_code"] == 200


def test_test_webhook_endpoint_error_response(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    client_http = _http_client()
    client_http.post = AsyncMock(return_value=_http_response(status_code=500, text="kaboom"))
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=client_http))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 500


def test_test_webhook_ssrf_rejected(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(side_effect=ValueError("blocked host")))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "blocked host" in data["error"]


def test_test_webhook_transport_error(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    client_http = _http_client(post_side_effect=httpx.RequestError("conn refused"))
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=client_http))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] is None


def test_test_webhook_signs_payload_with_secret(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session = api
    ep = _endpoint(secret_ciphertext=b"cipher")
    session.get = AsyncMock(return_value=ep)
    monkeypatch.setattr(admin_notifications, "decode_stored_secret_scoped", AsyncMock(return_value="raw-secret"))
    http_client = _http_client()
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=http_client))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 200
    sent_headers = http_client.post.call_args.kwargs["headers"]
    expected_sig = hmac.new(b"raw-secret", http_client.post.call_args.kwargs["content"], hashlib.sha256).hexdigest()
    assert sent_headers["X-Modulo-Signature"] == f"sha256={expected_sig}"


def test_test_webhook_sign_failure_still_posts(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session = api
    ep = _endpoint(secret_ciphertext=b"cipher")
    session.get = AsyncMock(return_value=ep)
    monkeypatch.setattr(
        admin_notifications, "decode_stored_secret_scoped", AsyncMock(side_effect=RuntimeError("decode failed"))
    )
    http_client = _http_client()
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=http_client))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 200
    sent_headers = http_client.post.call_args.kwargs["headers"]
    assert "X-Modulo-Signature" not in sent_headers


def test_test_webhook_not_found_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=None)
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_test_webhook_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=exc)
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/test")
    assert resp.status_code == expected


# --- POST /{webhook_id}/deliveries/{delivery_id}/retry ---


def test_re_enable_webhook_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    ep = _endpoint(auto_disabled=True, consecutive_dead_letter_count=12)
    session.get = AsyncMock(return_value=ep)
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/re-enable")
    assert resp.status_code == 200
    assert ep.auto_disabled is False
    assert ep.consecutive_dead_letter_count == 0
    assert ep.disabled_at is None


def test_re_enable_webhook_not_found_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=None)
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/re-enable")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_re_enable_webhook_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=exc)
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/re-enable")
    assert resp.status_code == expected


def test_re_enable_webhook_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/re-enable")
    assert resp.status_code == 500


# --- GET /deliveries (org-wide log) + retry-all ---


def test_list_deliveries_success(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    rows = [_delivery(), _delivery()]
    results = [_result(), _result(scalars=rows), _result(scalar=2)]  # 1st: kill-switch read

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if len(results) > 1 else results[0]

    session.execute = AsyncMock(side_effect=_execute)
    session.get = AsyncMock(return_value=_endpoint())
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["next_cursor"] is None


def test_list_deliveries_cursor_pagination(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    rows = [_delivery(), _delivery(), _delivery()]
    results = [_result(), _result(scalars=rows), _result(scalar=3)]  # 1st: kill-switch read

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if len(results) > 1 else results[0]

    session.execute = AsyncMock(side_effect=_execute)
    session.get = AsyncMock(return_value=_endpoint())
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries", params={"limit": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] == "2025-06-01T00:00:00+00:00"


def test_list_deliveries_invalid_cursor_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    session.execute = AsyncMock(return_value=_result())
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries", params={"cursor": "not-a-date"})
    assert resp.status_code == 422


def test_list_deliveries_webhook_missing_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(return_value=None)
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_list_deliveries_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(return_value=_endpoint())
    session.execute = AsyncMock(side_effect=exc)
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
    assert resp.status_code == expected


def test_list_deliveries_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.get(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries")
    assert resp.status_code == 500


# --- GET /available-events ---


def test_retry_delivery_success(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[_endpoint(), _delivery()])
    http_client = _http_client()
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=http_client))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True


def test_retry_delivery_endpoint_failure_recorded(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[_endpoint(), _delivery()])
    http_client = _http_client()
    http_client.post = AsyncMock(return_value=_http_response(status_code=500, text="nope"))
    results = [_result(scalar_one=3)]
    session.execute = AsyncMock(side_effect=results[0])

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return _result(scalar_one=3)

    session.execute = AsyncMock(side_effect=_execute)
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=http_client))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 500


def test_retry_delivery_dead_letter_threshold_disables(
    api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[_endpoint(), _delivery()])
    http_client = _http_client()
    http_client.post = AsyncMock(return_value=_http_response(status_code=500, text="nope"))
    session.execute = AsyncMock(return_value=_result(scalar_one=10))
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=http_client))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 200
    # 1: authz kill-switch read, 2: dead-letter bump, 3: auto-disable update.
    assert session.execute.await_count == 3


def test_retry_delivery_transport_error(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[_endpoint(), _delivery()])
    http_client = _http_client(post_side_effect=httpx.RequestError("conn refused"))
    session.execute = AsyncMock(return_value=_result(scalar_one=3))
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(return_value=http_client))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "conn refused" in data["error"]


def test_retry_delivery_ssrf_rejected(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[_endpoint(), _delivery()])
    session.execute = AsyncMock(return_value=_result(scalar_one=3))
    monkeypatch.setattr(admin_notifications, "pinned_async_client", AsyncMock(side_effect=ValueError("blocked")))
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "blocked" in data["error"]


def test_retry_delivery_webhook_missing_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[None])
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 404


def test_retry_delivery_log_missing_404(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[_endpoint(), None])
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Delivery log not found"


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_retry_delivery_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[exc])
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == expected


def test_retry_delivery_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.get = AsyncMock(side_effect=[RuntimeError("boom")])
    resp = client.post(f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry")
    assert resp.status_code == 500


# ---


def test_list_all_deliveries_with_filters_and_cursor(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    rows = [(_delivery(), "https://ep1"), (_delivery(), None), (_delivery(), "https://ep2")]
    results = [_result(), _result(rows=rows), _result(scalar=5)]  # 1st: kill-switch read

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if len(results) > 1 else results[0]

    session.execute = AsyncMock(side_effect=_execute)
    resp = client.get(
        "/api/v1/admin/notifications/deliveries",
        params={
            "limit": 2,
            "status": "failed",
            "event_type": "run_failed",
            "endpoint_id": str(_WEBHOOK_ID),
            "from": "2025-01-01T00:00:00+00:00",
            "to": "2025-12-31T00:00:00+00:00",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["next_cursor"] == "2025-06-01T00:00:00+00:00"
    assert data["items"][0]["endpoint_url"] == "https://ep1"
    assert not data["items"][1]["endpoint_url"]


def test_list_all_deliveries_invalid_date_422(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.get("/api/v1/admin/notifications/deliveries", params={"from": "not-a-date"})
    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_list_all_deliveries_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.get("/api/v1/admin/notifications/deliveries")
    assert resp.status_code == expected


def test_list_all_deliveries_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.get("/api/v1/admin/notifications/deliveries")
    assert resp.status_code == 500


def test_retry_all_failed_none_pending(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(rows=[]))
    resp = client.post("/api/v1/admin/notifications/deliveries/retry-all-failed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["retried"] == 0
    assert not data["errors"]
    assert data["success"] is True


def test_retry_all_failed_with_one_delivery(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(rows=[(_delivery(), _endpoint())]))
    monkeypatch.setattr(
        admin_notifications,
        "_retry_one_delivery",
        AsyncMock(return_value=(MagicMock(), None)),
    )
    resp = client.post("/api/v1/admin/notifications/deliveries/retry-all-failed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["retried"] == 1
    assert data["success"] is True


def test_retry_all_failed_collects_errors(api: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = api
    session.execute = AsyncMock(return_value=_result(rows=[(_delivery(), _endpoint()), (_delivery(), _endpoint())]))
    monkeypatch.setattr(
        admin_notifications,
        "_retry_one_delivery",
        AsyncMock(side_effect=[(MagicMock(), None), (None, "transport down")]),
    )
    resp = client.post("/api/v1/admin/notifications/deliveries/retry-all-failed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["retried"] == 2
    assert data["errors"] == ["transport down"]
    assert data["success"] is False


@pytest.mark.parametrize(("exc", "expected"), _DB_ERROR_PARAMS)
def test_retry_all_failed_error_mapping(api: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=exc)
    resp = client.post("/api/v1/admin/notifications/deliveries/retry-all-failed")
    assert resp.status_code == expected


def test_retry_all_failed_unexpected_error_500(api: tuple[TestClient, AsyncMock]) -> None:
    client, session = api
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    resp = client.post("/api/v1/admin/notifications/deliveries/retry-all-failed")
    assert resp.status_code == 500


# ---


def test_list_available_events(api: tuple[TestClient, AsyncMock]) -> None:
    client, _ = api
    resp = client.get("/api/v1/admin/notifications/available-events")
    assert resp.status_code == 200
    data = resp.json()
    assert "hitl_awaiting" in data
    assert len(data) == len(admin_notifications.AVAILABLE_EVENTS)
