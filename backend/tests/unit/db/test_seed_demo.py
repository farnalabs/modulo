"""Unit tests for the FAR-535 demo auto-login seed (modulo.db.seed_demo).

Hermetic in-memory SQLite (mirrors tests/unit/db/test_seed.py — no Postgres
required). Locks the seed contract: env-gated no-op when the demo trio is not
fully configured, idempotent entity creation (org, account, viewer membership,
2 published schemas, 1 pipeline + snapshot, deterministic terminal runs 1-2),
password re-stamping on MODULO_DEMO_PASSWORD rotation (including a corrupt
stored hash re-stamping instead of crashing the seed), role/system-admin/
must_change_password drift reset, the drift warning reporting the role captured
BEFORE the overwrite, demo-org scoping of every seeded entity even with a
second (non-demo) organisation present, the seed_demo_runtime wrapper
running the seed in its own transaction on a caller-provided session factory,
deterministic recovery when multiple soft-deleted 'demo'-slug orgs coexist
(Postgres partial-index reality — no MultipleResultsFound, undelete applied to
the chosen row), savepoint IntegrityError recovery for every sample-data
insert (concurrent-style duplicate ⇒ adopt the winner, seed still completes),
and failure-log sanitization: seed failures never surface SQLAlchemy bind
parameters (which embed the demo account's bcrypt password hash).
"""

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import modulo.db.seed_demo as seed_demo_module
from modulo.auth.passwords import hash_password, verify_password
from modulo.core.demo import DEMO_ORG_SLUG
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.trigger import Trigger
from modulo.db.seed_demo import seed_demo, seed_demo_runtime
from modulo.db.soft_delete import include_soft_deleted
from modulo.settings import Settings

_VALID_32 = "a" * 32
_DEMO_EMAIL = "demo@modulo.run"
_DEMO_PASSWORD = "demo-passphrase-123"

# Tables required by the seed incl. FK dependencies. Scoped create_all because
# unrelated models use Postgres-only column types SQLite cannot render.
_SEED_TABLES = {
    "organisations",
    "accounts",
    "org_memberships",
    "schemas",
    "schema_versions",
    "pipelines",
    "pipeline_snapshots",
    "runs",
    "teams",
    "team_memberships",
    "agents",
    "triggers",
    "run_daily_facts",
}


def _demo_settings(*, enabled: bool = True, user: str = _DEMO_EMAIL, password: str = _DEMO_PASSWORD) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_demo_enabled=enabled,
        modulo_demo_user=user,
        modulo_demo_password=password,
        modulo_multi_org_enabled=True,
    )


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        wanted = [t for t in Base.metadata.sorted_tables if t.name in _SEED_TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=wanted))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _run_seed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> str | None:
    monkeypatch.setattr(seed_demo_module, "get_settings", lambda: settings)
    # Reads between seeds autobegin an implicit transaction on the shared
    # session; end it so the explicit seed transaction can start cleanly.
    if session.in_transaction():
        await session.rollback()
    async with session.begin():
        return await seed_demo(session)


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _orgs(session: AsyncSession) -> list[Organisation]:
    return list((await session.execute(select(Organisation))).scalars())


@pytest.mark.parametrize(
    ("enabled", "user", "password"),
    [
        pytest.param(False, _DEMO_EMAIL, _DEMO_PASSWORD, id="disabled"),
        pytest.param(True, "", _DEMO_PASSWORD, id="user-empty"),
        pytest.param(True, _DEMO_EMAIL, "", id="password-empty"),
    ],
)
async def test_seed_disabled_is_a_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, enabled: bool, user: str, password: str
) -> None:
    summary = await _run_seed(session, monkeypatch, _demo_settings(enabled=enabled, user=user, password=password))
    assert summary is None
    assert await _count(session, Organisation) == 0
    assert await _count(session, Account) == 0


async def test_seed_creates_demo_entities(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = await _run_seed(session, monkeypatch, _demo_settings())
    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"

    assert await _count(session, Organisation) == 1
    assert await _count(session, Account) == 1
    assert await _count(session, OrgMembership) == 1
    # FAR-977: expanded to 5 schemas (Demo Intake, Demo Report,
    # GitHub Pull Request, Linear Issue, Release Notes).
    assert await _count(session, Schema) == 5
    assert await _count(session, SchemaVersion) == 5
    # FAR-977: 4 pipelines (original + PR Review & Triage + Release Notes Generator + Docs Sync).
    assert await _count(session, Pipeline) == 4
    assert await _count(session, PipelineSnapshot) == 4
    # FAR-977: 20 runs spread over 14 days.
    assert await _count(session, Run) == 20
    # FAR-977: team + membership.
    assert await _count(session, Team) == 1
    assert await _count(session, TeamMembership) == 1
    # FAR-977: 2 agents.
    assert await _count(session, Agent) == 2
    # FAR-977: 2 triggers (webhook + cron).
    assert await _count(session, Trigger) == 2

    org = (await _orgs(session))[0]
    assert org.slug == DEMO_ORG_SLUG

    account = (await session.execute(select(Account))).scalar_one()
    assert account.email == _DEMO_EMAIL
    assert account.is_system_admin is False
    assert account.active is True
    assert account.must_change_password is False

    membership = (await session.execute(select(OrgMembership))).scalar_one()
    assert membership.role == "viewer"
    assert membership.account_id == account.id
    assert membership.organisation_id == org.id

    published_versions = list(
        (await session.execute(select(SchemaVersion).where(SchemaVersion.published.is_(True)))).scalars()
    )
    assert len(published_versions) == 5

    runs = list((await session.execute(select(Run))).scalars())
    assert len(runs) == 20
    statuses = {run.status for run in runs}
    assert "complete" in statuses
    assert "failed" in statuses
    assert "awaiting_human" in statuses

    # FAR-977: awaiting_human daily-fact parity — non-terminal runs must NOT
    # fabricate completed_at / duration_ms.  Deleting the is_terminal gating in
    # seed_demo.py must cause these assertions to fail.
    awaiting_run = (
        await session.execute(select(Run).where(Run.status == "awaiting_human", Run.run_number == 6))
    ).scalar_one()
    awaiting_fact = (
        await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == awaiting_run.id))
    ).scalar_one()
    assert awaiting_fact.completed_at is None, "non-terminal fact must not fabricate completed_at"
    assert awaiting_fact.duration_ms is None, "non-terminal fact must not fabricate duration_ms"

    # Discriminating check: a TERMINAL run's fact must carry non-null values
    # that match the Run row, so the assertion isn't vacuously all-None.
    terminal_run = (
        await session.execute(select(Run).where(Run.status == "complete", Run.run_number == 1))
    ).scalar_one()
    terminal_fact = (
        await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == terminal_run.id))
    ).scalar_one()
    assert terminal_fact.completed_at is not None, "terminal fact must have completed_at"
    assert terminal_fact.duration_ms is not None, "terminal fact must have duration_ms"
    assert terminal_fact.completed_at == terminal_run.completed_at
    assert terminal_fact.duration_ms == int(
        (terminal_run.completed_at - terminal_run.started_at).total_seconds() * 1000
    )


async def test_seed_is_idempotent(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run_seed(session, monkeypatch, _demo_settings())
    counts_first = {
        "orgs": await _count(session, Organisation),
        "accounts": await _count(session, Account),
        "memberships": await _count(session, OrgMembership),
        "schemas": await _count(session, Schema),
        "schema_versions": await _count(session, SchemaVersion),
        "pipelines": await _count(session, Pipeline),
        "snapshots": await _count(session, PipelineSnapshot),
        "runs": await _count(session, Run),
        "teams": await _count(session, Team),
        "team_memberships": await _count(session, TeamMembership),
        "agents": await _count(session, Agent),
        "triggers": await _count(session, Trigger),
        "run_daily_facts": await _count(session, RunDailyFact),
    }

    await _run_seed(session, monkeypatch, _demo_settings())

    counts_second = {
        "orgs": await _count(session, Organisation),
        "accounts": await _count(session, Account),
        "memberships": await _count(session, OrgMembership),
        "schemas": await _count(session, Schema),
        "schema_versions": await _count(session, SchemaVersion),
        "pipelines": await _count(session, Pipeline),
        "snapshots": await _count(session, PipelineSnapshot),
        "runs": await _count(session, Run),
        "teams": await _count(session, Team),
        "team_memberships": await _count(session, TeamMembership),
        "agents": await _count(session, Agent),
        "triggers": await _count(session, Trigger),
        "run_daily_facts": await _count(session, RunDailyFact),
    }
    assert counts_second == counts_first
    # FAR-977: 20 runs in the expanded seed.
    assert counts_second["runs"] == 20


async def test_seed_restamps_password_on_rotation(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run_seed(session, monkeypatch, _demo_settings())
    rotated = "rotated-demo-passphrase"
    await _run_seed(session, monkeypatch, _demo_settings(password=rotated))

    account = (await session.execute(select(Account))).scalar_one()
    assert verify_password(rotated, account.password_hash) is True
    assert verify_password(_DEMO_PASSWORD, account.password_hash) is False


async def test_seed_resets_role_and_system_admin_drift(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run_seed(session, monkeypatch, _demo_settings())

    account = (await session.execute(select(Account))).scalar_one()
    membership = (await session.execute(select(OrgMembership))).scalar_one()
    membership.role = "operator"
    account.is_system_admin = True
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    refreshed_account = (await session.execute(select(Account))).scalar_one()
    refreshed_membership = (await session.execute(select(OrgMembership))).scalar_one()
    assert refreshed_membership.role == "viewer"
    assert refreshed_account.is_system_admin is False


async def test_seed_scopes_entities_to_demo_org_with_second_org_present(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_account = Account(
        email="other@example.com",
        display_name="Other",
        password_hash=hash_password("other-passphrase-123"),
        auth_provider="local",
        active=True,
    )
    other_org = Organisation(name="Other", slug="other", settings_json={})
    session.add(other_account)
    session.add(other_org)
    await session.flush()
    other_pipeline = Pipeline(
        organisation_id=other_org.id,
        name="Other Org Pipeline",
        account_id=other_account.id,
        visibility="org",
    )
    session.add(other_pipeline)
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    orgs = await _orgs(session)
    orgs_by_slug = {org.slug: org for org in orgs}
    demo_org = orgs_by_slug[DEMO_ORG_SLUG]
    other = orgs_by_slug["other"]

    demo_pipeline = (
        await session.execute(select(Pipeline).where(Pipeline.name == "Demo Governance Pipeline"))
    ).scalar_one()
    assert demo_pipeline.organisation_id == demo_org.id
    assert demo_pipeline.organisation_id != other.id

    runs = list((await session.execute(select(Run))).scalars())
    assert {run.organisation_id for run in runs} == {demo_org.id}

    schemas = list((await session.execute(select(Schema))).scalars())
    assert {schema.organisation_id for schema in schemas} == {demo_org.id}

    other_pipeline = (await session.execute(select(Pipeline).where(Pipeline.name == "Other Org Pipeline"))).scalar_one()
    assert other_pipeline.organisation_id == other.id

    demo_account = (await session.execute(select(Account).where(Account.email == _DEMO_EMAIL))).scalar_one()
    demo_memberships = list(
        (await session.execute(select(OrgMembership).where(OrgMembership.account_id == demo_account.id))).scalars()
    )
    assert {membership.organisation_id for membership in demo_memberships} == {demo_org.id}

    # FAR-977: verify scoping for expanded entity types.
    agents = list((await session.execute(select(Agent))).scalars())
    assert agents, "seed must create demo agents"
    assert {a.organisation_id for a in agents} == {demo_org.id}

    triggers = list((await session.execute(select(Trigger))).scalars())
    assert triggers, "seed must create demo triggers"
    assert {t.organisation_id for t in triggers} == {demo_org.id}

    teams = list((await session.execute(select(Team))).scalars())
    assert teams, "seed must create demo team"
    assert {t.organisation_id for t in teams} == {demo_org.id}

    team_memberships = list((await session.execute(select(TeamMembership))).scalars())
    assert team_memberships, "seed must create demo team membership"
    assert {tm.organisation_id for tm in team_memberships} == {demo_org.id}

    daily_facts = list((await session.execute(select(RunDailyFact))).scalars())
    assert daily_facts, "seed must create run daily facts"
    assert {f.organisation_id for f in daily_facts} == {demo_org.id}

    snapshots = list((await session.execute(select(PipelineSnapshot))).scalars())
    assert snapshots, "seed must create pipeline snapshots"
    assert {s.organisation_id for s in snapshots} == {demo_org.id}


async def test_seed_logs_previous_role_on_drift_reset(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The role-drift warning must report the role captured BEFORE the overwrite."""
    await _run_seed(session, monkeypatch, _demo_settings())
    membership = (await session.execute(select(OrgMembership))).scalar_one()
    membership.role = "operator"
    await session.commit()

    with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
        await _run_seed(session, monkeypatch, _demo_settings())

    reset_records = [record for record in caplog.records if record.getMessage() == "demo_seed.membership_role_reset"]
    assert len(reset_records) == 1
    assert reset_records[0].previous_role == "operator"
    assert reset_records[0].role == "viewer"
    assert reset_records[0].email == _DEMO_EMAIL

    refreshed = (await session.execute(select(OrgMembership))).scalar_one()
    assert refreshed.role == "viewer"


async def test_seed_restamps_corrupt_password_hash(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed stored hash re-stamps from env instead of crashing the seed."""
    await _run_seed(session, monkeypatch, _demo_settings())
    account = (await session.execute(select(Account))).scalar_one()
    account.password_hash = "not-a-valid-bcrypt-hash"
    await session.commit()

    summary = await _run_seed(session, monkeypatch, _demo_settings())

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    repaired = (await session.execute(select(Account))).scalar_one()
    assert verify_password(_DEMO_PASSWORD, repaired.password_hash) is True


async def test_seed_resets_must_change_password_drift(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing account flagged must_change_password is reset to False.

    Otherwise the demo viewer would be trapped in ForceChangePasswordView,
    whose mutation is viewer-denied — an unusable demo session.
    """
    await _run_seed(session, monkeypatch, _demo_settings())
    account = (await session.execute(select(Account))).scalar_one()
    account.must_change_password = True
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    refreshed = (await session.execute(select(Account))).scalar_one()
    assert refreshed.must_change_password is False


async def test_seed_demo_runtime_uses_provided_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_demo_runtime runs the seed in its own transaction on the given factory.

    main.py's boot lifespan passes its DI engine-backed factory; the wrapper
    must own the transaction so both callers share one engine path each.
    """
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    try:
        async with eng.begin() as conn:
            wanted = [t for t in Base.metadata.sorted_tables if t.name in _SEED_TABLES]
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=wanted))
        maker = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(seed_demo_module, "get_settings", lambda: _demo_settings())

        summary = await seed_demo_runtime(session_factory=maker)

        assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
        async with maker() as check:
            assert await _count(check, Organisation) == 1
            assert await _count(check, Account) == 1
            assert await _count(check, Run) == 20

        summary_again = await seed_demo_runtime(session_factory=maker)

        assert summary_again == summary
        async with maker() as check:
            assert await _count(check, Organisation) == 1
            assert await _count(check, Account) == 1
            assert await _count(check, Run) == 20
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# Concurrency simulations (qa-iterate iteration 2): soft-deleted duplicate
# slugs and savepoint IntegrityError recovery.
# ---------------------------------------------------------------------------


class _EmptyResult:
    """Result stand-in whose lookups all answer 'no row'."""

    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> "_EmptyResult":
        return self

    def first(self) -> None:
        return None

    def all(self) -> list[object]:
        return []


class _HideCheckOnce:
    """Hide ONE existence-check for ``entity`` from the seed's SELECT.

    Simulates a concurrent boot whose winner row commits AFTER the seed's
    existence check ran: the check misses it, so the seed proceeds to INSERT
    and (where a unique constraint exists) hits the real violation.
    """

    def __init__(self, session: AsyncSession, entity: type) -> None:
        self._session = session
        self._entity = entity
        self._pending = True
        self._real_execute = session.execute

    def _is_check(self, stmt: object) -> bool:
        descriptions = getattr(stmt, "column_descriptions", None)
        if not descriptions:
            return False
        return descriptions[0].get("entity") is self._entity

    async def _intercepted(self, stmt: object, *args: object) -> object:
        if self._pending and self._is_check(stmt):
            self._pending = False
            return _EmptyResult()
        return await self._real_execute(stmt, *args)  # type: ignore[arg-type]

    def install(self) -> None:
        self._session.execute = self._intercepted  # type: ignore[method-assign]

    def uninstall(self) -> None:
        self._session.execute = self._real_execute  # type: ignore[method-assign]


class _FlakyFlush:
    """Raise IntegrityError once on a flush that persists an ``entity`` row.

    Simulates the unique-constraint failure itself for entities whose natural
    key has no DB constraint in the SQLite test schema (e.g. pipelines), so
    the recovery branch can be exercised uniformly.
    """

    def __init__(self, session: AsyncSession, entity: type) -> None:
        self._session = session
        self._entity = entity
        self._real_flush = session.flush

    async def _intercepted(self) -> object:
        if any(isinstance(obj, self._entity) for obj in self._session.new):
            raise IntegrityError("simulated concurrent duplicate", None, Exception("uq_conflict"))
        return await self._real_flush()

    def install(self) -> None:
        self._session.flush = self._intercepted  # type: ignore[method-assign]

    def uninstall(self) -> None:
        self._session.flush = self._real_flush  # type: ignore[method-assign]


class _HideAllChecks:
    """Hide EVERY existence/recovery SELECT for ``entity`` from the seed.

    Unlike :class:`_HideCheckOnce`, the interception never burns out, so the
    post-IntegrityError recovery re-select also misses. That drives the seed's
    defensive ``if winner is None: raise`` branch — the path taken when a
    concurrent boot deletes the winner between the failed insert and the
    recovery lookup.
    """

    def __init__(self, session: AsyncSession, entity: type) -> None:
        self._session = session
        self._entity = entity
        self._real_execute = session.execute

    def _is_check(self, stmt: object) -> bool:
        descriptions = getattr(stmt, "column_descriptions", None)
        if not descriptions:
            return False
        return descriptions[0].get("entity") is self._entity

    async def _intercepted(self, stmt: object, *args: object) -> object:
        if self._is_check(stmt):
            return _EmptyResult()
        return await self._real_execute(stmt, *args)  # type: ignore[arg-type]

    def install(self) -> None:
        self._session.execute = self._intercepted  # type: ignore[method-assign]

    def uninstall(self) -> None:
        self._session.execute = self._real_execute  # type: ignore[method-assign]


class _ExplodingFlush:
    """Raise a non-IntegrityError once when persisting an ``entity`` row.

    Exercises the seed's broad ``except Exception`` swallow path, distinct from
    the IntegrityError recovery path the other flush helper drives.
    """

    def __init__(self, session: AsyncSession, entity: type) -> None:
        self._session = session
        self._entity = entity
        self._real_flush = session.flush

    async def _intercepted(self) -> object:
        if any(isinstance(obj, self._entity) for obj in self._session.new):
            raise RuntimeError("simulated unexpected write failure")
        return await self._real_flush()

    def install(self) -> None:
        self._session.flush = self._intercepted  # type: ignore[method-assign]

    def uninstall(self) -> None:
        self._session.flush = self._real_flush  # type: ignore[method-assign]


async def test_seed_adopts_winner_after_real_slug_conflict(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Org insert hitting the live slug unique index recovers by adoption."""
    await _run_seed(session, monkeypatch, _demo_settings())
    hide = _HideCheckOnce(session, Organisation)
    hide.install()
    try:
        summary = await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        hide.uninstall()

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, Organisation) == 1
    org = (await session.execute(select(Organisation))).scalar_one()
    assert org.slug == DEMO_ORG_SLUG
    assert org.deleted_at is None


async def test_seed_org_recovery_applies_undelete_repair(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IntegrityError recovery path undeletes a soft-deleted winner too."""
    await _run_seed(session, monkeypatch, _demo_settings())
    org = (await session.execute(select(Organisation))).scalar_one()
    org.deleted_at = datetime.now(UTC)
    await session.commit()

    hide = _HideCheckOnce(session, Organisation)
    hide.install()
    try:
        summary = await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        hide.uninstall()

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, Organisation) == 1
    recovered = (await session.execute(select(Organisation))).scalar_one()
    assert recovered.id == org.id
    assert recovered.deleted_at is None


async def test_seed_handles_multiple_soft_deleted_demo_slug_orgs(
    session: AsyncSession, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two soft-deleted 'demo' orgs: deterministic pick, no MultipleResultsFound.

    On Postgres the slug uniqueness is a PARTIAL index (deleted_at IS NULL), so
    several soft-deleted 'demo' rows coexist and a bare scalar_one_or_none
    would raise MultipleResultsFound every boot. SQLite renders the index as
    FULL unique (postgresql_where is Postgres-only), so the fixture drops it
    to emulate the partial-index reality.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX IF EXISTS uq_organisations_slug")
    now = datetime.now(UTC)
    older = Organisation(name="Demo", slug=DEMO_ORG_SLUG, settings_json={})
    newer = Organisation(name="Demo", slug=DEMO_ORG_SLUG, settings_json={})
    older.created_at = now - timedelta(hours=2)
    older.deleted_at = now - timedelta(hours=2)
    newer.created_at = now - timedelta(hours=1)
    newer.deleted_at = now - timedelta(hours=1)
    session.add_all([older, newer])
    await session.commit()

    summary = await _run_seed(session, monkeypatch, _demo_settings())

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    demo_orgs = list(
        (
            await session.execute(include_soft_deleted(select(Organisation).where(Organisation.slug == DEMO_ORG_SLUG)))
        ).scalars()
    )
    assert len(demo_orgs) == 2
    live = [org for org in demo_orgs if org.deleted_at is None]
    assert len(live) == 1
    assert live[0].id == newer.id
    membership = (await session.execute(select(OrgMembership))).scalar_one()
    assert membership.organisation_id == newer.id


@pytest.mark.parametrize(
    ("entity", "count_model"),
    [
        pytest.param(Schema, Schema, id="schema"),
        pytest.param(SchemaVersion, SchemaVersion, id="schema-version"),
        pytest.param(Pipeline, Pipeline, id="pipeline"),
        pytest.param(PipelineSnapshot, PipelineSnapshot, id="snapshot"),
        pytest.param(Run, Run, id="run"),
        pytest.param(Agent, Agent, id="agent"),
        pytest.param(Trigger, Trigger, id="trigger"),
        pytest.param(Team, Team, id="team"),
        pytest.param(TeamMembership, TeamMembership, id="team-membership"),
    ],
)
async def test_seed_sample_data_inserts_recover_after_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    entity: type,
    count_model: type,
) -> None:
    """Sample-data inserts adopt the concurrent winner and still complete.

    Pre-seeds normally, then re-runs the seed with the entity's existence
    check hidden once and its insert flushed into a simulated unique
    violation — the concurrent-boot race. The savepoint must roll back only
    the losing insert; the seed adopts the winner row and finishes with
    unchanged entity counts (zero duplicates, zero failures).
    """
    await _run_seed(session, monkeypatch, _demo_settings())
    counts_before = await _count(session, count_model)

    hide = _HideCheckOnce(session, entity)
    flaky = _FlakyFlush(session, entity)
    hide.install()
    flaky.install()
    try:
        summary = await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        flaky.uninstall()
        hide.uninstall()

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, count_model) == counts_before


@pytest.mark.parametrize("entity", [Pipeline, PipelineSnapshot, Run, Agent, Team])
async def test_seed_reraises_when_recovery_finds_no_winner(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, entity: type
) -> None:
    """Recovery re-raises the IntegrityError when the winner vanished too.

    The savepoint rolls back the losing insert, then the recovery re-select
    (hidden here) comes back empty — a second concurrent boot deleted the
    winner in the window between the two. The seed must surface the original
    failure rather than continue with a missing row.
    """
    hide = _HideAllChecks(session, entity)
    flaky = _FlakyFlush(session, entity)
    hide.install()
    flaky.install()
    try:
        with pytest.raises(IntegrityError, match="simulated concurrent duplicate"):
            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        flaky.uninstall()
        hide.uninstall()


async def test_seed_run_spec_unknown_pipeline_is_skipped(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run spec naming a pipeline absent from the lookup warns and skips."""
    extra_spec = (999, "complete", "manual", "Nonexistent Pipeline", 100, 0.001, 0, 1)
    monkeypatch.setattr(seed_demo_module, "_DEMO_RUN_SPECS", [*seed_demo_module._DEMO_RUN_SPECS, extra_spec])

    with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
        summary = await _run_seed(session, monkeypatch, _demo_settings())

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, Run) == 20
    assert any(record.getMessage() == "demo_seed.run_spec_unknown_pipeline" for record in caplog.records)


async def test_seed_run_node_output_failure_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A best-effort node-output write failure must not abort the run insert."""
    calls: list[str] = []

    async def _explode(*args: object, **kwargs: object) -> None:
        calls.append("called")
        raise RuntimeError("simulated node-output write failure")

    monkeypatch.setattr("modulo.db.crud.run_node_outputs.replace_run_node_outputs", _explode)

    with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
        summary = await _run_seed(session, monkeypatch, _demo_settings())

    assert calls, "seed must attempt to write node outputs"
    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, Run) == 20
    assert any(record.getMessage().startswith("demo_seed.run_outputs_write_failed") for record in caplog.records)


async def test_seed_daily_fact_integrity_conflict_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A RunDailyFact unique conflict is swallowed; the Run rows still land."""
    flaky = _FlakyFlush(session, RunDailyFact)
    flaky.install()
    try:
        with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
            summary = await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        flaky.uninstall()

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, Run) == 20
    assert await _count(session, RunDailyFact) == 0
    assert any(record.getMessage() == "demo_seed.daily_fact_recovered_after_conflict" for record in caplog.records)


async def test_seed_daily_fact_generic_failure_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-unique RunDailyFact write failure is swallowed, not fatal."""
    exploding = _ExplodingFlush(session, RunDailyFact)
    exploding.install()
    try:
        with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
            summary = await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        exploding.uninstall()

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    assert await _count(session, Run) == 20
    assert await _count(session, RunDailyFact) == 0
    assert any(record.getMessage().startswith("demo_seed.daily_fact_write_failed") for record in caplog.records)


async def test_seed_cron_trigger_recovers_after_conflict(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The cron trigger's savepoint recovery path is exercised on conflict."""
    await _run_seed(session, monkeypatch, _demo_settings())
    cron = (await session.execute(select(Trigger).where(Trigger.trigger_type == "cron"))).scalar_one()
    await session.delete(cron)
    await session.commit()

    flaky = _FlakyFlush(session, Trigger)
    flaky.install()
    try:
        with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
            summary = await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        flaky.uninstall()

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    # The webhook trigger already exists, so only the deleted cron hits the
    # insert -> conflict -> recovery-log branch.
    assert await _count(session, Trigger) == 1
    assert any(record.getMessage() == "demo_seed.cron_trigger_recovered" for record in caplog.records)


async def test_seed_stray_membership_warning_ignores_soft_deleted_orgs(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Soft-deleted other-org memberships must not inflate other_org_count."""
    other_account = Account(
        email="other@example.com",
        display_name="Other",
        password_hash=hash_password("other-passphrase-123"),
        auth_provider="local",
        active=True,
    )
    doomed_org = Organisation(name="Doomed", slug="doomed", settings_json={})
    session.add_all([other_account, doomed_org])
    await session.flush()
    session.add(
        OrgMembership(
            account_id=other_account.id,
            organisation_id=doomed_org.id,
            role="viewer",
        )
    )
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())
    # The demo account picks up a stray membership, then that org is
    # soft-deleted — a re-seed must not warn about the dead org.
    stray_org_id = doomed_org.id
    demo_account = (await session.execute(select(Account).where(Account.email == _DEMO_EMAIL))).scalar_one()
    session.add(OrgMembership(account_id=demo_account.id, organisation_id=stray_org_id, role="viewer"))
    await session.commit()
    doomed_org.deleted_at = datetime.now(UTC)
    await session.commit()

    with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
        await _run_seed(session, monkeypatch, _demo_settings())

    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "demo_seed.account_has_memberships_outside_demo_org"
    ]
    assert not warnings


# ---------------------------------------------------------------------------
# Failure-log sanitization (verification round): SQLAlchemy DBAPIError /
# StatementError str() and repr() embed the failed statement's bind parameters
# as "[parameters: (...)]" — for the demo account INSERT those include the
# bcrypt password_hash. Every seed-failure log/print surface must carry the
# sanitized text only.
# ---------------------------------------------------------------------------

_HASH_MARKER = "<bcrypt-hash>"


def _leaking_integrity_error() -> IntegrityError:
    """An IntegrityError whose str()/repr() embed a [parameters: (...)] section.

    Mirrors the real failure shape: SQLAlchemy renders the failed statement's
    bind params, and the demo account INSERT binds the bcrypt password hash.
    """
    return IntegrityError(
        "INSERT INTO accounts (email, password_hash) VALUES ($1, $2)",
        (_DEMO_EMAIL, _HASH_MARKER),
        Exception("duplicate key value violates unique constraint"),
    )


def test_safe_exc_text_strips_bind_parameters() -> None:
    """The sanitizer keeps type + message but removes the [parameters:] section."""
    exc = _leaking_integrity_error()
    raw = str(exc)
    # Discriminating pre-check: the fixture genuinely leaks, so the stripped
    # assertions below cannot pass vacuously.
    assert _HASH_MARKER in raw
    assert "[parameters:" in raw

    text = seed_demo_module._safe_exc_text(exc)

    assert _HASH_MARKER not in text
    assert "[parameters:" not in text
    assert text.startswith("IntegrityError:")


def test_safe_exc_text_handles_plain_exceptions() -> None:
    """A non-SQLAlchemy failure passes through with type + message intact."""
    text = seed_demo_module._safe_exc_text(ValueError("boom"))

    assert text == "ValueError: boom"


def test_seed_failure_log_and_print_never_leak_bind_parameters(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A seed failure surfaces sanitized text only — no hash, no [parameters:.

    Exercises the standalone entry (main): both the structured log record and
    the stdout FAILED line must carry the sanitized text; the raw exception
    (whose str embeds the hash) must never appear on any surface. Deliberately
    sync: main() drives the seed via asyncio.run(), which cannot run inside
    this suite's event loop.
    """
    settings = _demo_settings()
    monkeypatch.setattr(seed_demo_module, "get_settings", lambda: settings)

    async def _failing_runtime() -> None:
        raise _leaking_integrity_error()

    monkeypatch.setattr(seed_demo_module, "seed_demo_runtime", _failing_runtime)

    with caplog.at_level(logging.ERROR, logger="modulo.db.seed_demo"), pytest.raises(SystemExit) as excinfo:
        seed_demo_module.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    log_text = "\n".join(f"{record.getMessage()} {getattr(record, 'error', '')}" for record in caplog.records)
    combined = captured.out + captured.err + log_text
    assert _HASH_MARKER not in combined
    assert "[parameters:" not in combined
    # Still diagnosable: exception type + message survive the sanitization.
    assert "IntegrityError" in combined
    assert "demo_seed.failed" in log_text


# ---------------------------------------------------------------------------
# Convergence tests (FAR-977 Part 2): exercise the update-existing-row paths.
# Each test modifies an entity after the first seed, re-seeds, and verifies
# the seed converged the row to the current spec.
# ---------------------------------------------------------------------------


async def test_seed_converges_pipeline_description_and_graph(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline description and graph_nodes_json are converged to the spec."""
    await _run_seed(session, monkeypatch, _demo_settings())

    pipeline = (await session.execute(select(Pipeline).where(Pipeline.name == "Demo Governance Pipeline"))).scalar_one()
    # Mutate description + graph away from spec.
    pipeline.description = "stale description"
    pipeline.graph_nodes_json = [{"id": "stale"}]
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged = (
        await session.execute(select(Pipeline).where(Pipeline.name == "Demo Governance Pipeline"))
    ).scalar_one()
    assert converged.description == "Demo sample pipeline — read-only demo data (FAR-535)."
    # graph_nodes_json is the 4-node demo pipeline graph.
    assert len(converged.graph_nodes_json) == 4  # type: ignore[arg-type]


async def test_seed_converges_snapshot_graph_json(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """PipelineSnapshot graph_json is converged to the current pipeline spec."""
    await _run_seed(session, monkeypatch, _demo_settings())

    pipeline = (await session.execute(select(Pipeline).where(Pipeline.name == "Demo Governance Pipeline"))).scalar_one()
    snapshot = (
        await session.execute(
            select(PipelineSnapshot).where(
                PipelineSnapshot.pipeline_id == pipeline.id,
                PipelineSnapshot.snapshot_version == 1,
            )
        )
    ).scalar_one()
    snapshot.graph_json = {"nodes": [], "edges": []}
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged_snapshot = (
        await session.execute(
            select(PipelineSnapshot).where(
                PipelineSnapshot.pipeline_id == pipeline.id,
                PipelineSnapshot.snapshot_version == 1,
            )
        )
    ).scalar_one()
    assert converged_snapshot.graph_json != {"nodes": [], "edges": []}
    assert len(converged_snapshot.graph_json["nodes"]) == 4  # type: ignore[index]


async def test_seed_converges_schema_description_and_definition(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema description and SchemaVersion definition_json are converged."""
    await _run_seed(session, monkeypatch, _demo_settings())

    schema = (await session.execute(select(Schema).where(Schema.name == "Demo Intake"))).scalar_one()
    schema.description = "stale schema desc"
    version = (
        await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema.id,
                SchemaVersion.version == "v1",
            )
        )
    ).scalar_one()
    version.definition_json = {"type": "object", "properties": {}}
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged = (await session.execute(select(Schema).where(Schema.name == "Demo Intake"))).scalar_one()
    assert converged.description == "Demo sample: intake payload"
    converged_version = (
        await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == converged.id,
                SchemaVersion.version == "v1",
            )
        )
    ).scalar_one()
    assert converged_version.definition_json == {
        "type": "object",
        "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["title"],
    }


async def test_seed_converges_agent_description_and_prompt(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent description and prompt_template are converged to spec."""
    await _run_seed(session, monkeypatch, _demo_settings())

    agent = (await session.execute(select(Agent).where(Agent.name == "AI Code Reviewer"))).scalar_one()
    agent.description = "stale agent desc"
    agent.prompt_template = "stale prompt"
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged = (await session.execute(select(Agent).where(Agent.name == "AI Code Reviewer"))).scalar_one()
    assert "Analyses code changes" in converged.description
    assert "code reviewer" in converged.prompt_template


async def test_seed_converges_trigger_config_json(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Trigger config_json is converged to the current spec."""
    await _run_seed(session, monkeypatch, _demo_settings())

    webhook_trigger = (await session.execute(select(Trigger).where(Trigger.trigger_type == "webhook"))).scalar_one()
    webhook_trigger.config_json = {"stale": True}
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged = (await session.execute(select(Trigger).where(Trigger.trigger_type == "webhook"))).scalar_one()
    assert converged.config_json == {
        "events": ["pull_request"],
        "payload_mapping": {},
    }


async def test_seed_converges_cron_trigger_config_json(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cron trigger config_json is converged to the current spec."""
    await _run_seed(session, monkeypatch, _demo_settings())

    cron_trigger = (await session.execute(select(Trigger).where(Trigger.trigger_type == "cron"))).scalar_one()
    cron_trigger.config_json = {"stale": True}
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged = (await session.execute(select(Trigger).where(Trigger.trigger_type == "cron"))).scalar_one()
    assert converged.config_json == {"description": "Weekly release notes generation"}


async def test_seed_backfills_missing_daily_fact_for_existing_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing RunDailyFact for an existing run is backfilled on re-seed."""
    await _run_seed(session, monkeypatch, _demo_settings())

    run = (await session.execute(select(Run).where(Run.run_number == 1))).scalar_one()
    # Delete the daily fact for run 1.
    fact = (await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run.id))).scalar_one()
    await session.delete(fact)
    await session.commit()

    # Verify it's gone.
    assert await _count(session, RunDailyFact) == 19

    await _run_seed(session, monkeypatch, _demo_settings())

    # The fact should be backfilled.
    assert await _count(session, RunDailyFact) == 20
    backfilled = (await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run.id))).scalar_one()
    assert backfilled.status == "complete"
    assert backfilled.run_number == 1


async def test_seed_converges_run_display_fields(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing runs' display fields are converged to the spec."""
    await _run_seed(session, monkeypatch, _demo_settings())

    run = (await session.execute(select(Run).where(Run.run_number == 1))).scalar_one()
    # Mutate display fields away from spec — use valid status values.
    run.status = "failed"
    run.total_tokens = 0
    run.total_cost_usd = Decimal(0)
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    converged = (await session.execute(select(Run).where(Run.run_number == 1))).scalar_one()
    assert converged.status == "complete"
    assert converged.total_tokens == 1840
    assert converged.total_cost_usd == Decimal("0.0042")


async def test_seed_logs_convergence(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-seeding with changed entities logs convergence events."""
    await _run_seed(session, monkeypatch, _demo_settings())

    # Mutate pipeline to trigger convergence.
    pipeline = (await session.execute(select(Pipeline).where(Pipeline.name == "Demo Governance Pipeline"))).scalar_one()
    pipeline.description = "stale"
    await session.commit()

    with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
        await _run_seed(session, monkeypatch, _demo_settings())

    converged_msgs = [r for r in caplog.records if "converged" in r.getMessage()]
    assert len(converged_msgs) >= 1


async def test_seed_does_not_touch_non_demo_org(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Convergence only affects the demo org, never a second org."""
    other_account = Account(
        email="other@example.com",
        display_name="Other",
        password_hash=hash_password("other-passphrase-123"),
        auth_provider="local",
        active=True,
    )
    other_org = Organisation(name="Other", slug="other", settings_json={})
    session.add(other_account)
    session.add(other_org)
    await session.flush()
    other_pipeline = Pipeline(
        organisation_id=other_org.id,
        name="Other Pipeline",
        description="original",
        account_id=other_account.id,
        visibility="org",
        graph_nodes_json=[],
        default_autonomy_level="manual_approval",
    )
    session.add(other_pipeline)
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    other = (await session.execute(select(Pipeline).where(Pipeline.name == "Other Pipeline"))).scalar_one()
    assert other.description == "original"


async def test_seed_converges_pipeline_idempotent_on_second_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Second seed with no mutations only logs time-dependent run convergence, not entity convergence."""
    await _run_seed(session, monkeypatch, _demo_settings())

    with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
        await _run_seed(session, monkeypatch, _demo_settings())

    # Only run_converged (timestamps drift) should appear — no pipeline/snapshot/schema/agent/trigger convergence.
    entity_converged = [
        r for r in caplog.records if "converged" in r.getMessage() and "run_converged" not in r.getMessage()
    ]
    assert not entity_converged


# ---------------------------------------------------------------------------
# Exception handler coverage: exercise the best-effort try/except paths.
# ---------------------------------------------------------------------------


async def test_seed_run_outputs_write_failure_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When replace_run_node_outputs raises, the seed logs a warning and continues."""
    await _run_seed(session, monkeypatch, _demo_settings())

    # The function is lazily imported inside seed_demo, so we mock it at its
    # source module path.
    async def _explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("node outputs write exploded")

    monkeypatch.setattr(
        "modulo.db.crud.run_node_outputs.replace_run_node_outputs",
        _explode,
    )

    with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
        # Delete a run to force fresh creation through the node-outputs path.
        run = (await session.execute(select(Run).where(Run.run_number == 1))).scalar_one()
        await session.delete(run)
        await session.commit()

        await _run_seed(session, monkeypatch, _demo_settings())

    warnings = [r for r in caplog.records if "run_outputs_write_failed" in r.getMessage()]
    assert warnings, "Expected run_outputs_write_failed warning"


async def test_seed_daily_fact_integrity_error_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When RunDailyFact insert hits IntegrityError, the seed logs and continues."""
    await _run_seed(session, monkeypatch, _demo_settings())

    # Force the daily-fact insert path to raise IntegrityError by making
    # the session.flush raise when RunDailyFact is pending.
    real_flush = session.flush

    async def _fact_integrity_flush() -> None:
        pending_types = {type(obj) for obj in session.new}
        if RunDailyFact in pending_types:
            raise IntegrityError("simulated PK conflict", None, Exception("uq_conflict"))
        return await real_flush()

    session.flush = _fact_integrity_flush  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
            # Delete all daily facts so the seed tries to re-create them.
            facts = list((await session.execute(select(RunDailyFact))).scalars())
            for f in facts:
                await session.delete(f)
            await session.commit()

            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        session.flush = real_flush  # type: ignore[method-assign]

    recovered = [r for r in caplog.records if "daily_fact_recovered_after_conflict" in r.getMessage()]
    assert recovered, "Expected daily_fact_recovered_after_conflict log"


async def test_seed_daily_fact_generic_exception_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When RunDailyFact creation raises a non-IntegrityError, it's logged and swallowed."""
    await _run_seed(session, monkeypatch, _demo_settings())

    real_flush = session.flush

    async def _fact_generic_flush() -> None:
        pending_types = {type(obj) for obj in session.new}
        if RunDailyFact in pending_types:
            raise RuntimeError("daily fact write exploded")
        return await real_flush()

    session.flush = _fact_generic_flush  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
            facts = list((await session.execute(select(RunDailyFact))).scalars())
            for f in facts:
                await session.delete(f)
            await session.commit()

            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        session.flush = real_flush  # type: ignore[method-assign]

    warnings = [r for r in caplog.records if "daily_fact_write_failed" in r.getMessage()]
    assert warnings, "Expected daily_fact_write_failed warning"


# ---------------------------------------------------------------------------
# Lines 662, 666: unknown-pipeline skip in the new-run creation path.
# ---------------------------------------------------------------------------


async def test_seed_skips_run_for_unknown_pipeline(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run spec referencing a pipeline not in the lookup is skipped with a warning."""
    await _run_seed(session, monkeypatch, _demo_settings())
    original_specs = seed_demo_module._DEMO_RUN_SPECS
    # Inject a spec with an unknown pipeline name.
    augmented = [
        *original_specs,
        (99, "complete", "manual", "Nonexistent Pipeline", 100, 0.001, 0, 1),
    ]
    monkeypatch.setattr(seed_demo_module, "_DEMO_RUN_SPECS", augmented)
    try:
        with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        monkeypatch.setattr(seed_demo_module, "_DEMO_RUN_SPECS", original_specs)

    warnings = [r for r in caplog.records if "run_spec_unknown_pipeline" in r.getMessage()]
    assert warnings, "Expected run_spec_unknown_pipeline warning"
    # The unknown-pipeline run must not have been created.
    assert await _count(session, Run) == 20


# ---------------------------------------------------------------------------
# Lines 754-757: daily-fact handlers in the NEW-run creation path.
# ---------------------------------------------------------------------------


async def test_seed_new_run_daily_fact_integrity_error_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When a newly created run's daily-fact insert hits IntegrityError, it's logged and swallowed."""
    await _run_seed(session, monkeypatch, _demo_settings())

    # Delete run 1 so re-seed must create it fresh, then make the daily-fact
    # flush raise IntegrityError to hit lines 754-755.
    real_flush = session.flush

    async def _fact_integrity_flush() -> None:
        pending_types = {type(obj) for obj in session.new}
        if RunDailyFact in pending_types:
            raise IntegrityError("simulated PK conflict", None, Exception("uq_conflict"))
        return await real_flush()

    session.flush = _fact_integrity_flush  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
            run = (await session.execute(select(Run).where(Run.run_number == 1))).scalar_one()
            await session.delete(run)
            await session.commit()

            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        session.flush = real_flush  # type: ignore[method-assign]

    recovered = [r for r in caplog.records if "daily_fact_recovered_after_conflict" in r.getMessage()]
    assert recovered, "Expected daily_fact_recovered_after_conflict log for new-run path"


async def test_seed_new_run_daily_fact_generic_exception_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When a newly created run's daily-fact write raises a generic error, it's logged and swallowed."""
    await _run_seed(session, monkeypatch, _demo_settings())

    real_flush = session.flush

    async def _fact_generic_flush() -> None:
        pending_types = {type(obj) for obj in session.new}
        if RunDailyFact in pending_types:
            raise RuntimeError("daily fact write exploded")
        return await real_flush()

    session.flush = _fact_generic_flush  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
            run = (await session.execute(select(Run).where(Run.run_number == 1))).scalar_one()
            await session.delete(run)
            await session.commit()

            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        session.flush = real_flush  # type: ignore[method-assign]

    warnings = [r for r in caplog.records if "daily_fact_write_failed" in r.getMessage()]
    assert warnings, "Expected daily_fact_write_failed warning for new-run path"


# ---------------------------------------------------------------------------
# Line 1009: team-membership IntegrityError handler.
# ---------------------------------------------------------------------------


async def test_seed_team_membership_integrity_error_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When a team-membership insert hits IntegrityError, the seed logs and continues."""
    await _run_seed(session, monkeypatch, _demo_settings())

    real_flush = session.flush

    async def _membership_integrity_flush() -> None:
        pending_types = {type(obj) for obj in session.new}
        if TeamMembership in pending_types:
            raise IntegrityError("simulated team membership conflict", None, Exception("uq"))
        return await real_flush()

    session.flush = _membership_integrity_flush  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.INFO, logger="modulo.db.seed_demo"):
            # Delete the team membership so the seed tries to re-create it.
            membership = (await session.execute(select(TeamMembership))).scalar_one()
            await session.delete(membership)
            await session.commit()

            await _run_seed(session, monkeypatch, _demo_settings())
    finally:
        session.flush = real_flush  # type: ignore[method-assign]

    recovered = [r for r in caplog.records if "team_membership_recovered_after_conflict" in r.getMessage()]
    assert recovered, "Expected team_membership_recovered_after_conflict log"
