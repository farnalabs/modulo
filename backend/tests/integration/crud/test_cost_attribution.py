"""Integration tests for cost attribution — daily run counts, spend limits,
and team-level cost tracking with real Postgres.

Requires a running Postgres via testcontainers (pytest.mark.integration).
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.cost_controller import (
    check_and_record_spend,
    get_cost_report,
)
from modulo.db.crud.daily_run_count import (
    get_daily_run_counts,
    get_org_spend_total,
    upsert_daily_run_count,
)
from modulo.db.crud.team import create_team

pytestmark = pytest.mark.integration


async def _create_org(db_engine: AsyncEngine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) "
                    "VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {"id": str(org_id), "name": f"Org {slug}", "slug": slug},
            )
    return org_id


async def _create_user(
    db_engine: AsyncEngine, org_id: uuid.UUID, email: str
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO users (id, organisation_id, email, display_name, "
                    "org_role, auth_provider, active) "
                    "VALUES (:id, :org_id, :email, :name, 'admin', 'local', true)"
                ),
                {
                    "id": str(user_id),
                    "org_id": str(org_id),
                    "email": email,
                    "name": email.split("@")[0],
                },
            )
    return user_id


async def _set_org_limit(
    db_engine: AsyncEngine, org_id: uuid.UUID, limit: Decimal | None
) -> None:
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("UPDATE organisations SET daily_spend_limit = :limit WHERE id = :oid"),
                {"limit": limit, "oid": str(org_id)},
            )


async def _set_team_limit(
    db_engine: AsyncEngine, team_id: uuid.UUID, limit: Decimal | None
) -> None:
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("UPDATE teams SET daily_spend_limit = :limit WHERE id = :tid"),
                {"limit": limit, "tid": str(team_id)},
            )


# ---------------------------------------------------------------------------
# Daily run count CRUD
# ---------------------------------------------------------------------------


async def test_upsert_daily_run_count_creates_and_increments(
    db_engine: AsyncEngine,
) -> None:
    """upsert_daily_run_count creates a new row and increments on repeat."""
    org = await _create_org(db_engine, f"drc-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )

        created = await upsert_daily_run_count(
            session,
            org_id=org,
            increment_count=3,
            increment_spend=Decimal("15.50"),
        )
        await session.flush()
        assert created.run_count == 3
        assert created.total_spend_usd == Decimal("15.50")

        incremented = await upsert_daily_run_count(
            session,
            org_id=org,
            increment_count=1,
            increment_spend=Decimal("5.00"),
        )
        await session.flush()
        assert incremented.id == created.id
        assert incremented.run_count == 4
        assert incremented.total_spend_usd == Decimal("20.50")


async def test_upsert_daily_run_count_with_team(
    db_engine: AsyncEngine,
) -> None:
    """Team-scoped daily run counts are stored separately from org-scoped."""
    org = await _create_org(db_engine, f"drc-team-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "drc-team@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Cost Team", created_by=user)
        await session.flush()

        org_row = await upsert_daily_run_count(
            session, org_id=org, increment_count=5, increment_spend=Decimal("50")
        )
        await session.flush()

        team_row = await upsert_daily_run_count(
            session, org_id=org, team_id=team.id, increment_count=2, increment_spend=Decimal("20")
        )
        await session.flush()

        assert org_row.run_count == 5
        assert org_row.team_id is None
        assert team_row.run_count == 2
        assert team_row.team_id == team.id


async def test_get_daily_run_counts_filters(
    db_engine: AsyncEngine,
) -> None:
    """get_daily_run_counts respects team_id and date filters."""
    org = await _create_org(db_engine, f"drc-filter-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "drc-filter@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Filter Team", created_by=user)
        await session.flush()

        await upsert_daily_run_count(session, org_id=org, increment_count=1)
        await upsert_daily_run_count(session, org_id=org, team_id=team.id, increment_count=2)
        await session.flush()

    # Query org-wide
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        all_counts = await get_daily_run_counts(session, org_id=org)
        assert len(all_counts) >= 2

    # Query by team
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team_counts = await get_daily_run_counts(session, org_id=org, team_id=team.id)
        assert len(team_counts) >= 1
        for c in team_counts:
            assert c.team_id == team.id

    # Query org-only (no team)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        org_counts = await get_daily_run_counts(session, org_id=org, team_id=None)
        for c in org_counts:
            assert c.team_id is None


async def test_get_org_spend_total_excludes_team_rows(
    db_engine: AsyncEngine,
) -> None:
    """get_org_spend_total only sums rows where team_id IS NULL."""
    org = await _create_org(db_engine, f"spend-total-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "spend-total@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Spend Team", created_by=user)
        await session.flush()

        await upsert_daily_run_count(session, org_id=org, increment_spend=Decimal("100"))
        await upsert_daily_run_count(
            session, org_id=org, team_id=team.id, increment_spend=Decimal("50")
        )
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        total = await get_org_spend_total(session, org_id=org)
        assert total == Decimal("100")


# ---------------------------------------------------------------------------
# cost_controller integration
# ---------------------------------------------------------------------------


async def test_check_and_record_spend_happy_path(
    db_engine: AsyncEngine,
) -> None:
    """check_and_record_spend increments counts when no limit is set."""
    org = await _create_org(db_engine, f"spend-happy-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            approved, reason = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("10"), team_id=None
            )

        assert approved is True
        assert reason is None

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        counts = await get_daily_run_counts(session, org_id=org)
        assert len(counts) == 1
        assert counts[0].run_count == 1
        assert counts[0].total_spend_usd == Decimal("10")


async def test_check_and_record_spend_enforces_org_limit(
    db_engine: AsyncEngine,
) -> None:
    """Spend over the org daily limit is rejected."""
    org = await _create_org(db_engine, f"spend-limit-{uuid.uuid4().hex[:8]}")
    await _set_org_limit(db_engine, org, Decimal("100"))
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            ok1, _ = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("60"), team_id=None
            )
            assert ok1 is True

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            ok2, err2 = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("50"), team_id=None
            )
            assert ok2 is False
            assert "organisation" in (err2 or "").lower()

    # Verify only the first spend was recorded
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        counts = await get_daily_run_counts(session, org_id=org)
        assert counts[0].total_spend_usd == Decimal("60")
        assert counts[0].run_count == 1


async def test_check_and_record_spend_with_team_enforces_team_limit(
    db_engine: AsyncEngine,
) -> None:
    """Team-level spend limits are enforced independently of org limits."""
    org = await _create_org(db_engine, f"team-limit-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "team-limit@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team1 = await create_team(session, org_id=org, name="Team 1", created_by=user)
        team2 = await create_team(session, org_id=org, name="Team 2", created_by=user)
        await session.flush()

    await _set_org_limit(db_engine, org, Decimal("1000"))
    await _set_team_limit(db_engine, team1.id, Decimal("100"))
    await _set_team_limit(db_engine, team2.id, Decimal("50"))

    # Team 1: under limit
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            ok, _ = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("80"), team_id=team1.id
            )
            assert ok is True

    # Team 2: under limit
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            ok, _ = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("30"), team_id=team2.id
            )
            assert ok is True

    # Team 1: exceeds team limit (80 + 30 = 110 > 100)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            ok, err = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("30"), team_id=team1.id
            )
            assert ok is False
            assert "team" in (err or "").lower()

    # Team 2: at exact limit (30 + 20 = 50)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        async with session.begin():
            ok, _ = await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("20"), team_id=team2.id
            )
            assert ok is True

    # Verify team counts
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        all_counts = await get_daily_run_counts(session, org_id=org)
        team_rows = [c for c in all_counts if c.team_id is not None]
        assert len(team_rows) == 2

    # Verify org-level total includes all spend
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        await get_org_spend_total(session, org_id=org)
        # Only org-level rows (no team_id) — team rows excluded
        org_row = [c for c in all_counts if c.team_id is None]
        assert len(org_row) == 1
        assert org_row[0].total_spend_usd == Decimal("80") + Decimal("30") + Decimal("20")


async def test_get_cost_report_by_team(
    db_engine: AsyncEngine,
) -> None:
    """get_cost_report returns team-level aggregates."""
    org = await _create_org(db_engine, f"report-team-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "report-team@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team_a = await create_team(session, org_id=org, name="Report A", created_by=user)
        team_b = await create_team(session, org_id=org, name="Report B", created_by=user)
        await session.flush()

        # Record some spend
        async with session.begin():
            await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("100"), team_id=team_a.id
            )
        async with session.begin():
            await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("50"), team_id=team_a.id
            )
        async with session.begin():
            await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("75"), team_id=team_b.id
            )

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        report = await get_cost_report(
            session, org_id=org, group_by="team", period="month"
        )

        report_map = {r["entity_name"]: r for r in report}
        assert report_map["Report A"]["total_spend_usd"] == 150.0
        assert report_map["Report A"]["total_runs"] == 2
        assert report_map["Report B"]["total_spend_usd"] == 75.0
        assert report_map["Report B"]["total_runs"] == 1


async def test_get_cost_report_by_org(
    db_engine: AsyncEngine,
) -> None:
    """get_cost_report returns org-level aggregate (excluding team rows)."""
    org = await _create_org(db_engine, f"report-org-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "report-org@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Inner Team", created_by=user)
        await session.flush()

        # Org-level spend (no team_id)
        async with session.begin():
            await check_and_record_spend(session, org_id=org, cost_usd=Decimal("200"))
        # Team-level spend
        async with session.begin():
            await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("100"), team_id=team.id
            )

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        report = await get_cost_report(
            session, org_id=org, group_by="org", period="month"
        )

        assert len(report) == 1
        # Only org-level rows are aggregated
        assert report[0]["total_spend_usd"] == 200.0
        assert report[0]["total_runs"] == 1


async def test_unique_constraint_enforced(
    db_engine: AsyncEngine,
) -> None:
    """The unique constraint on (org_id, team_id, run_date) is enforced."""
    org = await _create_org(db_engine, f"unique-drc-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "unique-drc@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Unique DRC Team", created_by=user)
        await session.flush()

        async with session.begin():
            await check_and_record_spend(
                session, org_id=org, cost_usd=Decimal("10"), team_id=team.id
            )

    # Duplicate via raw insert should fail
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO org_daily_run_counts "
                    "(id, organisation_id, team_id, run_date, run_count, total_spend_usd) "
                    "VALUES (:id, :oid, :tid, :d, 1, 0)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org),
                    "tid": str(team.id),
                    "d": today,
                },
            )
            await session.commit()


async def test_daily_run_count_isolation_between_orgs(
    db_engine: AsyncEngine,
) -> None:
    """Daily run counts are isolated between organisations via RLS."""
    org_a = await _create_org(db_engine, f"iso-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"iso-b-{uuid.uuid4().hex[:8]}")
    user_a = await _create_user(db_engine, org_a, "iso-a@test.com")
    user_b = await _create_user(db_engine, org_b, "iso-b@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        team_a = await create_team(session, org_id=org_a, name="Iso Team A", created_by=user_a)
        await session.flush()
        await upsert_daily_run_count(session, org_id=org_a, team_id=team_a.id, increment_count=5)
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        team_b = await create_team(session, org_id=org_b, name="Iso Team B", created_by=user_b)
        await session.flush()
        await upsert_daily_run_count(session, org_id=org_b, team_id=team_b.id, increment_count=3)
        await session.flush()

    # Org A sees only its counts
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        counts_a = await get_daily_run_counts(session, org_id=org_a)
        assert sum(c.run_count for c in counts_a) == 5

    # Org B sees only its counts
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        counts_b = await get_daily_run_counts(session, org_id=org_b)
        assert sum(c.run_count for c in counts_b) == 3
