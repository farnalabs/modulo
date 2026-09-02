"""Tests for the public unauthenticated error ingest endpoint (/api/v1/errors/ingest/public)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.models.error import SessionKeyResponse


def _make_app():
    """Build a minimal FastAPI app with the errors router and overridden DB dependency."""
    from modulo.api.dependencies import get_db_session
    from modulo.api.routes.errors import router as errors_router

    app = FastAPI()
    app.include_router(errors_router)

    async def _override_db():
        session = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = session
        cm.__aexit__.return_value = None
        session.begin.return_value = cm
        return session

    app.dependency_overrides[get_db_session] = _override_db
    return app


def _valid_payload(event_overrides: dict | None = None) -> dict:
    ev = {
        "level": "warning",
        "message": "Something happened on the frontend",
        "source": "frontend",
        "environment": "production",
        "version": "1.0.0",
    }
    if event_overrides:
        ev.update(event_overrides)
    return {"events": [ev]}


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """Clear the module-level rate-limit and daily-cap state before each test."""
    import modulo.api.routes.errors as err_mod

    err_mod._public_rate_limit.clear()
    err_mod._public_daily_event_count.clear()


@pytest.fixture
def client():
    return TestClient(_make_app())


class TestPublicIngestEndpoint:
    """Tests for POST /api/v1/errors/ingest/public."""

    def test_valid_frontend_warning_event_returns_201(self, client):
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}]),
        ):
            resp = client.post("/api/v1/errors/ingest/public", json=_valid_payload())
        assert resp.status_code == 201
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) == 1
        assert "group_id" in data["results"][0]

    def test_source_backend_rejected(self, client):
        """Only frontend-source events are accepted; others are ignored."""
        resp = client.post(
            "/api/v1/errors/ingest/public",
            json=_valid_payload({"source": "backend"}),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert not data["results"]

    def test_level_critical_rejected(self, client):
        """Critical-level events are silently dropped."""
        resp = client.post(
            "/api/v1/errors/ingest/public",
            json=_valid_payload({"level": "critical"}),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert not data["results"]

    def test_body_over_10000_bytes_returns_413(self, client):
        large_message = "x" * 10001
        resp = client.post(
            "/api/v1/errors/ingest/public",
            json=_valid_payload({"message": large_message}),
        )
        assert resp.status_code == 413

    def test_body_just_under_10000_bytes_accepted(self, client):
        """Test boundary: payload just under 10,000 bytes is acceptable."""
        message = "x" * 9800
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}]),
        ):
            resp = client.post(
                "/api/v1/errors/ingest/public",
                json=_valid_payload({"message": message}),
            )
        assert resp.status_code == 201

    def test_missing_body_returns_422(self, client):
        resp = client.post("/api/v1/errors/ingest/public", content=b"", headers={"Content-Type": "application/json"})
        assert resp.status_code == 422

    def test_invalid_json_returns_422(self, client):
        resp = client.post(
            "/api/v1/errors/ingest/public", content=b"not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422

    def test_empty_events_list_returns_422(self, client):
        resp = client.post("/api/v1/errors/ingest/public", json={"events": []})
        assert resp.status_code == 422

    def test_programming_error_returns_501(self, client):
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(side_effect=ProgrammingError("mock", "mock", "mock")),
        ):
            resp = client.post("/api/v1/errors/ingest/public", json=_valid_payload())
        assert resp.status_code == 501
        data = resp.json()
        assert "migrations" in data["detail"].lower()

    def test_rate_limit_exceeded(self, client):
        """First request passes, second within 60s is rate-limited."""
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}]),
        ):
            resp1 = client.post("/api/v1/errors/ingest/public", json=_valid_payload())
            assert resp1.status_code == 201

            # Second request within the same 60s window
            resp2 = client.post("/api/v1/errors/ingest/public", json=_valid_payload())
            assert resp2.status_code == 429

    def test_rate_limit_passes_after_time_window(self, client, monkeypatch):
        """After 60 seconds, a new request should be allowed."""

        fake_time = [1000.0]

        def _fake_time():
            return fake_time[0]

        monkeypatch.setattr("modulo.api.routes.errors._time.time", _fake_time)

        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}]),
        ):
            resp1 = client.post("/api/v1/errors/ingest/public", json=_valid_payload())
            assert resp1.status_code == 201

            # Advance time by 61 seconds
            fake_time[0] = 1061.0
            resp2 = client.post("/api/v1/errors/ingest/public", json=_valid_payload())
            assert resp2.status_code == 201

    def test_ingest_pins_rls_org_context_to_orphan_org(self, client):
        """Regression (FAR-523): the pre-auth public ingest must pin the RLS
        org context to the orphan org BEFORE ingesting.

        error_events/error_groups are OrgScoped with an org-only ALL policy, so
        the INSERTs fail the WITH CHECK when ``app.organisation_id`` is unset —
        and ``ingest_batch`` swallows per-event errors, silently returning 201
        with nothing persisted. The route must call ``set_rls_org`` with
        ``ORPHAN_ORG_ID`` inside its transaction.
        """
        ingest_mock = AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}])
        with (
            patch("modulo.api.routes.errors._service.ingest_batch", ingest_mock),
            patch("modulo.api.routes.errors.set_rls_org", new_callable=AsyncMock) as rls_mock,
        ):
            resp = client.post("/api/v1/errors/ingest/public", json=_valid_payload())

        assert resp.status_code == 201
        rls_mock.assert_awaited_once()
        args = rls_mock.await_args.args
        # Pin the LITERAL nil UUID (not the module constant): if the constant
        # ever drifts from the actual orphan-org partition id this must fail.
        assert args[1] == uuid.UUID(int=0)

    def test_zero_persisted_returns_500_not_false_201(self, client):
        """FIX (FAR-523): ingest_batch swallows per-event failures — if the
        transaction returned ZERO results for submitted events (e.g. the
        orphan-org FK missing), the route must raise 500 instead of acking a
        false-success 201 with an empty results list."""
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[]),
        ):
            resp = client.post("/api/v1/errors/ingest/public", json=_valid_payload())

        assert resp.status_code == 500
        assert "persisted" in resp.json()["detail"]

    def test_partial_persist_stays_201(self, client):
        """Partial success (some events persisted) still acks 201."""
        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}]),
        ):
            resp = client.post(
                "/api/v1/errors/ingest/public",
                json=_valid_payload(),
            )

        assert resp.status_code == 201
        assert len(resp.json()["results"]) == 1


class TestSessionKeyResponse:
    """Verify the SessionKeyResponse model parses correctly with the new `key` field."""

    def test_parses_key_and_expiry(self):
        data = {"key": "abc123", "expires_in_seconds": 3600}
        model = SessionKeyResponse(**data)
        assert model.key == "abc123"
        assert model.expires_in_seconds == 3600

    def test_parses_key_only(self):
        data = {"key": "abc123"}
        model = SessionKeyResponse(**data)
        assert model.key == "abc123"
        assert model.expires_in_seconds == 3600

    def test_json_serialization(self):
        model = SessionKeyResponse(key="test-key-456")
        dumped = json.loads(model.model_dump_json())
        assert dumped["key"] == "test-key-456"
        assert dumped["expires_in_seconds"] == 3600
