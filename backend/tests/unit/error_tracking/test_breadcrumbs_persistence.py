"""Breadcrumb persistence across ingest → storage → detail serialization.

PRD §8.25 places breadcrumbs inside ``context_json`` (request URL, user agent,
user_id, breadcrumbs). The ingest SDK sends breadcrumbs as a top-level field
(capped at 50 by ``ErrorEventInput``); the route must fold them into
``context_json`` before the service persists the event, and the detail
serializer must read them back so the frontend breadcrumb trail works.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.models.error import ErrorEventInput

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_BREADCRUMBS = [
    {"type": "click", "timestamp": "2026-08-13T00:00:00Z", "data": {"target": "button.save"}},
    {"type": "api", "timestamp": "2026-08-13T00:00:01Z", "data": {"method": "POST", "url": "/runs"}},
]


def _make_ingest_app():
    from modulo.api.dependencies import get_db_session
    from modulo.api.routes.errors import router as errors_router
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

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
        session.execute = AsyncMock(return_value=exec_result)
        return session

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db
    return app


@pytest.fixture(autouse=True)
def _reset_key_store(monkeypatch):
    import modulo.api.routes.errors as err_mod

    monkeypatch.setattr(err_mod, "_key_store", None)
    err_mod._public_rate_limit.clear()
    err_mod._public_daily_event_count.clear()


# =========================================================================
# Route ingest — fold breadcrumbs into context_json
# =========================================================================


class TestIngestFoldsBreadcrumbs:
    def test_authenticated_ingest_persists_breadcrumbs_in_context(self):
        body = {
            "events": [
                {
                    "level": "error",
                    "message": "boom",
                    "source": "backend",
                    "context_json": {"url": "/api/x", "user_id": "u1"},
                    "breadcrumbs": _BREADCRUMBS,
                }
            ]
        }
        client = TestClient(_make_ingest_app())
        with (
            patch("modulo.api.routes.errors._key_store") as ks,
            patch("modulo.api.routes.errors._service.ingest_batch", new_callable=AsyncMock) as ingest,
        ):
            ks.verify_hmac = AsyncMock(return_value=True)
            ingest.return_value = [{"group_id": str(uuid.uuid4()), "is_new": True}]
            resp = client.post(
                "/api/v1/errors/ingest",
                json=body,
                headers={"X-Modulo-Error-Token": "test"},
            )
        assert resp.status_code == 201
        (_, _, events), _ = ingest.call_args
        assert "breadcrumbs" not in events[0]
        assert events[0]["context_json"]["breadcrumbs"] == _BREADCRUMBS
        assert events[0]["context_json"]["url"] == "/api/x"
        assert events[0]["context_json"]["user_id"] == "u1"

    def test_authenticated_ingest_no_breadcrumbs_leaves_context_untouched(self):
        body = {
            "events": [
                {
                    "level": "error",
                    "message": "boom",
                    "source": "backend",
                    "context_json": {"url": "/api/x"},
                }
            ]
        }
        client = TestClient(_make_ingest_app())
        with (
            patch("modulo.api.routes.errors._key_store") as ks,
            patch("modulo.api.routes.errors._service.ingest_batch", new_callable=AsyncMock) as ingest,
        ):
            ks.verify_hmac = AsyncMock(return_value=True)
            ingest.return_value = [{"group_id": str(uuid.uuid4()), "is_new": True}]
            resp = client.post(
                "/api/v1/errors/ingest",
                json=body,
                headers={"X-Modulo-Error-Token": "test"},
            )
        assert resp.status_code == 201
        (_, _, events), _ = ingest.call_args
        assert events[0]["context_json"] == {"url": "/api/x"}

    def test_public_ingest_persists_breadcrumbs_in_context(self):
        body = {
            "events": [
                {
                    "level": "warning",
                    "message": "frontend oops",
                    "source": "frontend",
                    "context_json": {"url": "/"},
                    "breadcrumbs": _BREADCRUMBS,
                }
            ]
        }
        client = TestClient(_make_ingest_app())
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            new_callable=AsyncMock,
        ) as ingest:
            ingest.return_value = [{"group_id": str(uuid.uuid4()), "is_new": True}]
            resp = client.post("/api/v1/errors/ingest/public", json=body)
        assert resp.status_code == 201
        (_, _, events), _ = ingest.call_args
        assert events[0]["context_json"]["breadcrumbs"] == _BREADCRUMBS
        assert events[0]["context_json"]["url"] == "/"


# =========================================================================
# Detail serialization — read breadcrumbs back out of context_json
# =========================================================================


class TestBreadcrumbSerialization:
    def _make_event(self, **kw):
        e = MagicMock()
        e.id = kw.get("id", uuid.uuid4())
        e.level = kw.get("level", "error")
        e.message = kw.get("message", "boom")
        e.stacktrace = kw.get("stacktrace")
        e.context_json = kw.get("context_json")
        e.source = kw.get("source", "backend")
        e.environment = kw.get("environment")
        e.version = kw.get("version")
        e.created_at = kw.get("created_at", datetime.now(UTC))
        return e

    def test_detail_returns_breadcrumbs_from_context(self):
        from modulo.api.routes.errors import _serialize_error_event_detail

        event = self._make_event(context_json={"url": "/x", "breadcrumbs": _BREADCRUMBS})
        detail = _serialize_error_event_detail(event)
        assert detail["breadcrumbs"] == _BREADCRUMBS
        assert detail["context_json"] == {"url": "/x", "breadcrumbs": _BREADCRUMBS}

    def test_detail_returns_none_when_context_has_no_breadcrumbs(self):
        from modulo.api.routes.errors import _serialize_error_event_detail

        event = self._make_event(context_json={"url": "/x"})
        detail = _serialize_error_event_detail(event)
        assert detail["breadcrumbs"] is None

    def test_detail_returns_none_when_context_is_null(self):
        from modulo.api.routes.errors import _serialize_error_event_detail

        detail = _serialize_error_event_detail(self._make_event(context_json=None))
        assert detail["breadcrumbs"] is None

    def test_detail_endpoint_returns_breadcrumbs(self):
        from modulo.api.dependencies import get_db_session
        from modulo.api.routes.errors import router as errors_router
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

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
            session.execute = AsyncMock(return_value=exec_result)
            return session

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_db_session] = _override_db

        group = MagicMock()
        group.id = uuid.uuid4()
        group.fingerprint = "fp"
        group.status = "new"
        group.level_peak = "error"
        group.count = 1
        group.first_seen = datetime.now(UTC)
        group.last_seen = datetime.now(UTC)
        group.sample_event_id = uuid.uuid4()
        group.assigned_to = None

        sample = self._make_event(context_json={"breadcrumbs": _BREADCRUMBS})
        with (
            patch("modulo.api.routes.errors.get_error_group", AsyncMock(return_value=group)),
            patch("modulo.api.routes.errors._fetch_sample_event", AsyncMock(return_value=sample)),
            patch("modulo.api.routes.errors.set_rls_org", AsyncMock()),
        ):
            client = TestClient(app)
            resp = client.get(f"/api/v1/errors/{group.id}")
        assert resp.status_code == 200
        assert resp.json()["sample_event"]["breadcrumbs"] == _BREADCRUMBS

    def test_prepare_event_data_helper_folds_and_excludes(self):
        from modulo.api.routes.errors import _prepare_event_data

        event = ErrorEventInput(
            level="error",
            message="boom",
            source="backend",
            context_json={"url": "/x"},
            breadcrumbs=_BREADCRUMBS,
        )
        data = _prepare_event_data(event)
        assert "breadcrumbs" not in data
        assert data["context_json"]["breadcrumbs"] == _BREADCRUMBS
        assert data["context_json"]["url"] == "/x"
