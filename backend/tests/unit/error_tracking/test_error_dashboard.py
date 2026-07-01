"""Tests for error dashboard API endpoints (list, detail, update, events)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.routes.errors import router as errors_router
from modulo.auth.jwt import AuthenticatedPrincipal

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
    g.assigned_to = kw.get("assigned_to", None)
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


def _make_app():
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
        return session

    from modulo.api.dependencies import get_db_session
    from modulo.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db
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
            assert data["items"] == []
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
            patch("modulo.api.routes.errors.update_error_group", AsyncMock(side_effect=ValueError("ErrorGroup not found"))),
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
