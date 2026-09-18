"""Demo-seed org isolation integration test (FAR-535).

Proves the security boundary of the demo auto-login seed: every seeded entity
lives in the demo organisation, and a session scoped to the demo org (or to a
second, non-demo organisation) cannot read the other's rows through Postgres
RLS. Uses the real migrations + FORCE RLS surface from the shared integration
conftest, mirrors the role/enforcement pattern of test_rls_isolation.py, and
runs the REAL seed_demo against the migrated testcontainer.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.models.account import Account
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.seed_demo import seed_demo
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

_DEMO_EMAIL = "demo@modulo.run"
_DEMO_PASSWORD = "demo-passphrase-123"
_OTHER_EMAIL = "demo-rls-other@example.com"


async def test_demo_seed_org_isolation_under_rls(db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Demo-org rows and other-org rows are mutually invisible under RLS."""
    monkeypatch.setenv("MODULO_DEMO_ENABLED", "1")
    monkeypatch.setenv("MODULO_DEMO_USER", _DEMO_EMAIL)
    monkeypatch.setenv("MODULO_DEMO_PASSWORD", _DEMO_PASSWORD)
    get_settings.cache_clear()

    role = f"test_demo_rls_{uuid.uuid4().hex[:8]}"
    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}"'))
        # rls_team_isolation on pipelines/runs references teams/team_memberships
        # inside the policy expression — policy evaluation requires SELECT on
        # every table it reads, so the enforcement role needs those grants too.
        await conn.execute(text(f'GRANT SELECT ON pipelines, runs, teams, team_memberships TO "{role}"'))
        await conn.execute(text("COMMIT"))

    try:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # Real seed run (superuser connection: inserts bypass RLS, as in prod
        # where the boot seed runs in the system context).
        async with factory() as session, session.begin():
            summary = await seed_demo(session)
        assert summary == f"org=demo user={_DEMO_EMAIL}"

        # A second, non-demo organisation with its own pipeline.
        async with factory() as session, session.begin():
            other_account = Account(
                email=_OTHER_EMAIL,
                display_name="Other RLS",
                password_hash="x" * 60,
                auth_provider="local",
                active=True,
            )
            other_org = Organisation(name="Other RLS", slug=f"other-demo-rls-{uuid.uuid4().hex[:8]}", settings_json={})
            session.add(other_account)
            session.add(other_org)
            await session.flush()
            session.add(
                Pipeline(
                    organisation_id=other_org.id,
                    name="Other Org Pipeline",
                    account_id=other_account.id,
                    visibility="org",
                )
            )
            other_org_id = other_org.id

        async with factory() as session:
            demo_org = (await session.execute(select(Organisation).where(Organisation.slug == "demo"))).scalar_one()
            assert demo_org is not None
            demo_org_id = demo_org.id

        # Enforcement 1: demo-org context sees ONLY demo pipelines/runs.
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(demo_org_id)})
            pipeline_names = {row[0] for row in (await conn.execute(text("SELECT name FROM pipelines"))).fetchall()}
            run_numbers = {row[0] for row in (await conn.execute(text("SELECT run_number FROM runs"))).fetchall()}
        assert "Demo Governance Pipeline" in pipeline_names
        assert "Other Org Pipeline" not in pipeline_names
        assert run_numbers == {1, 2}

        # Enforcement 2: other-org context sees NONE of the demo rows.
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(other_org_id)})
            other_pipeline_names = {
                row[0] for row in (await conn.execute(text("SELECT name FROM pipelines"))).fetchall()
            }
            other_run_count = (await conn.execute(text("SELECT count(*) FROM runs"))).scalar_one()
        assert "Other Org Pipeline" in other_pipeline_names
        assert "Demo Governance Pipeline" not in other_pipeline_names
        assert other_run_count == 0

    finally:
        async with db_engine.connect() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            await conn.execute(text("COMMIT"))
