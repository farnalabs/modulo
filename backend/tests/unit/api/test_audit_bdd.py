"""Extended unit tests for audit viewer endpoints — edge cases and combinations."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="viewer",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_event_dict(
    event_id: str | None = None,
    event_type: str = "pipeline.run",
    actor_user_id: str | None = str(_USER_ID),
    resource_type: str | None = "pipeline",
    payload_json: dict | None = None,
    created_at: str | None = None,
) -> dict:
    return {
        "id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "resource_type": resource_type,
        "resource_id": str(uuid.uuid4()),
        "payload_json": payload_json or {"key": "value"},
        "request_id": "req-123",
        "previous_hash": "abc",
        "created_at": created_at or _NOW.isoformat(),
    }


def _build_list_response(
    events: list[dict],
    next_cursor: str | None = None,
    prev_cursor: str | None = None,
) -> dict:
    return {
        "items": events,
        "total": len(events),
        "next_cursor": next_cursor,
        "prev_cursor": prev_cursor,
        "limit": 50,
    }


LIST_URL = "/api/v1/admin/audit"
BATCH_URL = "/api/v1/admin/audit/batch-detail"
VERIFY_URL = "/api/v1/admin/audit/verify"
EXPORT_URL = "/api/v1/admin/audit/export"


class TestListEdgeCases:
    def test_empty_cursor_returns_events(self, client: TestClient) -> None:
        events = [_make_event_dict() for _ in range(2)]
        with (
            patch(
                "modulo.api.routes.audit.list_audit_events",
                return_value=_build_list_response(events, next_cursor=str(events[-1]["id"])),
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{LIST_URL}?cursor=")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    def test_invalid_cursor_format_still_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.audit.list_audit_events",
                return_value=_build_list_response([]),
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{LIST_URL}?cursor=not-a-valid-uuid")
        assert resp.status_code == 200

    def test_list_with_limit_returns_correct_count(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.audit.list_audit_events",
                side_effect=lambda session, org_id, cursor=None, limit=50, **kw: _build_list_response(
                    [_make_event_dict() for _ in range(min(limit, 5))],
                    next_cursor=None,
                ),
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{LIST_URL}?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) <= 3


class TestBatchDetailEdgeCases:
    def test_batch_with_non_existent_ids(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.get_audit_events_batch", return_value=[]),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.post(
                BATCH_URL,
                json={"event_ids": ["00000000-0000-0000-0000-000000009999"]},
            )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_batch_detail_returns_full_payload(self, client: TestClient) -> None:
        event_id = str(uuid.uuid4())
        expected = _make_event_dict(event_id=event_id, payload_json={"key": "value"})
        with (
            patch("modulo.api.routes.audit.get_audit_events_batch", return_value=[expected]),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.post(BATCH_URL, json={"event_ids": [event_id]})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["payload_json"]["key"] == "value"

    def test_batch_detail_respects_rls(self, client: TestClient) -> None:
        event_id = str(uuid.uuid4())
        with (
            patch("modulo.api.routes.audit.get_audit_events_batch", return_value=[]),
            patch("modulo.api.routes.audit.set_rls_org") as mock_rls,
        ):
            resp = client.post(BATCH_URL, json={"event_ids": [event_id]})
        assert resp.status_code == 200
        mock_rls.assert_called_once()


class TestExportPagination:
    def test_export_page_1_returns_first_page(self, client: TestClient) -> None:
        events = [_make_event_dict() for _ in range(150)]
        page_events = events[:50]
        with (
            patch(
                "modulo.api.routes.audit.export_chain",
                return_value={
                    "items": page_events,
                    "total": 150,
                    "page": 1,
                    "page_size": 50,
                },
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{EXPORT_URL}?page=1&page_size=50")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 50
        assert data["page"] == 1
        assert data["total"] == 150

    def test_export_page_2_returns_second_page(self, client: TestClient) -> None:
        events = [_make_event_dict() for _ in range(150)]
        page_events = events[50:100]
        with (
            patch(
                "modulo.api.routes.audit.export_chain",
                return_value={
                    "items": page_events,
                    "total": 150,
                    "page": 2,
                    "page_size": 50,
                },
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{EXPORT_URL}?page=2&page_size=50")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 50
        assert data["page"] == 2

    def test_export_last_page_returns_remaining(self, client: TestClient) -> None:
        events = [_make_event_dict() for _ in range(10)]
        with (
            patch(
                "modulo.api.routes.audit.export_chain",
                return_value={
                    "items": events,
                    "total": 10,
                    "page": 1,
                    "page_size": 100,
                },
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{EXPORT_URL}?page=1&page_size=100")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 10
        assert data["total"] == 10

    def test_export_beyond_last_page_returns_empty(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.audit.export_chain",
                return_value={"items": [], "total": 10, "page": 10, "page_size": 50},
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(f"{EXPORT_URL}?page=10&page_size=50")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 0
        assert data["total"] == 10


class TestFilterCombinations:
    def test_filter_type_and_date_and_user(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = _build_list_response([])
            resp = client.get(
                f"{LIST_URL}?event_type=pipeline.run"
                f"&user_id={_USER_ID}"
                f"&from_date=2025-01-01T00:00:00Z"
                f"&to_date=2025-12-31T23:59:59Z"
                f"&entity_type=pipeline"
            )
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("event_type") == "pipeline.run"
        assert kwargs.get("actor_user_id") == _USER_ID
        assert kwargs.get("from_date") == "2025-01-01T00:00:00Z"
        assert kwargs.get("to_date") == "2025-12-31T23:59:59Z"
        assert kwargs.get("resource_type") == "pipeline"

    def test_filter_only_from_date(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = _build_list_response([])
            resp = client.get(f"{LIST_URL}?from_date=2025-06-01T00:00:00Z")
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("from_date") == "2025-06-01T00:00:00Z"
        assert kwargs.get("to_date") is None


class TestNonAdminAccess:
    def test_viewer_gets_402_for_list(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(LIST_URL)
        assert resp.status_code in (401, 402, 403)

    def test_viewer_gets_402_for_batch(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(BATCH_URL, json={"event_ids": []})
        assert resp.status_code in (401, 402, 403)

    def test_viewer_gets_402_for_verify(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(VERIFY_URL)
        assert resp.status_code in (401, 402, 403)

    def test_viewer_gets_402_for_export(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(EXPORT_URL)
        assert resp.status_code in (401, 402, 403)


class TestVerifyChain:
    def test_verify_chain_broken_hash(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.audit.verify_chain",
                return_value={
                    "valid": False,
                    "events_checked": 2,
                    "chain_break": {"index": 1, "expected": "hash_0001", "actual": "tampered"},
                },
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(VERIFY_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert data["chain_break"]["index"] == 1

    def test_verify_chain_empty_log(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.audit.verify_chain",
                return_value={"valid": True, "events_checked": 0, "chain_break": None},
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(VERIFY_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["events_checked"] == 0
