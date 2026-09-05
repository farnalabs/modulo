"""Tests for error dashboard API endpoints (list, detail, update, events)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.routes.errors import router as errors_router
from modulo.auth.jwt import AuthenticatedPrincipal
from tests.unit.api.plan_stubs import all_features

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_EVENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_NOW = datetime.now(UTC)


def _make_group(**kw):
    g = MagicMock()
    g.id = kw.get("id", _GROUP_ID)
    g.fingerprint = kw.get("fingerprint", "fp123")
    g.status = kw.get("status", "new")
    g.level_peak = kw.get("level_peak", "error")
    g.count = kw.get("count", 5)
    g.first_seen = kw.get("first_seen", _NOW)
    g.last_seen = kw.get("last_seen", _NOW)
    g.sample_event_id = kw.get("sample_event_id", _EVENT_ID)
    g.assigned_to = kw.get("assigned_to")
    return g


def _make_event(**kw):
    e = MagicMock()
    e.id = kw.get("id", _EVENT_ID)
    e.level = kw.get("level", "error")
    e.message = kw.get("message", "Something broke")
    e.stacktrace = kw.get("stacktrace", "Traceback...")
    e.context_json = kw.get("context_json", {"url": "/test"})
    e.source = kw.get("source", "backend")
    e.environment = kw.get("environment", "production")
    e.version = kw.get("version", "1.0.0")
    e.created_at = kw.get("created_at", _NOW)
    return e


def _make_app(execute_result: MagicMock | None = None):
    app = FastAPI()
    app.include_router(errors_router)

    async def _override_user():
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
        )

    async def _override_db():
        session = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = session
        cm.__aexit__.return_value = None
        session.begin.return_value = cm
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        exec_result.scalars.return_value.all.return_value = []
        if execute_result is not None:
            session.execute = AsyncMock(return_value=execute_result)
        else:
            session.execute = AsyncMock(return_value=exec_result)
        return session

    from modulo.api.dependencies import get_db_session, get_plan_context
    from modulo.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_plan_context] = lambda: all_features()
    return app


class TestListErrorGroups:
    def test_empty_list(self):
        with (
            patch("modulo.api.routes.errors.get_error_groups", AsyncMock(return_value=[])),
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock(return_value=0)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/errors")
            assert resp.status_code == 200
            data = resp.json()
            assert not data["items"]
            assert data["total"] == 0
            assert data["limit"] == 20

    def test_paginated(self):
        groups = [_make_group(id=uuid.uuid4()) for _ in range(3)]
        with (
            patch("modulo.api.routes.errors.get_error_groups", AsyncMock(return_value=groups)),
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock(return_value=10)),
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/errors?limit=3&offset=0")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["items"]) == 3
            assert data["total"] == 10

    def test_filters_passed(self):
        with (
            patch("modulo.api.routes.errors.get_error_groups") as gm,
            patch("modulo.api.routes.errors.count_error_groups", AsyncMock(return_value=1)),
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            gm.return_value = [_make_group()]
            client = TestClient(_make_app())
            resp = client.get("/api/v1/errors?status=new&level=error&source=backend")
            assert resp.status_code == 200
            _, kw = gm.call_args
            assert kw["status"] == "new"
            assert kw["level"] == "error"
            assert kw["source"] == "backend"


class TestGetErrorGroupDetail:
    def test_found(self):
        grp = _make_group()
        evt = _make_event()
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=grp)),
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=evt)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.get(f"/api/v1/errors/{_GROUP_ID}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["fingerprint"] == "fp123"
            assert data["level_peak"] == "error"

    def test_not_found(self):
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.get(f"/api/v1/errors/{uuid.uuid4()}")
            assert resp.status_code == 404


class TestPatchErrorGroup:
    def test_update_status(self):
        grp = _make_group()
        grp.status = "resolved"
        with (
            patch("modulo.api.routes.errors.update_error_group", AsyncMock(return_value=grp)),
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.patch(f"/api/v1/errors/{_GROUP_ID}", json={"status": "resolved"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "resolved"

    def test_not_found(self):
        with (
            patch(
                "modulo.api.routes.errors.update_error_group",
                AsyncMock(side_effect=ValueError("ErrorGroup not found")),
            ),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.patch(f"/api/v1/errors/{uuid.uuid4()}", json={"status": "resolved"})
            assert resp.status_code == 404


class TestListErrorEvents:
    def test_returns_events(self):
        evts = [_make_event(id=uuid.uuid4()) for _ in range(2)]
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=_make_group())),
            patch("modulo.api.routes.errors.get_error_events_by_group", AsyncMock(return_value=evts)),
            patch("modulo.api.routes.errors.count_error_events_by_group", AsyncMock(return_value=5)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.get(f"/api/v1/errors/{_GROUP_ID}/events")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["items"]) == 2
            assert data["total"] == 5

    def test_404_for_missing_group(self):
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=None)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(_make_app())
            resp = client.get(f"/api/v1/errors/{uuid.uuid4()}/events")
            assert resp.status_code == 404


class TestSchedulerStarvation:
    """GET /api/v1/errors/scheduler-starvation (FAR-604).

    Pipelines with unstarted pending runs carrying a capacity-marker error_code
    older than the starvation threshold — the pre-terminal runs the error
    dashboard otherwise never sees.
    """

    def _make_row(self, **kw) -> MagicMock:
        row = MagicMock()
        row.pipeline_id = kw.get("pipeline_id", uuid.UUID("00000000-0000-0000-0000-000000000099"))
        row.pipeline_name = kw.get("pipeline_name", "Starved Pipeline")
        row.pending_count = kw.get("pending_count", 63)
        row.oldest_created_at = kw.get("oldest_created_at", _NOW - timedelta(hours=13))
        return row

    def test_returns_starved_pipelines_with_threshold(self):
        rows = [self._make_row()]
        exec_result = MagicMock()
        exec_result.all.return_value = rows
        with patch("modulo.api.routes.errors.set_rls_org", AsyncMock()):
            client = TestClient(_make_app(execute_result=exec_result))
            resp = client.get("/api/v1/errors/scheduler-starvation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["threshold_minutes"] == 10
        item = data["items"][0]
        assert item["pipeline_id"] == str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
        assert item["pipeline_name"] == "Starved Pipeline"
        assert item["pending_count"] == 63
        assert item["oldest_created_at"] == (_NOW - timedelta(hours=13)).isoformat()
        expected_minutes = 13 * 60
        assert abs(item["oldest_age_minutes"] - expected_minutes) < 5, "the oldest age must be reported in minutes"

    def test_empty_when_no_starvation(self):
        exec_result = MagicMock()
        exec_result.all.return_value = []
        with patch("modulo.api.routes.errors.set_rls_org", AsyncMock()):
            client = TestClient(_make_app(execute_result=exec_result))
            resp = client.get("/api/v1/errors/scheduler-starvation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert not data["items"]

    def test_static_route_wins_over_error_id_uuid_route(self):
        # The route is declared BEFORE /{error_id} so the static path is matched
        # directly — if it fell through to the UUID converter it would 422.
        exec_result = MagicMock()
        exec_result.all.return_value = []
        with patch("modulo.api.routes.errors.set_rls_org", AsyncMock()):
            client = TestClient(_make_app(execute_result=exec_result))
            resp = client.get("/api/v1/errors/scheduler-starvation")
        assert resp.status_code == 200, "a 422 here means the {error_id} route shadowed the static path"

    def test_rls_org_scoped_before_query(self):
        exec_result = MagicMock()
        exec_result.all.return_value = []
        with patch("modulo.api.routes.errors.set_rls_org", AsyncMock()) as mock_rls:
            client = TestClient(_make_app(execute_result=exec_result))
            resp = client.get("/api/v1/errors/scheduler-starvation")
        assert resp.status_code == 200
        mock_rls.assert_awaited_once()

    def test_age_anchor_keys_on_earliest_trigger_event_receipt(self):
        # FAR-604: the starvation age must anchor on the run's EARLIEST
        # trigger-event receipt (MIN(trigger_events.received_at), falling back
        # to created_at) — never on created_at alone, which a coalescing
        # re-delivery refreshes on the dispatcher's short re-dispatch cadence
        # (a days-long wedge would read as minutes old and the banner would
        # flap). Pin the SQL shape: the trigger_events correlation + coalesce
        # fallback must be present in the emitted statement.
        captured: dict = {}

        async def _capture_execute(stmt, *args, **kwargs):
            captured["sql"] = str(stmt.compile())
            result = MagicMock()
            result.all.return_value = []
            return result

        app = FastAPI()
        app.include_router(errors_router)

        async def _override_user():
            return AuthenticatedPrincipal(
                username="admin",
                organisation_id=_ORG_ID,
                account_id=uuid.uuid4(),
                org_role="admin",
            )

        async def _override_db():
            session = MagicMock()
            cm = AsyncMock()
            cm.__aenter__.return_value = session
            cm.__aexit__.return_value = None
            session.begin.return_value = cm
            session.execute = _capture_execute
            return session

        from modulo.api.dependencies import get_db_session, get_plan_context
        from modulo.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_db_session] = _override_db
        app.dependency_overrides[get_plan_context] = lambda: all_features()

        with patch("modulo.api.routes.errors.set_rls_org", AsyncMock()):
            client = TestClient(app)
            resp = client.get("/api/v1/errors/scheduler-starvation")
        assert resp.status_code == 200
        sql = captured["sql"]
        assert "trigger_events" in sql, "the age anchor must correlate the run's trigger events"
        assert "received_at" in sql, "the age anchor must key on the earliest event receipt"
        assert "coalesce" in sql.lower(), "runs without a trigger event must fall back to created_at"
