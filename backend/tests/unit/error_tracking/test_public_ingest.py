"""Tests for the public unauthenticated error ingest endpoint (/api/v1/errors/ingest/public)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.models.error import SessionKeyResponse
from tests.unit.api.mock_session import configure_mock_session


@pytest.fixture(autouse=True)
def _stub_audit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1516: stub the fresh ``audit_session`` the ``audited_system(...)``
    dependency writes on (a real engine — no database in the unit tier); the
    dependency itself still runs."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def _factory() -> AsyncGenerator[AsyncMock, None]:
        yield session

    monkeypatch.setattr("modulo.core.audit_coverage._shared_session_factory", lambda: _factory)


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
    err_mod._public_rate_limit_capacity_warning_at = 0.0
    err_mod._public_daily_capacity_warning_at = 0.0


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

    def test_daily_cap_exceeded_returns_429(self, client):
        """A client already at the daily event cap is refused with 429."""
        import modulo.api.routes.errors as err_mod

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        # The TestClient's requests arrive from the "testclient" host.
        err_mod._public_daily_event_count["testclient"] = {today: 100}

        with patch(
            "modulo.api.routes.errors._service.ingest_batch",
            AsyncMock(return_value=[{"group_id": str(uuid.uuid4()), "is_new": True}]),
        ):
            resp = client.post("/api/v1/errors/ingest/public", json=_valid_payload())

        assert resp.status_code == 429
        assert "Daily cap exceeded" in resp.json()["detail"]

    def test_ingest_pins_rls_org_context_to_orphan_org(self, client):
        """Regression (FAR-523): the pre-auth public ingest must pin the RLS
        org context to the orphan org BEFORE ingesting.

        error_events/error_groups are OrgScoped with an org-only ALL policy, so
        the INSERTs fail the WITH CHECK when ``app.organisation_id`` is unset —
        and ``ingest_batch`` swallows per-event errors, silently returning 201
        with nothing persisted. The route must call ``set_rls_org`` with
        ``SYSTEM_ORG_ID`` inside its transaction.
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


class TestPublicRateLimiterBoundedState:
    """The public (unauthenticated) IP rate-limiter map must stay hard-bounded.

    Pre-fix the route pruned only the *current* IP's expired timestamps and
    never removed stale keys, so entries for one-off client IPs accumulated
    forever on an unauthenticated route (a memory-exhaustion DoS). These tests
    pin the sweep + LRU hard bound and prove an actively-limited client is never
    evicted-and-reset by a concurrent flood of new IPs.
    """

    @staticmethod
    def _fresh_limiter() -> dict[str, list[float]]:
        return {}

    def test_stale_clients_are_swept_and_key_set_stays_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Many stale keys must be evicted so the tracked set does not grow forever."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 10)
        now = 1000.0
        stale_ages = now - err_mod._PUBLIC_RATE_LIMIT_WINDOW_SECONDS - 60.0
        for i in range(50):
            err_mod._public_rate_limit[f"stale-{i}"] = [stale_ages]

        err_mod._check_public_rate_limit("fresh-client", now)

        assert len(err_mod._public_rate_limit) <= 10
        assert "stale-0" not in err_mod._public_rate_limit
        assert "stale-49" not in err_mod._public_rate_limit
        assert "fresh-client" in err_mod._public_rate_limit

    def test_in_window_clients_are_hard_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The map must stay at/below the cap even when every tracked client is in-window.

        This is the case the sweep alone cannot handle: all keys are live, so
        the sweep frees nothing. The LRU eviction backstop must still bound it.
        """
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 3)
        now = 1000.0
        for i in range(3):
            err_mod._public_rate_limit[f"live-{i}"] = [now]

        err_mod._check_public_rate_limit("new-client", now)

        assert len(err_mod._public_rate_limit) == 3
        assert "new-client" in err_mod._public_rate_limit

    def test_distinct_ip_flood_stays_hard_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flood of distinct new in-window IPs must not grow the map past the cap.

        Documents the bound's trade-off: once every tracked client is in-window
        and the map is full, admitting new IPs evicts least-recently-used keys —
        the hard bound holds, but a client that has gone quiet may be dropped
        and later admitted fresh (LRU is best-effort, not a per-client
        guarantee).
        """
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 4)
        now = 1000.0
        for i in range(4):
            err_mod._public_rate_limit[f"k-{i}"] = [now]

        for i in range(20):
            err_mod._check_public_rate_limit(f"flood-{i}", now)

        assert len(err_mod._public_rate_limit) == 4
        assert "flood-19" in err_mod._public_rate_limit

    def test_eviction_spares_the_most_recently_used_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Eviction must drop the least-recently-used key, not the client seen most recently."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 2)
        now = 1000.0
        err_mod._public_rate_limit["a"] = [now]
        err_mod._public_rate_limit["b"] = [now]
        # Touch 'a' via a rejected request so it becomes most-recently-used.
        with pytest.raises(HTTPException) as first:
            err_mod._check_public_rate_limit("a", now)
        assert first.value.status_code == 429

        err_mod._check_public_rate_limit("c", now)

        assert "a" in err_mod._public_rate_limit
        assert "b" not in err_mod._public_rate_limit
        assert "c" in err_mod._public_rate_limit

    def test_eviction_does_not_weaken_a_limited_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Admitting a new client at cap must not reset an actively-limited client's window."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 2)
        now = 1000.0
        err_mod._public_rate_limit["limited"] = [now]
        err_mod._public_rate_limit["idle"] = [now]
        with pytest.raises(HTTPException) as first:
            err_mod._check_public_rate_limit("limited", now)
        assert first.value.status_code == 429

        err_mod._check_public_rate_limit("new", now)

        assert "limited" in err_mod._public_rate_limit
        assert "idle" not in err_mod._public_rate_limit
        with pytest.raises(HTTPException) as second:
            err_mod._check_public_rate_limit("limited", now)
        assert second.value.status_code == 429

    def test_cap_of_one_still_admits_a_new_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cap of 1 must hard-evict the single live key to admit a new client."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 1)
        now = 1000.0
        err_mod._public_rate_limit["live"] = [now]

        err_mod._check_public_rate_limit("new", now)

        assert len(err_mod._public_rate_limit) == 1
        assert "new" in err_mod._public_rate_limit
        assert "live" not in err_mod._public_rate_limit

    def test_empty_timestamp_list_is_swept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A key holding an empty list carries no rate-limit information and is swept."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_CLIENTS", 1)
        err_mod._public_rate_limit["empty"] = []

        err_mod._check_public_rate_limit("fresh", 1000.0)

        assert "empty" not in err_mod._public_rate_limit
        assert "fresh" in err_mod._public_rate_limit

    def test_rate_limit_still_trips_after_eviction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hardening must not weaken the limiter: a second request still 429s."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        now = 1000.0
        err_mod._check_public_rate_limit("client", now)

        with pytest.raises(HTTPException) as exc:
            err_mod._check_public_rate_limit("client", now)

        assert exc.value.status_code == 429

    def test_sweep_inspects_at_most_one_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stale sweep must scan a bounded batch, never the whole map.

        The sweep runs on the per-request admission path; a full-map scan is the
        O(n) CPU amplification this bound removes. Only the first
        ``_PUBLIC_SWEEP_BATCH`` (LRU-oldest) keys may be inspected.
        """
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_PUBLIC_SWEEP_BATCH", 2)
        now = 1000.0
        for i in range(5):
            err_mod._public_rate_limit[f"stale-{i}"] = [now - 9999]
        err_mod._public_rate_limit["live"] = [now]

        evicted = err_mod._sweep_stale_public_rate_limit_clients(now - 60)

        assert evicted == 2
        assert len(err_mod._public_rate_limit) == 4
        assert "stale-0" not in err_mod._public_rate_limit
        assert "stale-1" not in err_mod._public_rate_limit
        assert "stale-2" in err_mod._public_rate_limit
        assert "live" in err_mod._public_rate_limit

    def test_front_sweep_is_best_effort_when_a_rejected_key_goes_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bounded front scan is best-effort, not exhaustive.

        Rejection re-touches a key (to spare it from LRU eviction) without
        appending ``now``, so its LRU position can be newer than its newest
        timestamp. A key that was rate-limited shortly before going quiet can
        therefore sit BEHIND a still-in-window key, out of reach of a bounded
        front scan. This pins that documented contract: the sweep reclaims what
        it can and the LRU backstop — not the sweep — is the hard bound.
        """
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_PUBLIC_SWEEP_BATCH", 1)
        err_mod._check_public_rate_limit("A", 925.0)
        err_mod._check_public_rate_limit("B", 960.0)
        with pytest.raises(HTTPException):
            # Re-touches "A" (no append), moving it to MRU while its stamp stays 925.
            err_mod._check_public_rate_limit("A", 970.0)

        window_start = 1000.0 - err_mod._PUBLIC_RATE_LIMIT_WINDOW_SECONDS  # 940.0
        assert list(err_mod._public_rate_limit) == ["B", "A"]

        evicted = err_mod._sweep_stale_public_rate_limit_clients(window_start)

        assert evicted == 0
        assert "A" in err_mod._public_rate_limit  # stale but behind in-window "B"
        err_mod._evict_least_recently_used_public_clients(1)
        assert list(err_mod._public_rate_limit) == ["A"]

    def test_capacity_warning_is_interval_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The at-capacity WARNING must fire at most once per interval, not per request."""
        import modulo.api.routes.errors as err_mod

        warn_spy = MagicMock()
        monkeypatch.setattr(err_mod, "_public_rate_limit_capacity_warning_at", 0.0)
        monkeypatch.setattr(err_mod._log, "warning", warn_spy)

        err_mod._warn_public_rate_limit_at_capacity(1000.0)
        err_mod._warn_public_rate_limit_at_capacity(1000.0)
        err_mod._warn_public_rate_limit_at_capacity(1299.0)
        assert warn_spy.call_count == 1

        err_mod._warn_public_rate_limit_at_capacity(1300.0)
        assert warn_spy.call_count == 2

    def test_detail_message_derives_from_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 429 detail must be derived from the window constant, not hardcoded."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_rate_limit", self._fresh_limiter())
        monkeypatch.setattr(err_mod, "_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", 45.0)
        now = 1000.0
        err_mod._public_rate_limit["client"] = [now]

        with pytest.raises(HTTPException) as exc:
            err_mod._check_public_rate_limit("client", now)

        assert exc.value.status_code == 429
        assert "45 seconds" in exc.value.detail
        assert "60 seconds" not in exc.value.detail


class TestPublicDailyCapBoundedState:
    """The public (unauthenticated) daily-cap map must stay hard-bounded.

    Pre-fix the route used ``setdefault`` and pruned only on the SUCCESS path, so
    one-off client IPs that failed later (invalid JSON, a DB error, or the daily
    cap itself) accumulated ``{date: count}`` dicts forever on an unauthenticated
    route. These tests pin the bounded sweep + LRU hard bound that replaces it.
    """

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def test_stale_daily_clients_are_swept_and_key_set_stays_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Many stale keys must be evicted so the tracked set does not grow forever."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_DAILY_CLIENTS", 10)
        for i in range(50):
            err_mod._public_daily_event_count[f"stale-{i}"] = {"2000-01-01": 5}

        days = err_mod._admit_public_daily_client("fresh", 1000.0)

        assert isinstance(days, dict)
        assert len(err_mod._public_daily_event_count) <= 10
        assert "stale-0" not in err_mod._public_daily_event_count
        assert "stale-49" not in err_mod._public_daily_event_count
        assert "fresh" in err_mod._public_daily_event_count

    def test_in_window_daily_clients_are_hard_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The map must stay at/below the cap even when every tracked client is in-window."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_DAILY_CLIENTS", 3)
        today = self._today()
        for i in range(3):
            err_mod._public_daily_event_count[f"live-{i}"] = {today: 1}

        err_mod._admit_public_daily_client("new-client", 1000.0)

        assert len(err_mod._public_daily_event_count) == 3
        assert "new-client" in err_mod._public_daily_event_count

    def test_distinct_ip_daily_flood_stays_hard_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flood of distinct new in-window IPs must not grow the map past the cap."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_DAILY_CLIENTS", 4)
        today = self._today()
        for i in range(4):
            err_mod._public_daily_event_count[f"k-{i}"] = {today: 1}

        for i in range(20):
            err_mod._admit_public_daily_client(f"flood-{i}", 1000.0)

        assert len(err_mod._public_daily_event_count) == 4
        assert "flood-19" in err_mod._public_daily_event_count

    def test_daily_eviction_spares_most_recently_used_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Eviction must drop the least-recently-used key, not the client seen most recently."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_DAILY_CLIENTS", 2)
        today = self._today()
        err_mod._public_daily_event_count["a"] = {today: 1}
        err_mod._public_daily_event_count["b"] = {today: 1}
        # Re-admit 'a' so it becomes most-recently-used.
        err_mod._admit_public_daily_client("a", 1000.0)

        err_mod._admit_public_daily_client("c", 1000.0)

        assert "a" in err_mod._public_daily_event_count
        assert "b" not in err_mod._public_daily_event_count
        assert "c" in err_mod._public_daily_event_count

    def test_daily_eviction_keeps_an_active_clients_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Admitting at cap must not drop or reset an actively-seen client's today count."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_DAILY_CLIENTS", 2)
        today = self._today()
        err_mod._public_daily_event_count["active"] = {today: 42}
        err_mod._public_daily_event_count["idle"] = {today: 1}

        days = err_mod._admit_public_daily_client("active", 1000.0)
        assert days[today] == 42

        err_mod._admit_public_daily_client("new", 1000.0)

        assert "active" in err_mod._public_daily_event_count
        assert err_mod._public_daily_event_count["active"][today] == 42
        assert "idle" not in err_mod._public_daily_event_count

    def test_cap_of_one_daily_map_still_admits_a_new_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cap of 1 must hard-evict the single live key to admit a new client."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        monkeypatch.setattr(err_mod, "_MAX_TRACKED_PUBLIC_DAILY_CLIENTS", 1)
        today = self._today()
        err_mod._public_daily_event_count["live"] = {today: 1}

        err_mod._admit_public_daily_client("new", 1000.0)

        assert len(err_mod._public_daily_event_count) == 1
        assert "new" in err_mod._public_daily_event_count
        assert "live" not in err_mod._public_daily_event_count

    def test_admitted_client_drops_stale_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A long-lived client's entry must not retain dated counters outside the window."""
        import modulo.api.routes.errors as err_mod

        monkeypatch.setattr(err_mod, "_public_daily_event_count", {})
        today = self._today()
        err_mod._public_daily_event_count["client"] = {"2000-01-01": 7, today: 3}

        days = err_mod._admit_public_daily_client("client", 1000.0)

        assert "2000-01-01" not in days
        assert days[today] == 3

    def test_daily_capacity_warning_is_interval_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The daily-cap at-capacity WARNING must fire at most once per interval."""
        import modulo.api.routes.errors as err_mod

        warn_spy = MagicMock()
        monkeypatch.setattr(err_mod, "_public_daily_capacity_warning_at", 0.0)
        monkeypatch.setattr(err_mod._log, "warning", warn_spy)

        err_mod._warn_public_daily_cap_at_capacity(1000.0)
        err_mod._warn_public_daily_cap_at_capacity(1299.0)
        assert warn_spy.call_count == 1

        err_mod._warn_public_daily_cap_at_capacity(1300.0)
        assert warn_spy.call_count == 2


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
