"""Instance-scope (SYSTEM_ORG_ID sentinel partition) error read routes (FAR-1547).

The public error-ingest path and org-less backend ERRORs write instance-level
/unattributed rows into the SYSTEM_ORG_ID partition. Before FAR-1547 every
read route pinned ``principal.organisation_id``, so that partition was
write-only and invisible in the product.

These tests pin the four properties the read view must hold:

* a SYSTEM ADMIN reads the sentinel partition (``/api/v1/errors/instance``);
* a tenant principal is REFUSED 403 — never silently served rows;
* unauthenticated callers are refused 401 (fail closed, no anonymous read);
* the read transaction pins the RLS org to the sentinel BEFORE the first
  query, and the tenant route keeps pinning the tenant's own org — the two
  partitions never cross.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.routes.errors import router as errors_router
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.organisation import SYSTEM_ORG_ID
from tests.unit.api.plan_stubs import all_features

_TENANT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_EVENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_NOW = datetime.now(UTC)


def _make_group(**kw) -> MagicMock:
    g = MagicMock()
    g.id = kw.get("id", _GROUP_ID)
    g.fingerprint = kw.get("fingerprint", "fp-instance")
    g.status = kw.get("status", "new")
    g.level_peak = kw.get("level_peak", "error")
    g.count = kw.get("count", 3)
    g.first_seen = kw.get("first_seen", _NOW)
    g.last_seen = kw.get("last_seen", _NOW)
    g.sample_event_id = kw.get("sample_event_id", _EVENT_ID)
    g.assigned_to = kw.get("assigned_to")
    return g


def _make_event(**kw) -> MagicMock:
    e = MagicMock()
    e.id = kw.get("id", _EVENT_ID)
    e.level = kw.get("level", "error")
    e.message = kw.get("message", "Instance-level failure")
    e.stacktrace = kw.get("stacktrace", "Traceback...")
    e.context_json = kw.get("context_json", {"source": "public-ingest"})
    e.source = kw.get("source", "frontend")
    e.environment = kw.get("environment", "production")
    e.version = kw.get("version", "1.0.0")
    e.created_at = kw.get("created_at", _NOW)
    return e


def _make_db_override():
    async def _override_db():
        session = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = session
        cm.__aexit__.return_value = None
        session.begin.return_value = cm
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        exec_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=exec_result)
        return session

    return _override_db


def _make_app(*, is_system_admin: bool, with_principal: bool = True) -> FastAPI:
    """Build an app with the errors router wired to a controllable principal."""
    app = FastAPI()
    app.include_router(errors_router)

    if with_principal:

        async def _override_user():
            return AuthenticatedPrincipal(
                username="admin",
                organisation_id=_TENANT_ORG_ID,
                account_id=uuid.uuid4(),
                org_role="admin",
                is_system_admin=is_system_admin,
            )

        from modulo.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = _override_user

    from modulo.api.dependencies import get_db_session, get_plan_context

    app.dependency_overrides[get_db_session] = _make_db_override()
    app.dependency_overrides[get_plan_context] = lambda: all_features()
    return app


class TestInstanceListRoute:
    def test_system_admin_reads_sentinel_partition(self):
        with (
            patch("modulo.api.routes.errors.get_error_groups", AsyncMock(return_value=[_make_group()])) as gm,
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock(return_value=1)),
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()) as mock_rls,
        ):
            client = TestClient(_make_app(is_system_admin=True))
            resp = client.get("/api/v1/errors/instance")

        # A 422 here would mean the static route lost to the /{error_id} UUID
        # converter (declaration order).
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["fingerprint"] == "fp-instance"
        # The sentinel org is the read context — NOT the caller's tenant org.
        rls_args = mock_rls.await_args.args
        assert len(rls_args) >= 2, f"set_rls_org must receive the session and the org, got {rls_args}"
        assert rls_args[1] == SYSTEM_ORG_ID
        assert gm.call_args.kwargs["org_id"] == SYSTEM_ORG_ID

    def test_tenant_principal_is_refused(self):
        with (
            patch("modulo.api.routes.errors.get_error_groups", AsyncMock()) as gm,
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock()) as cm,
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()) as mock_rls,
        ):
            client = TestClient(_make_app(is_system_admin=False))
            resp = client.get("/api/v1/errors/instance")

        assert resp.status_code == 403
        assert "errors.resolve_instance" in resp.json()["detail"]
        # Fail closed: no query runs, so a tenant can never be silently
        # served rows (its own or the sentinel's).
        gm.assert_not_awaited()
        cm.assert_not_awaited()
        mock_rls.assert_not_awaited()

    def test_unauthenticated_caller_is_refused(self):
        with patch("modulo.api.routes.errors.get_error_groups", AsyncMock()) as gm:
            client = TestClient(_make_app(is_system_admin=False, with_principal=False))
            resp = client.get("/api/v1/errors/instance")

        assert resp.status_code == 401
        gm.assert_not_awaited()


class TestInstanceDetailAndEventsRoutes:
    def test_system_admin_reads_sentinel_group_detail(self):
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=_make_group())) as gm,
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=_make_event())),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app(is_system_admin=True))
            resp = client.get(f"/api/v1/errors/instance/{_GROUP_ID}")

        assert resp.status_code == 200, resp.text
        assert resp.json()["fingerprint"] == "fp-instance"
        assert resp.json()["sample_event"]["message"] == "Instance-level failure"
        assert gm.call_args.kwargs["org_id"] == SYSTEM_ORG_ID

    def test_system_admin_detail_of_missing_group_is_404(self):
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app(is_system_admin=True))
            resp = client.get(f"/api/v1/errors/instance/{uuid.uuid4()}")

        assert resp.status_code == 404

    def test_tenant_is_refused_detail_and_events(self):
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock()) as gm,
            patch("modulo.api.routes.errors.get_error_events_by_group", AsyncMock()) as ge,
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()) as mock_rls,
        ):
            client = TestClient(_make_app(is_system_admin=False))
            detail_resp = client.get(f"/api/v1/errors/instance/{_GROUP_ID}")
            events_resp = client.get(f"/api/v1/errors/instance/{_GROUP_ID}/events")

        assert detail_resp.status_code == 403
        assert events_resp.status_code == 403
        assert "errors.resolve_instance" in detail_resp.json()["detail"]
        gm.assert_not_awaited()
        ge.assert_not_awaited()
        mock_rls.assert_not_awaited()

    def test_system_admin_reads_sentinel_events(self):
        evts = [_make_event(id=uuid.uuid4()) for _ in range(2)]
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=_make_group())),
            patch("modulo.api.routes.errors.get_error_events_by_group", AsyncMock(return_value=evts)) as ge,
            patch("modulo.api.routes.errors.count_error_events_by_group", AsyncMock(return_value=5)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app(is_system_admin=True))
            resp = client.get(f"/api/v1/errors/instance/{_GROUP_ID}/events")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5
        assert ge.call_args.kwargs["org_id"] == SYSTEM_ORG_ID


class TestRlsContextCorrectness:
    def test_sentinel_rls_is_pinned_before_the_first_query(self):
        """The RLS org must be set INSIDE the transaction, before any read."""
        order: list[str] = []

        async def _capture_rls(_session, org_id: uuid.UUID) -> None:
            order.append(f"rls:{org_id}")

        async def _capture_query(**kwargs):
            order.append(f"query:{kwargs['org_id']}")
            return []

        with (
            patch("modulo.api.routes.errors.set_rls_org", _capture_rls),
            patch("modulo.api.routes.errors.get_error_groups", _capture_query),
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock(return_value=0)),
        ):
            client = TestClient(_make_app(is_system_admin=True))
            resp = client.get("/api/v1/errors/instance")

        assert resp.status_code == 200, resp.text
        assert order[0] == f"rls:{SYSTEM_ORG_ID}", f"RLS must be pinned before the query, got {order}"
        assert order[1] == f"query:{SYSTEM_ORG_ID}", f"the query must read the sentinel partition, got {order}"

    def test_tenant_list_route_still_pins_the_tenant_org(self):
        """Partition separation: the tenant route never reads the sentinel."""
        captured: list[uuid.UUID] = []

        async def _capture_rls(_session, org_id: uuid.UUID) -> None:
            captured.append(org_id)

        async def _capture_query(**kwargs):
            captured.append(kwargs["org_id"])
            return []

        with (
            patch("modulo.api.routes.errors.set_rls_org", _capture_rls),
            patch("modulo.api.routes.errors.get_error_groups", _capture_query),
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock(return_value=0)),
        ):
            client = TestClient(_make_app(is_system_admin=False))
            resp = client.get("/api/v1/errors")

        assert resp.status_code == 200, resp.text
        assert captured == [_TENANT_ORG_ID, _TENANT_ORG_ID]
        assert SYSTEM_ORG_ID not in captured, "the tenant route must never read the sentinel partition"
