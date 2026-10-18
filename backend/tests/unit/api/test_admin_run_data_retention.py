"""Unit tests for /api/v1/admin/run-retention endpoints (FAR-427).

Covers the API contract for the three new admin run-data-retention routes:
candidates listing, JSONL export, and the confirm-gated purge. Authz behaviour
(org-admin scope, system-admin cross-org, feature-tier gate) is asserted against
mocked CRUD + a mocked DB session — no Postgres.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import admin_run_retention as route
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.plan_stubs import all_features

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-000000000009")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")

CANDIDATES_URL = "/api/v1/admin/run-retention/candidates"
EXPORT_URL = "/api/v1/admin/run-retention/export"
PURGE_URL = "/api/v1/admin/run-retention/purge"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _principal(*, role: str = "admin", system_admin: bool = False, org_id: uuid.UUID = _ORG) -> TenantPrincipal:
    return TenantPrincipal(
        username="admin" if role == "admin" else "viewer",
        organisation_id=org_id,
        account_id=_USER,
        org_role=role,
        is_system_admin=system_admin,
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class _ClientBuilder:
    """Build a TestClient with the given principal + a provided mock session."""

    def __init__(self, session: AsyncMock, principal: TenantPrincipal) -> None:
        app.dependency_overrides.clear()
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_plan_context] = lambda: all_features()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_tenant_user] = lambda: principal

    def client(self) -> TestClient:
        return TestClient(app)


def _build(session: AsyncMock, principal: TenantPrincipal) -> TestClient:
    builder = _ClientBuilder(session, principal)
    return builder.client()


@pytest.fixture(autouse=True)
def _clean() -> Generator[None, None, None]:
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /candidates
# ---------------------------------------------------------------------------


class TestCandidates:
    def test_returns_200_with_candidate_shape(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        result = {
            "runs": [
                {
                    "id": str(uuid.uuid4()),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "status": "complete",
                    "pipeline_id": str(uuid.uuid4()),
                    "thread_id": f"{_ORG}:run",
                    "estimated_bytes": 1234,
                }
            ],
            "total_count": 1,
            "total_estimated_bytes": 1234,
            "terminal_total": 1,
            "terminal_estimated_bytes": 1234,
        }
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.list_retention_candidates",
                new=AsyncMock(return_value=result),
            ),
        ):
            resp = client.get(CANDIDATES_URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["total_estimated_bytes"] == 1234
        assert body["terminal_total"] == 1
        assert body["terminal_estimated_bytes"] == 1234
        assert body["runs"][0]["status"] == "complete"
        expected_keys = {"id", "created_at", "status", "pipeline_id", "thread_id", "estimated_bytes"}
        assert set(body["runs"][0].keys()) == expected_keys
        session.begin.assert_called_once()

    def test_scopes_to_org_admin_own_org(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()) as mock_rls,
            patch(
                "modulo.api.routes.admin_run_retention.list_retention_candidates",
                new=AsyncMock(return_value={"runs": [], "total_count": 0, "total_estimated_bytes": 0}),
            ) as mock_list,
        ):
            resp = client.get(CANDIDATES_URL)

        assert resp.status_code == 200
        mock_list.assert_awaited_once()
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["org_id"] == _ORG
        mock_rls.assert_awaited_once_with(session, _ORG)

    def test_org_admin_rejects_cross_org_scope(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        resp = client.get(CANDIDATES_URL, params={"organisation_id": str(_OTHER_ORG)})
        assert resp.status_code == 403

    def test_system_admin_may_scope_to_any_org(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.list_retention_candidates",
                new=AsyncMock(return_value={"runs": [], "total_count": 0, "total_estimated_bytes": 0}),
            ) as mock_list,
        ):
            resp = client.get(CANDIDATES_URL, params={"organisation_id": str(_OTHER_ORG)})

        assert resp.status_code == 200
        assert mock_list.call_args.kwargs["org_id"] == _OTHER_ORG

    def test_non_admin_returns_403(self) -> None:
        session = _make_session()
        client = _build(session, _principal(role="runner"))
        resp = client.get(CANDIDATES_URL)
        assert resp.status_code == 403

    def test_returns_503_on_db_error(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.list_retention_candidates",
                side_effect=RuntimeError("boom"),
            ),
        ):
            resp = client.get(CANDIDATES_URL)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /export
# ---------------------------------------------------------------------------


def _raising_export(exc: Exception):
    """An ``iter_run_export`` stand-in whose FIRST ``__anext__`` raises ``exc``.

    Models a start-up failure (RLS/tenant/validation/migration/database) that
    priming must surface as a real HTTP status before the stream starts.
    """

    async def _gen(session_arg, **kwargs):
        raise exc
        yield  # pragma: no cover - unreachable; keeps this an async generator

    return _gen


class TestExport:
    def test_streams_jsonl_with_download_headers(self) -> None:
        session = _make_session()
        client = _build(session, _principal())

        async def fake_export(session_arg, **kwargs):
            yield '{"id":"run-1","status":"complete"}\n'
            yield '{"id":"run-2","status":"failed"}\n'

        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin_run_retention.iter_run_export", new=fake_export),
        ):
            resp = client.post(EXPORT_URL, json={"status": "complete"})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        assert resp.headers["content-disposition"].startswith("attachment; filename=")
        lines = resp.text.splitlines()
        # Two run records plus the FAR-1924 terminating completeness sentinel.
        assert len(lines) == 3
        assert json.loads(lines[0])["id"] == "run-1"
        assert json.loads(lines[2]) == {"_complete": True}

    def test_streams_a_completeness_sentinel_on_a_clean_stream(self) -> None:
        """FAR-1924: a successful export ends with the completion marker."""
        session = _make_session()
        client = _build(session, _principal())

        async def fake_export(session_arg, **kwargs):
            yield '{"id":"run-1"}\n'

        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin_run_retention.iter_run_export", new=fake_export),
        ):
            resp = client.post(EXPORT_URL, json={})

        assert resp.status_code == 200
        assert resp.text.endswith(route._COMPLETE_SENTINEL)

    def test_empty_stream_still_emits_the_completeness_sentinel(self) -> None:
        """An empty (but successful) export is complete, not truncated."""
        session = _make_session()
        client = _build(session, _principal())

        async def fake_export(session_arg, **kwargs):
            if False:  # pragma: no cover - keeps this an async generator
                yield ""

        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin_run_retention.iter_run_export", new=fake_export),
        ):
            resp = client.post(EXPORT_URL, json={})

        assert resp.status_code == 200
        assert resp.text == route._COMPLETE_SENTINEL

    def test_startup_db_error_yields_503_not_a_truncated_200(self) -> None:
        """FAR-1924 prove-the-fix: a DB error before the first item -> real 503.

        Before priming, ``iter_run_export``'s first ``__anext__`` ran while the
        body was streamed — i.e. after HTTP 200 had already been sent — so the
        client saw a truncated 200 with no error status. Awaiting the first item
        inside the handler maps the error to 503 before the response starts.
        """
        session = _make_session()
        client = _build_unhandled(session, _principal())
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.iter_run_export",
                new=_raising_export(SQLAlchemyError("boom")),
            ),
        ):
            resp = client.post(EXPORT_URL, json={})

        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == "Database temporarily unavailable."

    def test_startup_programming_error_yields_501(self) -> None:
        """A missing migration surfaces as 501, not a truncated 200."""
        session = _make_session()
        client = _build_unhandled(session, _principal())
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.iter_run_export",
                new=_raising_export(ProgrammingError("s", {}, Exception())),
            ),
        ):
            resp = client.post(EXPORT_URL, json={})

        assert resp.status_code == 501, resp.text
        assert resp.json()["detail"] == _MSG_501

    def test_startup_http_exception_passes_through_untouched(self) -> None:
        """An HTTPException raised while priming is re-raised with its status."""
        session = _make_session()
        client = _build_unhandled(session, _principal())
        with (
            patch(
                "modulo.api.routes.admin_run_retention.set_rls_org",
                new=AsyncMock(side_effect=HTTPException(status_code=403, detail="nope")),
            ),
            patch("modulo.api.routes.admin_run_retention.iter_run_export", new=_raising_export(RuntimeError())),
        ):
            resp = client.post(EXPORT_URL, json={})

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "nope"

    def test_startup_unexpected_error_yields_500(self) -> None:
        """A non-DB startup error maps to 500, not a truncated 200."""
        session = _make_session()
        client = _build_unhandled(session, _principal())
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.iter_run_export",
                new=_raising_export(RuntimeError("boom")),
            ),
        ):
            resp = client.post(EXPORT_URL, json={})

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"] == "An unexpected error occurred."

    def test_mid_stream_error_omits_the_completeness_sentinel(self) -> None:
        """The residual is not silent: a truncated stream lacks the sentinel.

        Priming only covers failures at or before the first item. An error AFTER
        the stream begins still truncates the body at HTTP 200 — this asserts
        the completeness sentinel is emitted only on clean exhaustion, so a
        consumer can tell a partial file from a complete one.
        """
        session = _make_session()
        client = _build_unhandled(session, _principal())

        async def fake_export(session_arg, **kwargs):
            yield '{"id":"run-1"}\n'
            raise SQLAlchemyError("mid-stream boom")

        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin_run_retention.iter_run_export", new=fake_export),
        ):
            resp = client.post(EXPORT_URL, json={})

        # The first item primed cleanly, so the status is 200...
        assert resp.status_code == 200
        # ...and the sentinel's ABSENCE is the truncation signal.
        assert route._COMPLETE_SENTINEL not in resp.text

    def test_org_admin_cannot_export_another_org(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        resp = client.post(EXPORT_URL, json={"organisation_id": str(_OTHER_ORG)})
        assert resp.status_code == 403

    def test_system_admin_can_export_cross_org(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        seen_org: list[uuid.UUID | None] = []

        async def fake_export(session_arg, **kwargs):
            seen_org.append(kwargs.get("org_id"))
            yield "[]\n"

        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin_run_retention.iter_run_export", new=fake_export),
        ):
            resp = client.post(
                EXPORT_URL, json={"organisation_id": str(_OTHER_ORG), "date_from": "2026-01-01T00:00:00Z"}
            )

        assert resp.status_code == 200
        assert seen_org == [_OTHER_ORG]


# ---------------------------------------------------------------------------
# POST /purge
# ---------------------------------------------------------------------------


class TestPurge:
    def test_requires_confirm(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        resp = client.post(PURGE_URL, json={})
        assert resp.status_code == 400
        assert "confirm" in resp.json()["detail"]

    def test_purges_and_appends_audit_for_org_scope(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        result = {"purged_runs": 2, "purged_checkpoints": 10, "freed_estimated_bytes": 99999}
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.purge_terminal_runs",
                new=AsyncMock(return_value=result),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()) as audit,
        ):
            resp = client.post(PURGE_URL, json={"confirm": True, "status": "complete"})

        assert resp.status_code == 200
        body = resp.json()
        assert body == result
        audit.assert_awaited_once()
        audit_kwargs = audit.call_args.kwargs
        assert audit_kwargs["org_id"] == _ORG
        assert audit_kwargs["event_type"] == "run_retention_purge"
        assert audit_kwargs["payload_json"]["purged_runs"] == 2

    def test_system_admin_cross_org_purge_skips_org_audit(self) -> None:
        """A cross-org purge has no single org chain to write an audit event to."""
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        result = {"purged_runs": 1, "purged_checkpoints": 0, "freed_estimated_bytes": 100}
        with (
            patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
            patch(
                "modulo.api.routes.admin_run_retention.purge_terminal_runs",
                new=AsyncMock(return_value=result),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()) as audit,
        ):
            resp = client.post(PURGE_URL, json={"confirm": True})

        assert resp.status_code == 200
        audit.assert_not_awaited()

    def test_org_admin_rejects_cross_org_purge(self) -> None:
        session = _make_session()
        client = _build(session, _principal())
        resp = client.post(PURGE_URL, json={"confirm": True, "organisation_id": str(_OTHER_ORG)})
        assert resp.status_code == 403

    def test_non_admin_returns_403(self) -> None:
        session = _make_session()
        client = _build(session, _principal(role="operator"))
        resp = client.post(PURGE_URL, json={"confirm": True})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# @handle_db_errors backstop (FAR-1905)
# ---------------------------------------------------------------------------
#
# ``candidates`` and ``purge`` each reach the DB in-body (``_run_scoped`` plus
# the CRUD call) but, before this change, translated an unexpected DB error only
# through their hand-written ``except`` arms — no ``@handle_db_errors``
# backstop. The arms already cover a DB error raised INSIDE the handler's own
# ``try`` (ProgrammingError -> 501, SQLAlchemyError -> 503, Exception -> 500,
# IntegrityError -> 409 on purge), so those are mapped with or without the
# decorator. The backstop's distinguishing job is to catch a DB error that
# ESCAPES the handler's arms. ``_resolve_org_id`` is the one seam each handler
# calls BEFORE its ``try`` block, so raising from it is the faithful way to
# exercise exactly that class of escape.
#
# ``export`` is intentionally NOT wrapped: its DB work runs inside the
# StreamingResponse generator after the handler returns, so a route-level
# decorator could not catch it.

_SQL = SQLAlchemyError("boom")
_PROG = ProgrammingError("s", {}, Exception())
_MSG_503 = "Database temporarily unavailable."
_MSG_501 = "Feature is not available. Run database migrations to enable it."


def _build_unhandled(session: AsyncMock, principal: TenantPrincipal) -> TestClient:
    """A client that surfaces the app catch-all 500 as a status, not a raise.

    ``raise_server_exceptions=False`` matters for the prove-the-fix assertion:
    without the decorator the DB error escapes to the app's catch-all handler,
    which answers 500; TestClient would otherwise re-raise it.
    """
    _ClientBuilder(session, principal)
    return TestClient(app, raise_server_exceptions=False)


def _issue(client: TestClient, method: str, url: str, body: dict[str, object] | None) -> httpx2.Response:
    """Issue one request for a parametrised route (keeps ``**kwargs`` out of mypy)."""
    if method == "get":
        return client.get(url)
    return client.post(url, json=body)


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("get", CANDIDATES_URL, None),
        ("post", PURGE_URL, {"confirm": True}),
    ],
    ids=["candidates", "purge"],
)
@pytest.mark.parametrize(
    ("exc", "expected", "detail"),
    [(_SQL, 503, _MSG_503), (_PROG, 501, _MSG_501)],
    ids=["sqlalchemy-503", "programming-501"],
)
def test_backstop_maps_escaping_db_error_to_standard_status(
    method: str, url: str, body: dict[str, object] | None, exc: Exception, expected: int, detail: str
) -> None:
    """FAR-1905 prove-the-fix: an unexpected DB error escaping the handler body.

    ``_resolve_org_id`` runs before each handler's ``try``, so a DB error raised
    from it is exactly the error class the hand-written arms do NOT cover. Only
    the ``@handle_db_errors`` backstop maps it: 503 for a generic
    ``SQLAlchemyError``, 501 for a ``ProgrammingError``. Without the decorator
    the error escapes to the app's catch-all handler, which answers 500 with the
    generic internal-error detail — so this assertion fails.
    """
    session = _make_session()
    client = _build_unhandled(session, _principal())
    try:
        with patch("modulo.api.routes.admin_run_retention._resolve_org_id", side_effect=exc):
            resp = _issue(client, method, url, body)
        assert resp.status_code == expected, resp.text
        assert resp.json()["detail"] == detail
    finally:
        app.dependency_overrides.clear()


def test_backstop_does_not_shadow_purge_conflict_verdict() -> None:
    """The purge arm's own IntegrityError -> 409 verdict survives the backstop.

    ``handle_db_errors`` maps ``IntegrityError`` -> 409 with its own generic
    detail ("Resource conflict. The operation could not be completed."). The
    route's hand-written arm raises the purge-specific detail first, inside its
    ``try``; the decorator re-raises an ``HTTPException`` unchanged, so the
    arm's exact detail must win. Asserting that specific detail (which differs
    from the backstop's) is the discriminating check that the new backstop did
    not shadow the route's own classification.
    """
    session = _make_session()
    client = _build(session, _principal())
    with (
        patch("modulo.api.routes.admin_run_retention.set_rls_org", new=AsyncMock()),
        patch(
            "modulo.api.routes.admin_run_retention.purge_terminal_runs",
            new=AsyncMock(side_effect=IntegrityError("s", {}, Exception())),
        ),
    ):
        resp = client.post(PURGE_URL, json={"confirm": True})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Resource conflict. The purge could not be completed."
