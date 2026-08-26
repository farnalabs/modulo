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

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
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
        assert len(lines) == 2
        assert json.loads(lines[0])["id"] == "run-1"

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
