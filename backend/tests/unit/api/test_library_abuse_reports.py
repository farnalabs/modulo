"""Unit tests for the library abuse-report moderation queue (FAR-1753).

Covers the two routes that expose ``list_abuse_reports`` / ``review_abuse_report``
(``crud/rating.py`` previously had NO route caller):

* ``GET  /api/v1/libraries/admin/abuse-reports`` — org-scoped queue, optional
  ``?status=`` filter, pagination;
* ``POST /api/v1/libraries/admin/abuse-reports/{report_id}/resolve`` — records a
  decision (``reviewed`` / ``dismissed``) stamped with the caller's account.

Unit tier: no DB — the crud functions are patched at the route-module boundary
so the test can assert the route reaches them with the right arguments, and the
SQLAlchemy session is the strict contract-correct double from
``tests/unit/api/mock_session.py``.

Resolving a report RECORDS A DECISION only: nothing in this surface deletes,
hides or mutates the reported rating (there is no rating-delete path).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_REPORTER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_REVIEWED_AT = datetime(2025, 1, 2, tzinfo=UTC)

_LIST_PATH = "/api/v1/libraries/admin/abuse-reports"
_CRUD_LIST = "modulo.api.routes.library.list_abuse_reports"
_CRUD_REVIEW = "modulo.api.routes.library.review_abuse_report"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _principal(*, org_role: str = "admin") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
        is_system_admin=False,
    )


def _build_client(principal: AuthenticatedPrincipal) -> TestClient:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return TestClient(app)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    yield _build_client(_principal())
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    """A caller without ``library.manage`` (operator) — must be refused."""
    yield _build_client(_principal(org_role="viewer"))
    app.dependency_overrides.clear()


def _make_report(
    *,
    report_id: uuid.UUID | None = None,
    status: str = "pending",
    rating_id: uuid.UUID | None = None,
) -> MagicMock:
    """A report double carrying EVERY field ``AbuseReportQueueItem`` validates."""
    report = MagicMock()
    report.id = report_id or uuid.uuid4()
    report.organisation_id = _ORG_ID
    report.primitive_id = uuid.uuid4()
    report.rating_id = rating_id or uuid.uuid4()
    report.reporter_account_id = _REPORTER_ID
    report.reason = "Inappropriate content in the comment"
    report.status = status
    report.reviewed_at = _REVIEWED_AT if status != "pending" else None
    report.reviewer_account_id = _USER_ID if status != "pending" else None
    report.created_at = _NOW
    return report


# ---------------------------------------------------------------------------
# GET — list the queue
# ---------------------------------------------------------------------------


def test_list_queue_empty_returns_empty_page(client: TestClient) -> None:
    page = PageResult(items=[], total=0, page=1, page_size=20)
    with patch(_CRUD_LIST, new_callable=AsyncMock, return_value=page) as crud:
        resp = client.get(_LIST_PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert not body["items"]
    assert body["total"] == 0
    crud.assert_awaited_once()


def test_list_queue_scopes_to_caller_org(client: TestClient) -> None:
    report = _make_report()
    page = PageResult(items=[report], total=1, page=1, page_size=20)
    with patch(_CRUD_LIST, new_callable=AsyncMock, return_value=page) as crud:
        resp = client.get(_LIST_PATH)
    assert resp.status_code == 200, resp.text
    args = crud.await_args
    assert args.args[1] == _ORG_ID
    assert args.kwargs["status_filter"] is None
    assert args.kwargs["page"] == 1
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(report.id)
    assert item["reporter_account_id"] == str(_REPORTER_ID)
    assert item["status"] == "pending"
    assert item["reviewed_at"] is None


def test_list_queue_returns_review_fields(client: TestClient) -> None:
    report = _make_report(status="reviewed")
    page = PageResult(items=[report], total=1, page=1, page_size=20)
    with patch(_CRUD_LIST, new_callable=AsyncMock, return_value=page):
        resp = client.get(_LIST_PATH)
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "reviewed"
    assert datetime.fromisoformat(item["reviewed_at"]) == _REVIEWED_AT
    assert item["reviewer_account_id"] == str(_USER_ID)


def test_list_queue_passes_status_filter(client: TestClient) -> None:
    page = PageResult(items=[], total=0, page=1, page_size=20)
    with patch(_CRUD_LIST, new_callable=AsyncMock, return_value=page) as crud:
        resp = client.get(_LIST_PATH, params={"status": "dismissed", "page": 2, "page_size": 5})
    assert resp.status_code == 200, resp.text
    args = crud.await_args
    assert args.kwargs["status_filter"] == "dismissed"
    assert args.kwargs["page"] == 2
    assert args.kwargs["page_size"] == 5


def test_list_queue_rejects_unknown_status(client: TestClient) -> None:
    page = PageResult(items=[], total=0, page=1, page_size=20)
    with patch(_CRUD_LIST, new_callable=AsyncMock, return_value=page) as crud:
        resp = client.get(_LIST_PATH, params={"status": "open"})
    assert resp.status_code == 422
    crud.assert_not_awaited()


def test_list_queue_viewer_forbidden(viewer_client: TestClient) -> None:
    page = PageResult(items=[], total=0, page=1, page_size=20)
    with patch(_CRUD_LIST, new_callable=AsyncMock, return_value=page) as crud:
        resp = viewer_client.get(_LIST_PATH)
    assert resp.status_code == 403
    crud.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProgrammingError("SELECT 1", {}, Exception("relation does not exist")), 501),
        (SQLAlchemyError(), 503),
        (IntegrityError("INSERT", {}, Exception("duplicate")), 409),
    ],
)
def test_list_queue_db_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    with patch(_CRUD_LIST, new_callable=AsyncMock, side_effect=exc):
        resp = client.get(_LIST_PATH)
    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST — resolve (record a decision)
# ---------------------------------------------------------------------------


def _resolve_path(report_id: uuid.UUID) -> str:
    return f"{_LIST_PATH}/{report_id}/resolve"


def _resolve(client: TestClient, report_id: uuid.UUID, decision: str) -> object:
    return client.post(_resolve_path(report_id), json={"status": decision})


@pytest.mark.parametrize("decision", ["reviewed", "dismissed"])
def test_resolve_records_decision_and_stamps_principal(client: TestClient, decision: str) -> None:
    report_id = uuid.uuid4()
    report = _make_report(report_id=report_id, status=decision)
    with patch(_CRUD_REVIEW, new_callable=AsyncMock, return_value=report) as crud:
        resp = _resolve(client, report_id, decision)
    assert resp.status_code == 200, resp.text
    args = crud.await_args
    assert args.args[1] == report_id
    assert args.kwargs["new_status"] == decision
    assert args.kwargs["reviewer_account_id"] == _USER_ID
    body = resp.json()
    assert body["status"] == decision
    assert body["reviewer_account_id"] == str(_USER_ID)
    # The decision touches the REPORT row only — the rating it points at is
    # still referenced by the response, i.e. it was not removed.
    assert body["rating_id"] == str(report.rating_id)


def test_resolve_rejects_unknown_status(client: TestClient) -> None:
    report_id = uuid.uuid4()
    report = _make_report(report_id=report_id)
    with patch(_CRUD_REVIEW, new_callable=AsyncMock, return_value=report) as crud:
        resp = _resolve(client, report_id, "deleted")
    assert resp.status_code == 422
    crud.assert_not_awaited()


def test_resolve_unknown_report_returns_404(client: TestClient) -> None:
    report_id = uuid.uuid4()
    with patch(_CRUD_REVIEW, new_callable=AsyncMock, return_value=None) as crud:
        resp = _resolve(client, report_id, "reviewed")
    assert resp.status_code == 404
    assert crud.await_args.args[1] == report_id


def test_resolve_viewer_forbidden(viewer_client: TestClient) -> None:
    report_id = uuid.uuid4()
    report = _make_report(report_id=report_id)
    with patch(_CRUD_REVIEW, new_callable=AsyncMock, return_value=report) as crud:
        resp = _resolve(viewer_client, report_id, "reviewed")
    assert resp.status_code == 403
    crud.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProgrammingError("SELECT 1", {}, Exception("relation does not exist")), 501),
        (SQLAlchemyError(), 503),
        (IntegrityError("UPDATE", {}, Exception("duplicate")), 409),
    ],
)
def test_resolve_db_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    with patch(_CRUD_REVIEW, new_callable=AsyncMock, side_effect=exc):
        resp = _resolve(client, uuid.uuid4(), "reviewed")
    assert resp.status_code == expected, resp.text


def test_resolve_route_is_audited(client: TestClient) -> None:
    """The decision route carries the coarse ``audited()`` dependency.

    Driving it proves the audit dependency is actually wired (a dropped
    ``dependencies=[...]`` would leave the route unaudited while every other
    assertion here still passed).
    """
    report_id = uuid.uuid4()
    report = _make_report(report_id=report_id, status="reviewed")
    with (
        patch(_CRUD_REVIEW, new_callable=AsyncMock, return_value=report),
        patch(
            "modulo.core.audit_coverage.append_audit_event_isolated",
            new_callable=AsyncMock,
        ) as append,
    ):
        resp = _resolve(client, report_id, "reviewed")
    assert resp.status_code == 200, resp.text
    assert append.await_count == 1
    assert append.await_args.kwargs["event_type"] == "abuse_report_reviewed"
