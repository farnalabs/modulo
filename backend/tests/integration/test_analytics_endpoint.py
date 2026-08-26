"""Integration tests for GET /api/v1/analytics/query (ADR 020).

Covers: two-org isolation through the endpoint (the explicit org predicate is
the ONLY control on Postgres), predicate-strip → RLS returns zero rows,
feature-gate 402, permission registration, validation (range > 365d, limit >
1000), statement-timeout → 503, and an empty org → empty series.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.auth.jwt import create_access_token
from modulo.auth.permissions import PERMISSIONS, resolve_required
from modulo.core.feature_flags import PlanContext

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


class _NoFeatures:
    def feature_enabled(self, name: str) -> bool:
        return False

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "community"

    def has_license_key(self) -> bool:
        return False


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')",
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _insert_fact(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    run_date: date,
    status: str = "complete",
    trigger_type: str = "manual",
    cost: float | None = 1.25,
    tokens: int | None = 100,
    created_at: datetime | None = None,
    error_code: str | None = None,
    claim_count: int | None = None,
    queue_wait_ms: int | None = None,
    final_idle_ms: int | None = None,
    cancellation_requested: bool | None = None,
    dispatcher: str | None = None,
    node_count: int | None = None,
    sandbox_agent_node_count: int | None = None,
    max_node_timeout_seconds: int | None = None,
    parent_run_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    run_number: int | None = None,
    output_bytes: int | None = None,
    rate_limited: bool | None = None,
    pipeline_id: uuid.UUID | None = None,
    team_name: str | None = None,
    dispatched_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    total_queue_wait_ms: int | None = None,
) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO run_daily_facts (id, organisation_id, run_id, run_date, created_at, "
                "trigger_type, status, total_cost_usd, total_tokens, error_code, claim_count, "
                "queue_wait_ms, final_idle_ms, cancellation_requested, dispatcher, node_count, "
                "sandbox_agent_node_count, max_node_timeout_seconds, parent_run_id, snapshot_id, "
                "run_number, output_bytes, rate_limited, pipeline_id, team_name, dispatched_at, "
                "started_at, completed_at, total_queue_wait_ms) "
                "VALUES (:id, :oid, :rid, :day, :created, :tt, :st, :cost, :tok, :err, :claims, "
                ":qwait, :fidle, :cancel, :disp, :ncount, :sa_count, :max_to, :parent, :snap, "
                ":rnum, :obytes, :rlim, :pid, :tname, :disp_at, :started, :completed, :tqwait)",
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "rid": str(run_id),
                "day": run_date,
                "created": created_at
                if created_at is not None
                else datetime.combine(run_date, datetime.min.time(), tzinfo=UTC),
                "tt": trigger_type,
                "st": status,
                "cost": cost,
                "tok": tokens,
                "err": error_code,
                "claims": claim_count,
                "qwait": queue_wait_ms,
                "fidle": final_idle_ms,
                "cancel": cancellation_requested,
                "disp": dispatcher,
                "ncount": node_count,
                "sa_count": sandbox_agent_node_count,
                "max_to": max_node_timeout_seconds,
                "parent": str(parent_run_id) if parent_run_id else None,
                "snap": str(snapshot_id) if snapshot_id else None,
                "rnum": run_number,
                "obytes": output_bytes,
                "rlim": rate_limited,
                "pid": str(pipeline_id) if pipeline_id else None,
                "tname": team_name,
                "disp_at": dispatched_at,
                "started": started_at,
                "completed": completed_at,
                "tqwait": total_queue_wait_ms,
            },
        )


async def _seed_pipeline_for_fact(
    db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID, name: str
) -> uuid.UUID:
    """Create a real pipeline row so a fact's pipeline_id FK resolves."""
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :desc, :uid, 5, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "desc": f"Pipeline for {name}",
                "uid": str(user_id),
            },
        )
    return pipeline_id


def _token(org_id: uuid.UUID | None, user_id: uuid.UUID, role: str, is_system_admin: bool = False) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(user_id),
        org_role=role,
        is_system_admin=is_system_admin,
    )


@asynccontextmanager
async def _plan_client(
    db_url: str,
    app_engine: AsyncEngine,
    plan: PlanContext,
) -> AsyncGenerator[AsyncClient, None]:
    """An ASGI client whose get_plan_context resolves to *plan*.

    Shared by the feature-gate tests so each plan variation builds its own
    client without duplicating the override wiring.
    """
    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def _plan_ctx() -> PlanContext:
        return plan

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = _plan_ctx
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "AnalyticsEndpoint-A")


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "AnalyticsEndpoint-B")


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "analytics-a@test.local")


@pytest_asyncio.fixture(scope="module")
async def user_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "analytics-b@test.local")


@pytest_asyncio.fixture(scope="module")
async def empty_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Dedicated org for the empty-series test — module-scoped but NEVER written
    to by any other test, so it stays truly empty (org_a is polluted by the
    two-org isolation test which inserts facts for it)."""
    return await _seed_org(db_engine, "AnalyticsEndpoint-Empty")


@pytest_asyncio.fixture(scope="module")
async def empty_user(db_engine: AsyncEngine, empty_org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, empty_org, "analytics-empty@test.local")


@pytest_asyncio.fixture(scope="module")
async def concurrency_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Dedicated org for the concurrency test.

    ``org_a`` is module-scoped and shared with every other test in this file,
    many of which insert ``run_date=today`` facts for it (started_at NULL →
    queued through every hour bucket). The concurrency test asserts EXACT
    per-bucket counts, so running against ``org_a`` makes it order-dependent:
    earlier ``today`` facts inflate ``max_queued``/``max_active``. A fresh org
    keeps the test hermetic regardless of sibling-test pollution or date.
    """
    return await _seed_org(db_engine, "AnalyticsEndpoint-Concurrency")


@pytest_asyncio.fixture(scope="module")
async def concurrency_user(db_engine: AsyncEngine, concurrency_org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, concurrency_org, "analytics-concurrency@test.local")


@pytest_asyncio.fixture(scope="module")
async def ongoing_dimension_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Dedicated org for the ongoing trigger_type dimension test (FAR-158).

    ``org_a`` is module-scoped and shared with every other test in this file,
    many of which insert ``run_date=today`` trigger_type facts for it — so an
    EXACT trigger_type count against ``org_a`` would be order-dependent. A
    fresh org keeps the exact-count assertion hermetic (the concurrency_org
    precedent).
    """
    return await _seed_org(db_engine, "AnalyticsEndpoint-Ongoing")


@pytest_asyncio.fixture(scope="module")
async def ongoing_dimension_user(db_engine: AsyncEngine, ongoing_dimension_org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, ongoing_dimension_org, "analytics-ongoing@test.local")


class TestTwoOrgIsolation:
    async def test_org_b_never_sees_org_a(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today - timedelta(days=1))
        await _insert_fact(db_engine, org_id=org_b, run_id=uuid.uuid4(), run_date=today)

        token = _token(org_b, user_b, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        total = sum(b["count"] for b in payload["buckets"])
        assert total == 1, (
            "org B must see exactly its own run — org A's fact leaked through the query "
            f"(total={total}, buckets={payload['buckets']})"
        )


class TestEmptyOrg:
    async def test_empty_org_returns_empty_series(
        self,
        integration_client: AsyncClient,
        empty_org: uuid.UUID,
        empty_user: uuid.UUID,
    ) -> None:
        token = _token(empty_org, empty_user, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?date_from=2026-07-01&date_to=2026-07-07",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert payload["buckets"], "an empty org must still return zero-filled buckets for the range"
        assert all(b["count"] == 0 for b in payload["buckets"])
        # FAR-200 wire contract: the serialized response must carry the
        # freshness indicator — an empty org reads as "no data yet", not stale.
        assert payload["facts_stale"] is False, "an empty org reads as 'no data yet', not stale"
        assert payload["facts_freshness_hours"] is None, "an empty org has no terminal fact day to measure from"


class TestDimensionedQuery:
    async def test_trigger_type_dimension_returns_keyed_buckets(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """A dimensioned query through the endpoint must return non-None keys.

        Regression guard for PR #740 review round 3: the dimension column was in
        GROUP BY but never in the SELECT, so every bucket collapsed under
        key=None. The raw trigger_type must reach the row and bucket_rows.
        """
        today = datetime.now(UTC).date()
        for tt in ("manual", "cron", "webhook"):
            await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today, trigger_type=tt)

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={today.isoformat()}&date_to={today.isoformat()}&dimension=trigger_type",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        keys = {b["key"] for b in payload["buckets"]}
        assert {"manual", "cron", "webhook"} <= keys, f"expected dimensioned keys, got {keys}"
        assert None not in keys, "dimensioned buckets must carry non-None keys"
        assert sum(b["count"] for b in payload["buckets"]) == 3
        # FAR-200 wire contract: the serialized response must carry the
        # freshness indicator — a fresh org (today's terminal facts) reports
        # numeric hours within the 36h staleness window and is not stale.
        assert payload["facts_freshness_hours"] is not None, "a fresh org must report numeric freshness hours"
        assert payload["facts_freshness_hours"] <= 36, "today's terminal facts are within the 36h staleness window"
        assert payload["facts_stale"] is False, "a fresh org with today's terminal facts is not stale"

    async def test_folder_dimension_returns_uuid_keys(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """folder_id dimensioned query returns the raw UUID keys (no label)."""
        today = datetime.now(UTC).date()
        folder = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipeline_folders (id, organisation_id, name, account_id) "
                    "VALUES (:fid, :oid, 'QA Folder', :aid)",
                ),
                {"fid": str(folder), "oid": str(org_a), "aid": str(user_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO run_daily_facts (id, organisation_id, run_id, run_date, created_at, trigger_type, "
                    "status, folder_id) VALUES (:id, :oid, :rid, :day, :created, 'manual', 'complete', :fid)",
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_a),
                    "rid": str(uuid.uuid4()),
                    "day": today,
                    "created": datetime.combine(today, datetime.min.time(), tzinfo=UTC),
                    "fid": str(folder),
                },
            )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={today.isoformat()}&date_to={today.isoformat()}&dimension=folder",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        keys = {b["key"] for b in payload["buckets"]}
        assert str(folder) in keys, f"expected the folder uuid as a bucket key, got {keys}"
        # org_a also holds earlier null-folder facts in the same range, so None is
        # legitimate — the regression guard is that the folder UUID key is present
        # at all (pre-fix every bucket collapsed under None).
        assert any(b["key"] is not None for b in payload["buckets"]), (
            "dimensioned buckets must not all collapse under None"
        )

    async def test_ongoing_trigger_type_dimension_is_hermetic(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        ongoing_dimension_org: uuid.UUID,
        ongoing_dimension_user: uuid.UUID,
    ) -> None:
        """'ongoing' participates in the trigger_type dimension loop — verified
        against a DEDICATED org so the exact-count assertion never depends on
        sibling-test facts inserted into org_a."""
        today = datetime.now(UTC).date()
        for tt in ("manual", "cron", "webhook", "ongoing"):
            await _insert_fact(
                db_engine,
                org_id=ongoing_dimension_org,
                run_id=uuid.uuid4(),
                run_date=today,
                trigger_type=tt,
            )

        token = _token(ongoing_dimension_org, ongoing_dimension_user, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={today.isoformat()}&date_to={today.isoformat()}&dimension=trigger_type",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        keys = {b["key"] for b in payload["buckets"]}
        assert "ongoing" in keys, f"expected an 'ongoing' dimensioned key, got {keys}"
        assert sum(b["count"] for b in payload["buckets"]) == 4, "exactly the 4 facts inserted in the dedicated org"


class TestHourGranularity:
    async def test_group_by_hour_returns_iso_datetime_buckets(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=today,
            created_at=datetime(today.year, today.month, today.day, 10, 0, tzinfo=UTC),
        )
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=today,
            created_at=datetime(today.year, today.month, today.day, 14, 0, tzinfo=UTC),
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?group_by=hour&date_from={today.isoformat()}&date_to={today.isoformat()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        buckets = resp.json()["buckets"]
        assert len(buckets) == 24, "a single day at hour granularity must zero-fill 24 hourly buckets"
        assert all("T" in b["date"] and b["date"].endswith(":00:00") for b in buckets), (
            "hour buckets must carry ISO datetime dates"
        )
        by_hour = {b["date"]: b["count"] for b in buckets}
        assert by_hour[f"{today.isoformat()}T10:00:00"] >= 1, "the 10:00 fact must land in the 10:00 bucket"
        assert by_hour[f"{today.isoformat()}T14:00:00"] >= 1, "the 14:00 fact must land in the 14:00 bucket"

    async def test_auto_granularity_resolves_hour_for_short_range(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?auto_granularity=true&date_from=2026-08-01&date_to=2026-08-01",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert payload["group_by"] == "hour", "a <=3-day range must resolve to hour granularity"
        assert len(payload["buckets"]) == 24

    async def test_auto_granularity_resolves_week_for_long_range(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?auto_granularity=true&date_from=2026-01-01&date_to=2026-08-01",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["group_by"] == "week", "a >90-day range must resolve to week granularity"


class TestPredicateStrip:
    async def test_no_org_predicate_yields_zero_rows_under_rls(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
    ) -> None:
        """The isolation invariant: modulo_app is BYPASSRLS and the ORM tenant
        filter is not registered on Postgres — the explicit org predicate is the
        only control. As a belt-and-braces check, a predicate-STRIPPED query run
        as a genuinely NOBYPASSRLS role (app_engine = modulo_integration_app)
        with NO org context must return ZERO rows (RLS confines even without the
        predicate)."""
        today = datetime.now(UTC).date()
        await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today - timedelta(days=1))

        from sqlalchemy import select

        from modulo.core.analytics.builder import AnalyticsQuery, build_facts_query
        from modulo.db.models.run_daily_facts import RunDailyFact

        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            # Strip the org predicate: take the builder's statement, drop its
            # WHERE clause, and execute WITHOUT any app.organisation_id context.
            base = AnalyticsQuery(org_id=org_a, date_from=today - timedelta(days=30), date_to=today)
            stmt, params = build_facts_query(base)
            stripped = (
                select(*stmt.selected_columns)
                .where(RunDailyFact.run_date >= (today - timedelta(days=30)))
                .group_by(RunDailyFact.run_date)
            )
            # The aggregate FILTERs (complete/stall counts) carry bound params —
            # the query still needs them even with the org predicate stripped.
            result = await session.execute(stripped, params)
            rows = result.all()
        assert rows == [], "RLS must confine a predicate-stripped query to zero rows"


class TestFeatureAndPermission:
    async def test_require_feature_off_returns_402(
        self,
        db_url: str,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
        from modulo.api.main import app
        from modulo.settings import Settings, get_settings

        settings = Settings(
            database_url=db_url,
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_csrf_enabled=False,
            modulo_auth_rate_limit_enabled=False,
            redis_url="",
            modulo_admin_password="",
        )

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            factory = async_sessionmaker(app_engine, expire_on_commit=False)
            async with factory() as session:
                yield session

        async def _no_features_ctx() -> PlanContext:
            return _NoFeatures()

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[_get_engine] = lambda: app_engine
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_plan_context] = _no_features_ctx

        token = _token(org_a, user_a, "admin")
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/analytics/query",
                    headers={"Authorization": f"Bearer {token}"},
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 402, f"Expected 402 when analytics_page is off, got {resp.status_code}: {resp.text}"

    def test_analytics_query_permission_registered(self) -> None:
        assert PERMISSIONS["analytics.query"] == "viewer"
        assert resolve_required("analytics.query") == "viewer"


class TestValidation:
    async def test_date_range_over_365_days_returns_422(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?date_from=2025-01-01&date_to=2026-08-01",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, f"Expected 422 for a >365d range, got {resp.status_code}: {resp.text}"

    async def test_limit_over_1000_returns_422(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?limit=1001",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, f"Expected 422 for limit > 1000, got {resp.status_code}: {resp.text}"

    async def test_mixed_naive_aware_bounds_do_not_500(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """A bare-date date_from mixed with an aware date_to must NOT 500.

        Pre-fix the range checks compared/subtracted a naive date_from against
        an aware date_to and raised ``TypeError`` (which escaped the handler's
        try/except as a 500). Both bounds are now normalised to aware UTC before
        any comparison, so the request must return a clean 200.
        """
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?date_from=2026-08-01&date_to=2026-08-05T14:00:00Z",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    async def test_non_utc_offset_bounds_convert_to_utc_before_bucketing(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """A -05:00 date_from crossing a date boundary must bucket from the
        UTC-converted date.

        2026-07-31T21:00-05:00 is 2026-08-01T02:00Z, so the day grid must start
        at 2026-08-01 — never the raw local date 2026-07-31 (the pre-fix
        re-labelling behaviour).
        """
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?date_from=2026-07-31T21:00:00-05:00&date_to=2026-08-03T00:00:00-05:00",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        buckets = resp.json()["buckets"]
        assert buckets, "expected zero-filled buckets for the range"
        assert buckets[0]["date"] == "2026-08-01", (
            "the -05:00 date_from 2026-07-31T21:00 must convert to 2026-08-01 02:00Z — "
            f"first bucket is {buckets[0]['date']}"
        )

    async def test_explicit_hour_over_fourteen_days_returns_422(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """Explicit group_by=hour over a >14-day range must return a clean 422.

        The hour-grid amplification guard (PR #766 review finding 4): without
        it, the bucket grid would materialise up to 24 buckets/day per dimension
        key before limit truncation.
        """
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?group_by=hour&date_from=2026-01-01&date_to=2026-01-20",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
        assert "hour" in resp.json()["detail"].lower()


class TestStatementTimeout:
    async def test_statement_timeout_maps_to_503(
        self,
        integration_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        from sqlalchemy import func, select

        import modulo.core.analytics.service as analytics_service

        # PG-only: the endpoint SET LOCALs a tiny statement_timeout and the
        # patched builder emits pg_sleep(5) → QueryCanceled → 503.
        monkeypatch.setattr(analytics_service, "_DEFAULT_STATEMENT_TIMEOUT_MS", 50)
        monkeypatch.setattr(
            analytics_service,
            "build_facts_query",
            lambda _query: (select(func.pg_sleep(5)), {}),
        )
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503, f"Expected 503 on statement timeout, got {resp.status_code}: {resp.text}"
        assert "timeout" in resp.json()["detail"].lower()


class TestProgrammingError:
    async def test_missing_table_maps_to_501(
        self,
        integration_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        import modulo.core.analytics.service as analytics_service

        # A missing table (migrations not applied) raises ProgrammingError. It
        # must map to 501 "run migrations" — NOT be swallowed by the broader
        # DBAPIError branch (which would return 503).
        monkeypatch.setattr(
            analytics_service,
            "build_facts_query",
            lambda _query: (text("SELECT * FROM analytics_no_such_table"), {}),
        )
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 501, f"Expected 501 on missing table, got {resp.status_code}: {resp.text}"
        assert "migration" in resp.json()["detail"].lower()


class TestEnrichedColumns:
    """The FAR-102 enrichment columns must flow through the query buckets."""

    async def test_query_returns_enriched_bucket_fields(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=today,
            status="failed",
            error_code="executor_stalled",
            queue_wait_ms=5000,
            final_idle_ms=120000,
            output_bytes=4096,
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={today.isoformat()}&date_to={today.isoformat()}&status=failed",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        bucket = resp.json()["buckets"][0]
        assert bucket["failure_count"] >= 1
        assert bucket["stall_count"] >= 1, "executor_stalled is a stall error code"
        assert bucket["avg_queue_wait_ms"] == 5000.0
        assert bucket["avg_final_idle_ms"] == 120000.0
        assert bucket["avg_output_bytes"] == 4096.0

    async def test_status_filter_accepts_router_no_match_and_budget_exceeded_round_trip(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        # The analytics status filter must accept every terminal run status that
        # the executor can write to run_daily_facts — including the newer
        # ``router_no_match`` and ``budget_exceeded`` statuses. A stale backend
        # AnalyticsStatus enum returns 422 for these (FAR-415 regression guard).
        day = date(2026, 7, 21)
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=day,
            status="router_no_match",
        )
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=day,
            status="budget_exceeded",
        )

        token = _token(org_a, user_a, "admin")
        for status_value in ("router_no_match", "budget_exceeded"):
            resp = await integration_client.get(
                f"/api/v1/analytics/query?date_from={day.isoformat()}&date_to={day.isoformat()}&status={status_value}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, (
                f"status={status_value} must be a valid analytics filter, got {resp.status_code}: {resp.text}"
            )
            total = sum(b["count"] for b in resp.json()["buckets"])
            assert total == 1, f"status={status_value} filter must return its one fact, got {total}"

    async def test_query_multi_pipeline_filter(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        pid_a = await _seed_pipeline_for_fact(db_engine, org_a, user_a, "Multi-A")
        pid_b = await _seed_pipeline_for_fact(db_engine, org_a, user_a, "Multi-B")
        pid_c = await _seed_pipeline_for_fact(db_engine, org_a, user_a, "Multi-C")
        await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today, pipeline_id=pid_a)
        await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today, pipeline_id=pid_b)
        await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today, pipeline_id=pid_c)

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={today.isoformat()}&date_to={today.isoformat()}"
            f"&pipeline_id={pid_a}&pipeline_id={pid_b}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        total = sum(b["count"] for b in resp.json()["buckets"])
        assert total == 2, f"multi-value pipeline filter must return only A and B runs, got {total}"

    async def test_query_error_code_filter(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        # A distinct past date avoids colliding with other tests' `today` facts
        # in the shared session-scoped org (org_a) — the exact-count assertion
        # requires an uncontaminated run_date.
        day = date(2026, 6, 15)
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=day,
            status="failed",
            error_code="executor_stalled",
        )
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=day,
            status="failed",
            error_code="some_other_error",
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={day.isoformat()}&date_to={day.isoformat()}"
            "&error_code=executor_stalled",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        total = sum(b["count"] for b in resp.json()["buckets"])
        assert total == 1, f"error_code filter must narrow to the matching fact, got {total}"

        # The API presents canonical DOTTED codes while the facts table stores
        # the RAW spelling — the dotted input must expand to the raw
        # ``executor_stalled`` row (build_error_code_condition IN-clause).
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={day.isoformat()}&date_to={day.isoformat()}&error_code=agent.stall",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        total = sum(b["count"] for b in resp.json()["buckets"])
        assert total == 1, f"dotted error_code filter must match the raw executor_stalled fact, got {total}"

    async def test_error_code_dimension_returns_keyed_buckets(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=today,
            status="failed",
            error_code="executor_stalled",
        )
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=today,
            status="failed",
            error_code="node_timeout",
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={today.isoformat()}&date_to={today.isoformat()}&dimension=error_code",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        keys = {b["key"] for b in resp.json()["buckets"]}
        # bucket_rows canonicalizes the RAW facts-table codes (executor_stalled /
        # node_timeout) into the dotted taxonomy, so the dimension returns the
        # canonical keys the runs API presents.
        assert {"agent.stall", "node.timeout"} <= keys, f"expected error_code keys, got {keys}"

    async def test_error_code_unknown_aggregate_round_trip(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        # A distinct past date avoids colliding with other tests' facts in the
        # shared session-scoped org (org_a) — the exact-count assertion requires
        # an uncontaminated run_date.
        day = date(2026, 7, 20)
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=day,
            status="failed",
            error_code="SomeWeirdError",
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={day.isoformat()}&date_to={day.isoformat()}&dimension=error_code",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        keys = {b["key"] for b in resp.json()["buckets"]}
        assert "harness.unknown" in keys, (
            f"unmapped raw error_code must canonicalize into the harness.unknown dimension slice, got {keys}"
        )

        # The aggregate filter (NOT IN over the complement of known codes) must
        # match the SAME raw row the unknown slice shows.
        resp = await integration_client.get(
            f"/api/v1/analytics/query?date_from={day.isoformat()}&date_to={day.isoformat()}&error_code=harness.unknown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        total = sum(b["count"] for b in resp.json()["buckets"])
        assert total == 1, f"error_code=harness.unknown must return the unmapped raw row, got {total}"


class TestExportEndpoint:
    async def test_export_returns_raw_fact_rows(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        rid = uuid.uuid4()
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=rid,
            run_date=today,
            status="failed",
            error_code="executor_stalled",
            claim_count=2,
            queue_wait_ms=5000,
            output_bytes=4096,
            rate_limited=True,
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/export?date_from={today.isoformat()}&date_to={today.isoformat()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert payload["total"] >= 1
        item = next(i for i in payload["items"] if i["run_id"] == str(rid))
        assert item["error_code"] == "executor_stalled"
        assert item["claim_count"] == 2
        assert item["queue_wait_ms"] == 5000
        assert item["output_bytes"] == 4096
        assert item["rate_limited"] is True
        assert item["status"] == "failed"

    async def test_export_csv_attachment(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        await _insert_fact(
            db_engine,
            org_id=org_a,
            run_id=uuid.uuid4(),
            run_date=today,
            error_code="executor_stalled",
        )

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/export?format=csv&date_from={today.isoformat()}&date_to={today.isoformat()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers.get("content-disposition", "")
        body = resp.text
        assert "run_id" in body, "the CSV must carry the fact column headers"
        assert "executor_stalled" in body, "the CSV must carry the fact row values"

    async def test_export_paginates(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        for _ in range(3):
            await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=today)

        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/export?date_from={today.isoformat()}&date_to={today.isoformat()}&limit=2&offset=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert len(payload["items"]) == 2
        assert payload["total"] >= 3

    async def test_export_limit_over_max_returns_422(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/export?limit=5001",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, f"Expected 422 for export limit > 5000, got {resp.status_code}: {resp.text}"

    async def test_export_org_b_never_sees_org_a(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        """Export is raw fact rows — the same isolation invariant as the query.

        Both orgs hold facts on the same day; org B's export must contain only
        its own row (the explicit org predicate is the ONLY isolation control).
        """
        day = date(2026, 6, 20)
        rid_b = uuid.uuid4()
        await _insert_fact(db_engine, org_id=org_a, run_id=uuid.uuid4(), run_date=day, cost=1.25)
        await _insert_fact(db_engine, org_id=org_b, run_id=rid_b, run_date=day, cost=9.99)

        token = _token(org_b, user_b, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/export?date_from={day.isoformat()}&date_to={day.isoformat()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert payload["total"] == 1, (
            "org B's export must contain only its own fact — org A's same-day fact leaked "
            f"(total={payload['total']}, items={payload['items']})"
        )
        assert payload["items"][0]["run_id"] == str(rid_b)
        assert payload["items"][0]["total_cost_usd"] == pytest.approx(9.99)

    async def test_export_require_feature_off_returns_402(
        self,
        db_url: str,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """/export must be gated by the same analytics_page feature as /query."""
        token = _token(org_a, user_a, "admin")
        async with _plan_client(db_url, app_engine, _NoFeatures()) as client:
            resp = await client.get(
                "/api/v1/analytics/export",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 402, f"Expected 402 when analytics_page is off, got {resp.status_code}: {resp.text}"


class TestBackfillEnrichment:
    """Backfilled facts must carry the enriched columns (NULL facts are a bug)."""

    async def test_backfill_populates_enriched_columns(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        from sqlalchemy import event as _sa_event
        from sqlalchemy.pool import NullPool

        # A terminal run WITHOUT a fact — the backfill must pick it up with the
        # enriched columns populated from the source run + snapshot.
        run_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                    "VALUES (:id, :oid, :name, :desc, :uid, 5, 30, 300, "
                    "'{}'::json, '[]'::json, 'manual_approval', 'org')"
                ),
                {"id": str(pipeline_id), "oid": str(org_a), "name": "Backfill-Enrich", "desc": "x", "uid": str(user_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, :gjson, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
                ),
                {
                    "id": str(snapshot_id),
                    "pid": str(pipeline_id),
                    "oid": str(org_a),
                    "gjson": (
                        '{"nodes": [{"id": "n1", "node_type": "agent", "timeout_seconds": 120}, '
                        '{"id": "n2", "node_type": "sandbox_agent", "timeout_seconds": 600}]}'
                    ),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                    "status, input_hash, langgraph_thread_id, run_number, created_at, started_at, "
                    "completed_at, dispatched_at, heartbeat_at, claim_count, cancellation_requested, "
                    "error_code, outputs_json, rate_limit_key) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', 'failed', :hash, :thread, 7, "
                    ":created, :started, :completed, :dispatched, :heartbeat, 3, true, "
                    "'executor_stalled', :outjson, 'rate:limit:key')"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_a),
                    "pid": str(pipeline_id),
                    "sid": str(snapshot_id),
                    "hash": uuid.uuid4().hex,
                    "thread": f"thread-backfill-enrich-{run_id.hex[:8]}",
                    "created": datetime(2026, 8, 7, 8, 59, 0, tzinfo=UTC),
                    "started": datetime(2026, 8, 7, 9, 0, 5, tzinfo=UTC),
                    "dispatched": datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
                    "completed": datetime(2026, 8, 7, 9, 30, 0, tzinfo=UTC),
                    "heartbeat": datetime(2026, 8, 7, 9, 29, 0, tzinfo=UTC),
                    "outjson": '{"node_a": {"result": "ok"}}',
                },
            )

        # Backfill via a BYPASSRLS role (the maintenance cron runs as one): the
        # conftest FORCE-enables RLS on runs/pipeline_snapshots even for
        # superusers, so a cross-org INSERT...SELECT needs BYPASSRLS.
        bypass_role = "analytics_endpoint_bypass"
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(text(f'DROP ROLE IF EXISTS "{bypass_role}"'))
            await conn.execute(text(f'CREATE ROLE "{bypass_role}" NOSUPERUSER BYPASSRLS NOLOGIN'))
            await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{bypass_role}"'))
            await conn.execute(
                text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{bypass_role}"')
            )
            await conn.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{bypass_role}"'))

        from modulo.core.analytics.maintenance import backfill_facts

        engine = create_async_engine(
            db_engine.url.render_as_string(hide_password=False),
            poolclass=NullPool,
        )

        @_sa_event.listens_for(engine.sync_engine, "checkout")
        def _set_role_on_checkout(
            dbapi_connection: object,
            _connection_record: object,
            _connection_proxy: object,
        ) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f'SET ROLE "{bypass_role}"')
            finally:
                cursor.close()

        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
                await backfill_facts(session, date(2026, 8, 7))
        finally:
            await engine.dispose()
            async with db_engine.connect() as conn:
                await conn.execute(text(f'DROP OWNED BY "{bypass_role}"'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{bypass_role}"'))
                await conn.commit()

        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT error_code, claim_count, queue_wait_ms, final_idle_ms, "
                        "cancellation_requested, dispatcher, node_count, sandbox_agent_node_count, "
                        "max_node_timeout_seconds, parent_run_id, snapshot_id, run_number, "
                        "output_bytes, rate_limited, dispatched_at, started_at, completed_at, "
                        "total_queue_wait_ms "
                        "FROM run_daily_facts WHERE run_id = :rid"
                    ),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None, "the backfill must produce a fact for the terminal run"
        assert row[0] == "executor_stalled"
        assert row[1] == 3
        assert row[2] == 5000, "queue_wait_ms = started - dispatched"
        assert row[3] == 60000, "final_idle_ms = completed - heartbeat"
        assert row[4] is True, "cancellation_requested"
        assert row[6] == 2, "node_count must come from the snapshot graph_json"
        assert row[7] == 1, "sandbox_agent_node_count from the snapshot graph_json"
        assert row[8] == 600, "max_node_timeout_seconds from the snapshot graph_json"
        assert row[10] == uuid.UUID(str(snapshot_id))
        assert row[11] == 7
        assert row[12] is not None, "output_bytes from outputs_json"
        assert row[12] > 0, "output_bytes from outputs_json"
        assert row[13] is True, "rate_limited from rate_limit_key"
        # FAR-134 concurrency columns — absolute instants + full queue wait.
        assert row[14] == datetime(2026, 8, 7, 9, 0, tzinfo=UTC), "dispatched_at from Run.dispatched_at"
        assert row[15] == datetime(2026, 8, 7, 9, 0, 5, tzinfo=UTC), "started_at from Run.started_at"
        assert row[16] == datetime(2026, 8, 7, 9, 30, 0, tzinfo=UTC), "completed_at from Run.completed_at"
        assert row[17] == 65000, "total_queue_wait_ms = started - created"


class TestConcurrencyEndpoint:
    """GET /api/v1/analytics/concurrency — slot-utilization series (FAR-134).

    Runs only where Docker/testcontainers is available (CI / merge queue); the
    unit tests pin the overlap math, this pins the endpoint wiring + org
    isolation through the real Postgres.
    """

    async def test_concurrency_buckets_overlap_and_org_scope(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        concurrency_org: uuid.UUID,
        concurrency_user: uuid.UUID,
        org_b: uuid.UUID,
    ) -> None:
        today = datetime.now(UTC).date()
        # A dedicated org (concurrency_org) so the exact-count assertions are
        # hermetic — the module-scoped org_a accumulates run_date=today facts
        # from earlier tests in this file.
        # Org C: one run spanning 09:30..10:30 (overlaps the 09:00 and 10:00
        # hour buckets) + one queued-only run (created 23:00, never started).
        await _insert_fact(
            db_engine,
            org_id=concurrency_org,
            run_id=uuid.uuid4(),
            run_date=today,
            created_at=datetime(today.year, today.month, today.day, 9, 20, tzinfo=UTC),
            started_at=datetime(today.year, today.month, today.day, 9, 30, tzinfo=UTC),
            completed_at=datetime(today.year, today.month, today.day, 10, 30, tzinfo=UTC),
            total_queue_wait_ms=600_000,
        )
        await _insert_fact(
            db_engine,
            org_id=concurrency_org,
            run_id=uuid.uuid4(),
            run_date=today,
            created_at=datetime(today.year, today.month, today.day, 23, 0, tzinfo=UTC),
            started_at=None,
            completed_at=None,
        )
        # Org B: a run that must NOT leak into the concurrency org's buckets.
        await _insert_fact(
            db_engine,
            org_id=org_b,
            run_id=uuid.uuid4(),
            run_date=today,
            created_at=datetime(today.year, today.month, today.day, 9, 30, tzinfo=UTC),
            started_at=datetime(today.year, today.month, today.day, 9, 35, tzinfo=UTC),
            completed_at=datetime(today.year, today.month, today.day, 9, 40, tzinfo=UTC),
        )

        token = _token(concurrency_org, concurrency_user, "admin")
        resp = await integration_client.get(
            f"/api/v1/analytics/concurrency?group_by=hour&date_from={today.isoformat()}&date_to={today.isoformat()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert payload["group_by"] == "hour"
        buckets = {b["date"]: b for b in payload["buckets"]}
        assert len(buckets) == 24, "a single day at hour granularity must zero-fill 24 buckets"
        assert buckets[f"{today.isoformat()}T09:00:00"]["max_active"] == 1, (
            "the 09:30..10:30 run must count toward the 09:00 bucket's active overlap"
        )
        assert buckets[f"{today.isoformat()}T10:00:00"]["max_active"] == 1, (
            "a run spanning a bucket boundary counts in BOTH buckets"
        )
        assert buckets[f"{today.isoformat()}T23:00:00"]["max_queued"] == 1, (
            "a never-started run counts as queued through the range"
        )
        assert buckets[f"{today.isoformat()}T23:00:00"]["max_active"] == 0
        total_active = sum(b["max_active"] for b in payload["buckets"])
        assert total_active == 2, f"org B's run leaked into the concurrency org — total active {total_active}"
