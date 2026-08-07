"""Integration tests for GET /api/v1/analytics/query (ADR 020).

Covers: two-org isolation through the endpoint (the explicit org predicate is
the ONLY control on Postgres), predicate-strip → RLS returns zero rows,
feature-gate 402, permission registration, validation (range > 365d, limit >
1000), statement-timeout → 503, and an empty org → empty series.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.auth.permissions import PERMISSIONS, resolve_required
from modulo.core.feature_flags import PlanContext

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


class _AllFeatures:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


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
) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO run_daily_facts (id, organisation_id, run_id, run_date, created_at, "
                "trigger_type, status, total_cost_usd, total_tokens) "
                "VALUES (:id, :oid, :rid, :day, :created, :tt, :st, :cost, :tok)",
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
            },
        )


def _token(org_id: uuid.UUID | None, user_id: uuid.UUID, role: str, is_system_admin: bool = False) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(user_id),
        org_role=role,
        is_system_admin=is_system_admin,
    )


@pytest_asyncio.fixture
async def integration_client(db_url: str, app_engine: AsyncEngine) -> AsyncClient:
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

    async def _all_features_ctx() -> PlanContext:
        return _AllFeatures()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = _all_features_ctx

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

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
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/analytics/query?date_from=2026-07-01&date_to=2026-07-07",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        payload = resp.json()
        assert payload["buckets"], "an empty org must still return zero-filled buckets for the range"
        assert all(b["count"] == 0 for b in payload["buckets"])


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
            stmt, _ = build_facts_query(base)
            stripped = (
                select(*stmt.selected_columns)
                .where(RunDailyFact.run_date >= (today - timedelta(days=30)))
                .group_by(RunDailyFact.run_date)
            )
            result = await session.execute(stripped)
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

        import modulo.api.routes.analytics as analytics_route

        # PG-only: the endpoint SET LOCALs a tiny statement_timeout and the
        # patched builder emits pg_sleep(5) → QueryCanceled → 503.
        monkeypatch.setattr(analytics_route, "_DEFAULT_STATEMENT_TIMEOUT_MS", 50)
        monkeypatch.setattr(
            analytics_route,
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
        import modulo.api.routes.analytics as analytics_route

        # A missing table (migrations not applied) raises ProgrammingError. It
        # must map to 501 "run migrations" — NOT be swallowed by the broader
        # DBAPIError branch (which would return 503).
        monkeypatch.setattr(
            analytics_route,
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
