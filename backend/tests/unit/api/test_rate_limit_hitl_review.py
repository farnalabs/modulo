"""Unit tests for HITL review endpoint rate limiting.

PRD §7.18 specifies 20/min for HITL review endpoints. They live under
/api/v1/runs/{run_id}/hitl/{gate_id}/{action} where the run/gate ids are
variable, so they are matched by a dedicated HITL rule (`HITL_RULE`: 20/min)
instead of the more generous /api/v1/runs rule (60/min) that prefix-matches
the path. Since FAR-611 the bucket key normalizes the WHOLE variable tail —
run id, gate id (which is an arbitrary node id like
``hitl_gate_<source>_<target>``, NOT a UUID), and the trailing action — so the
20/min budget is aggregate per identity across runs, gates, and actions. The
pre-FAR-611 normalizer only stripped hex-UUID gate segments, which let the
2026-09-05 bulk-approve sweep (22 gates / ~34 req/min) spread its requests
across per-gate buckets and never exceed 20/min on any single one.

FAR-611 review fix: the approve-capable manual-output submit route
(POST /api/v1/runs/{run_id}/manual/{gate_id}/submit) has no /hitl/ segment in
its path, so it used to ride the 60/min runs rule; it now shares the SAME
aggregate bucket as the /hitl/ review actions.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry, RateLimitRule
from modulo.settings import Settings

HITL_ENDPOINTS = [
    "/api/v1/runs/run-123/hitl/gate-abc/approve",
    "/api/v1/runs/run-123/hitl/gate-abc/reject",
    "/api/v1/runs/run-123/hitl/gate-abc/claim",
    "/api/v1/runs/run-123/hitl/gate-abc/deliver-manual",
    "/api/v1/runs/run-123/hitl/gate-abc/approve-with-modification",
    "/api/v1/runs/run-123/manual/gate-abc/submit",
]

# A realistic (non-hex) HITL gate id — gate ids are "hitl_gate_<source>_<target>"
# node ids, never UUIDs. This is the exact shape that defeated the pre-FAR-611
# normalizer.
_SWEEP_RUN_ID = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
_SWEEP_GATE = "hitl_gate_fetch-pr-title_verify-branch"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",  # nosec
        modulo_ratelimit_bypass_token="test-bypass",
    )


def _make_app(registry: RateLimiterRegistry | None = None) -> FastAPI:
    app = FastAPI()

    for endpoint in HITL_ENDPOINTS:
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)

    app.add_middleware(
        RateLimitMiddleware,  # type: ignore[arg-type]
        settings=_make_settings(),
        registry=registry,
    )
    return app


class TestHitlReviewRateLimit:
    """Verify HITL review endpoints are rate limited under the dedicated 20/min rule."""

    def test_hitl_rule_is_20_per_min(self) -> None:
        """PRD §7.18 defines a dedicated 20/min rule for HITL review."""
        hitl_rule = RateLimitMiddleware.HITL_RULE
        assert hitl_rule.max_requests == 20
        assert hitl_rule.window_s == 60

    def test_hitl_rule_is_more_restrictive_than_runs(self) -> None:
        """HITL review paths must be capped at 20/min, not the runs 60/min."""
        run_rule = next((r for r in RateLimitMiddleware.RULES if r.path_prefix == "/api/v1/runs"), None)
        assert run_rule is not None
        assert RateLimitMiddleware.HITL_RULE.max_requests < run_rule.max_requests

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_endpoint_is_rate_limited(self, endpoint: str) -> None:
        """Each HITL endpoint should be rate limited by the middleware."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_within_limit_succeeds(self, endpoint: str) -> None:
        """Within-limit requests to HITL endpoints should succeed."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        mock_registry.check.assert_awaited_once()

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_429_has_retry_after_header(self, endpoint: str) -> None:
        """429 responses must include a Retry-After header."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Retry-After" in resp.headers

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_key_is_one_aggregate_bucket(self, endpoint: str) -> None:
        """Every HITL review action shares ONE aggregate bucket per identity
        (FAR-611) — the variable run/gate ids and the action are normalized
        away so rotating any of them cannot dodge the 20/min budget."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        mock_registry.check.assert_awaited_once()
        key = mock_registry.check.await_args[0][0]
        assert key == "ip:testclient:/api/v1/runs/<run_id>/hitl/<gate_id>"

    def test_hitl_key_normalizes_variable_uuid_segments(self) -> None:
        """Variable run/gate UUIDs must be normalised to fixed placeholders so
        per-segment bucket rotation never happens (FAR-1304, FAR-611)."""
        run_id = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
        gate_id = "7cba9876-543f-4edc-8ba1-fedcba987654"
        endpoint = f"/api/v1/runs/{run_id}/hitl/{gate_id}/claim"
        app = FastAPI()
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        key = mock_registry.check.await_args[0][0]
        assert key == "ip:testclient:/api/v1/runs/<run_id>/hitl/<gate_id>"

    def test_hitl_key_normalizes_non_uuid_gate_ids(self) -> None:
        """REAL gate ids are node ids like ``hitl_gate_<source>_<target>`` —
        the pre-FAR-611 normalizer only stripped hex-UUID gate segments, so
        these paths kept the raw gate id in the key and every gate got its
        own 20/min bucket. This is the regression test for the 2026-09-05
        sweep (22 gates / ~34 req/min, never throttled)."""
        endpoint = f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{_SWEEP_GATE}/claim"
        app = FastAPI()
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        key = mock_registry.check.await_args[0][0]
        assert key == "ip:testclient:/api/v1/runs/<run_id>/hitl/<gate_id>"
        assert _SWEEP_GATE not in key

    def test_hitl_key_does_not_leak_raw_uuids(self) -> None:
        """Raw run/gate UUIDs must never surface in a rate-limit bucket key."""
        run_id = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
        gate_id = "7cba9876-543f-4edc-8ba1-fedcba987654"
        endpoint = f"/api/v1/runs/{run_id}/hitl/{gate_id}/claim"
        app = FastAPI()
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        key = mock_registry.check.await_args[0][0]
        assert run_id not in key
        assert gate_id not in key

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_check_uses_20_per_min_budget(self, endpoint: str) -> None:
        """The registry check for HITL review must use the 20/min budget."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        mock_registry.check.assert_awaited_once()
        max_requests = mock_registry.check.await_args.kwargs["max_requests"]
        assert max_requests == 20

    def test_hitl_get_not_rate_limited(self) -> None:
        """GET requests to HITL endpoints should not be rate limited."""
        app = FastAPI()
        app.add_api_route(
            "/api/v1/runs/run-123/hitl/gate-abc/pending",
            lambda: {"gates": []},
            methods=["GET"],
            include_in_schema=False,
        )
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.get("/api/v1/runs/run-123/hitl/gate-abc/pending")

        assert resp.status_code != status.HTTP_429_TOO_MANY_REQUESTS

    def test_hitl_prd_20_per_min_is_enforced(self) -> None:
        """PRD §7.18 specifies 20/min for HITL review and it must be enforced."""
        assert RateLimitRule(path_prefix="/hitl/", max_requests=20, window_s=60) == RateLimitMiddleware.HITL_RULE

    def test_rule_for_prefers_hitl_rule_over_runs(self) -> None:
        """_rule_for must resolve HITL paths to the dedicated 20/min rule."""
        instance = RateLimitMiddleware(app=FastAPI(), settings=_make_settings())
        for endpoint in HITL_ENDPOINTS:
            request = MagicMock()
            request.url.path = endpoint
            assert instance._rule_for(request).max_requests == 20

    def test_rule_for_manual_submit_uses_hitl_rule(self) -> None:
        """The manual-output submit route is a HITL approve-capability surface
        (FAR-611 review fix): it must resolve to the 20/min HITL rule, not the
        60/min runs rule."""
        instance = RateLimitMiddleware(app=FastAPI(), settings=_make_settings())
        request = MagicMock()
        request.url.path = f"/api/v1/runs/{_SWEEP_RUN_ID}/manual/gate-abc/submit"
        rule = instance._rule_for(request)
        assert rule is RateLimitMiddleware.HITL_RULE
        assert rule.max_requests == 20

    def test_rule_for_keeps_runs_rule_for_non_hitl(self) -> None:
        """Non-HITL runs paths must stay under the 60/min runs rule."""
        instance = RateLimitMiddleware(app=FastAPI(), settings=_make_settings())
        request = MagicMock()
        request.url.path = "/api/v1/runs/run-123/cancel"
        rule = instance._rule_for(request)
        assert rule.path_prefix == "/api/v1/runs"
        assert rule.max_requests == 60


class _FakePipeline:
    """Minimal transactional pipeline for the sliding-window limiter."""

    def __init__(self, store: dict[str, dict[str, float]]) -> None:
        self._store = store
        self._ops: list[tuple] = []

    def zremrangebyscore(self, key: str, lo: float, hi: float) -> "_FakePipeline":
        self._ops.append(("zrem", key, lo, hi))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> "_FakePipeline":
        self._ops.append(("zadd", key, mapping))
        return self

    def zcard(self, key: str) -> "_FakePipeline":
        self._ops.append(("zcard", key))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list[object]:
        results = [self._apply(op) for op in self._ops]
        self._ops = []
        return results

    def _apply(self, op: tuple) -> object:
        kind = op[0]
        if kind == "zrem":
            _, key, lo, hi = op
            bucket = self._store.setdefault(key, {})
            stale = [member for member, score in bucket.items() if lo <= score <= hi]
            for member in stale:
                del bucket[member]
            return len(stale)
        if kind == "zadd":
            _, key, mapping = op
            self._store.setdefault(key, {}).update(mapping)
            return 1
        if kind == "zcard":
            return len(self._store.get(op[1], {}))
        return True


class _FakeRedis:
    """Sorted-set Redis stand-in for the sliding window (no wall-clock sleeps:
    all requests land inside one 60s window by construction)."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, float]] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self._store)


def _make_sweep_app(gate_ids: list[str], actions: tuple[str, ...], registry: RateLimiterRegistry) -> FastAPI:
    """App exposing claim/approve routes for every gate under a fixed UUID run."""
    app = FastAPI()
    for gate in gate_ids:
        for action in actions:
            app.add_api_route(
                f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{gate}/{action}",
                lambda: {"status": "ok"},
                methods=["POST"],
                include_in_schema=False,
            )
    app.add_middleware(
        RateLimitMiddleware,  # type: ignore[arg-type]
        settings=_make_settings(),
        registry=registry,
    )
    return app


class TestAggregateSweepThrottle:
    """FAR-611 regression: the 2026-09-05 sweep pattern must now be throttled."""

    def test_21st_gate_within_window_is_throttled(self) -> None:
        """21 distinct gates claim+approved in one window hit the aggregate cap.

        The pre-FAR-611 limiter gave every gate its own bucket (one request
        each) so this exact pattern sailed through unthrottled."""
        gates = [f"hitl_gate_node-{i}_review" for i in range(25)]
        registry = RateLimiterRegistry(redis_client=_FakeRedis())
        client = TestClient(_make_sweep_app(gates, ("claim",), registry))

        statuses = [client.post(f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{gate}/claim").status_code for gate in gates]

        assert all(code == status.HTTP_200_OK for code in statuses[:20])
        assert statuses[20] == status.HTTP_429_TOO_MANY_REQUESTS
        assert statuses[21] == status.HTTP_429_TOO_MANY_REQUESTS

    def test_mixed_claim_and_approve_share_the_budget(self) -> None:
        """A claim+approve sweep across one gate set trips at 21 total actions."""
        gates = [f"hitl_gate_node-{i}_verify" for i in range(15)]
        registry = RateLimiterRegistry(redis_client=_FakeRedis())
        client = TestClient(_make_sweep_app(gates, ("claim", "approve"), registry))

        statuses = []
        for gate in gates:
            statuses.append(client.post(f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{gate}/claim").status_code)
            statuses.append(client.post(f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{gate}/approve").status_code)

        first_20_ok = all(code == status.HTTP_200_OK for code in statuses[:20])
        assert first_20_ok
        assert statuses[20] == status.HTTP_429_TOO_MANY_REQUESTS


def _make_mixed_surface_app(gate_ids: list[str], registry: RateLimiterRegistry) -> FastAPI:
    """App exposing BOTH budgeted HITL surfaces for every gate: the /hitl/
    approve route and the /manual/{gate}/submit route (no /hitl/ segment)."""
    app = FastAPI()
    for gate in gate_ids:
        app.add_api_route(
            f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{gate}/approve",
            lambda: {"status": "ok"},
            methods=["POST"],
            include_in_schema=False,
        )
        app.add_api_route(
            f"/api/v1/runs/{_SWEEP_RUN_ID}/manual/{gate}/submit",
            lambda: {"status": "ok"},
            methods=["POST"],
            include_in_schema=False,
        )
    app.add_middleware(
        RateLimitMiddleware,  # type: ignore[arg-type]
        settings=_make_settings(),
        registry=registry,
    )
    return app


class TestManualSubmitSharesHitlBudget:
    """FAR-611 review fix: the manual-output submit route (approve-capable,
    no /hitl/ segment) is budgeted by the SAME aggregate 20/min rule as the
    /hitl/ review actions, and both surfaces share ONE bucket."""

    def test_manual_submit_normalizes_to_hitl_bucket_key(self) -> None:
        """The manual submit's bucket key is the /hitl/ aggregate placeholder —
        alternating surfaces therefore drains one budget, not two."""
        endpoint = f"/api/v1/runs/{_SWEEP_RUN_ID}/manual/gate-abc/submit"
        app = FastAPI()
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        key = mock_registry.check.await_args[0][0]
        assert key == "ip:testclient:/api/v1/runs/<run_id>/hitl/<gate_id>"

    def test_20_mixed_surface_requests_trip_the_21st(self) -> None:
        """20 mixed /hitl/approve + /manual/submit requests across distinct
        gates fill one bucket; the 21st (either surface) 429s. Pre-fix, the
        manual submits rode the 60/min runs rule and the mix sailed through."""
        gates = [f"hitl_gate_node-{i}_manual" for i in range(25)]
        registry = RateLimiterRegistry(redis_client=_FakeRedis())
        client = TestClient(_make_mixed_surface_app(gates, registry))

        paths = []
        for i, gate in enumerate(gates):
            if i % 2 == 0:
                paths.append(f"/api/v1/runs/{_SWEEP_RUN_ID}/hitl/{gate}/approve")
            else:
                paths.append(f"/api/v1/runs/{_SWEEP_RUN_ID}/manual/{gate}/submit")

        statuses = [client.post(path).status_code for path in paths]

        assert all(code == status.HTTP_200_OK for code in statuses[:20])
        assert statuses[20] == status.HTTP_429_TOO_MANY_REQUESTS
        assert statuses[21] == status.HTTP_429_TOO_MANY_REQUESTS
