"""Integration test fixtures — spins up a real Postgres via Testcontainers."""

import asyncio
import contextlib
import gc
import json
import logging
import os
import sys
import uuid
import warnings
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

# psycopg (used by the LangGraph checkpointer via ModuloPostgresSaver) cannot
# run on Windows' default ProactorEventLoop. The integration session loop is
# created via asyncio.new_event_loop(), which honours this policy — set the
# Selector policy at import time so every integration session loop (and any
# full PipelineExecutor.execute / resume run) works on Windows too.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Collection imports the FastAPI app before database fixtures run. Provide only
# test-local defaults here so standalone collection never depends on a caller's
# shell environment. ``setdefault`` still lets CI supply explicit values.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/modulo_integration")
os.environ.setdefault("SECRET_KEY", "a" * 32)
# A valid Fernet key (32 base64-decoded bytes). ``"a" * 32`` is NOT a valid
# Fernet key — the executor's ModuloPostgresSaver constructs ``Fernet()`` from
# settings.fernet_key, and an invalid key raises ValueError, failing every run
# (the deploy gate failure). Must match what ci.yml sets at the workflow level.
os.environ.setdefault("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

BACKEND_ROOT = Path(__file__).parents[2]


# ---------------------------------------------------------------------------
# Docker availability detection — fails the session early when Docker is
# missing instead of letting every container fixture error individually.
# ---------------------------------------------------------------------------
def _docker_available() -> bool:
    """Return True if Docker is reachable via the testcontainers SDK."""
    try:
        from testcontainers.core.docker_client import DockerClient  # type: ignore[import-untyped]

        client = DockerClient()
        client.client.ping()
        return True
    except Exception as exc:
        import logging

        logging.getLogger(__name__).info(
            "Docker is not available: %s — integration tests will be skipped locally or fail in CI",
            exc,
        )
        return False


_DOCKER_AVAILABLE = _docker_available()

# How many integration items were skipped by the collection hook (set by
# pytest_collection_modifyitems, read by pytest_terminal_summary).
_skip_count = 0


def _ci_env_is_truthy() -> bool:
    """Return True when the ``CI`` env var is set to a truthy value."""
    ci = os.environ.get("CI", "").strip().lower()
    return ci not in ("", "0", "false")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_docker(reason): mark test as skipped when Docker is unavailable",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    global _skip_count

    if _DOCKER_AVAILABLE:
        return

    if _ci_env_is_truthy():
        pytest.exit(
            "FATAL: Docker is NOT available in CI — integration coverage cannot be "
            "silently skipped. Fix the Docker daemon / testcontainers environment.",
            returncode=1,
        )

    # Local: skip every integration test loudly.
    reason = (
        "SKIPPED: Docker is not available locally — integration tests require "
        "Docker (Testcontainers). These tests run in CI. "
        "Install Docker Desktop or start Docker to run integration tests locally."
    )
    for item in items:
        item.add_marker(pytest.mark.skip(reason=reason))
    _skip_count = len(items)

    border = "=" * 80
    banner = (
        f"\n{border}\n"
        f"  WARNING: Docker is not available — SKIPPING the entire integration "
        f"test suite ({_skip_count} tests).\n"
        f"  These tests run in CI.\n"
        f"{border}\n"
    )
    # Write to stderr so it is always visible even with captured output.
    sys.stderr.write(banner)
    sys.stderr.flush()


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    if _leaked_asyncpg_connections:
        terminalreporter.section("Integration suite — unclosed asyncpg connections detected")
        for nodeid, detail in _leaked_asyncpg_connections:
            terminalreporter.write_line(f"  {nodeid}: {detail}", red=True)
        terminalreporter.write_line(
            f"  {len(_leaked_asyncpg_connections)} unclosed asyncpg connection(s) were "
            "attributed to the test that leaked them and quarantined so they cannot fail an "
            "unrelated test. Fix the leaking session/engine; set "
            "MODULO_INTEGRATION_STRICT_CONNECTION_LEAKS=1 to make them a hard failure.",
            yellow=True,
        )

    if _skip_count == 0:
        return
    terminalreporter.section("Integration suite — Docker unavailable (local)")
    terminalreporter.write_line(
        f"  {_skip_count} integration test(s) were SKIPPED because Docker is not "
        f"available locally. These tests run in CI.\n"
        f"  To run locally, install Docker Desktop or start the Docker daemon.",
        yellow=True,
    )


@pytest.fixture(scope="session")
def session_monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Session-scoped monkeypatch so session-scoped fixtures can set env vars.

    The built-in ``monkeypatch`` fixture is function-scoped; requesting it from
    a session-scoped fixture raises a ScopeMismatch. This mirror restores all
    changes on session teardown.
    """
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Generator[None, None, None]:
    """Keep settings derived from one integration test out of the next."""
    from modulo.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _domain_table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
        return names - {"alembic_version"}
    finally:
        await engine.dispose()


def _with_credentials(database_url: str, user: str, password: str) -> str:
    """Swap the username/password in an asyncpg URL (keeps host/port/db)."""
    from urllib.parse import quote

    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


@dataclass(frozen=True)
class EvalFixture:
    """The field set for one ``evals`` fixture row.

    ``evals`` is the live eval-definition store (FAR-1060 chunk 1; read/write
    cut over from the legacy ``eval_definitions`` table by migration 0254):
    ``eval_results.eval_id`` references ``evals`` and every production loader
    reads it. Integration fixtures seed directly into ``evals`` — the legacy
    table is a frozen pre-cutover snapshot and is not written by integration
    fixtures (only backfill/migration probes still target it).
    """

    id: uuid.UUID
    organisation_id: uuid.UUID
    pipeline_id: uuid.UUID
    name: str
    eval_type: str
    account_id: uuid.UUID
    node_id: uuid.UUID | None = None
    config: dict[str, object] | None = None


async def insert_eval(conn: AsyncConnection, fixture: EvalFixture) -> None:
    """Insert one ``evals`` row on *conn*.

    Single source of truth for the eval-fixture insert so a future ``evals``
    schema change is single-touch. Callers pass an already-open connection
    inside the surrounding transaction so the eval row commits atomically with
    the objects it belongs to.
    """
    await conn.execute(
        text(
            "INSERT INTO evals "
            "(id, organisation_id, pipeline_id, node_id, name, eval_type, config_json, account_id) "
            "VALUES (:id, :oid, :pid, :nid, :name, :eval_type, CAST(:cfg AS jsonb), :aid)"
        ),
        {
            "id": str(fixture.id),
            "oid": str(fixture.organisation_id),
            "pid": str(fixture.pipeline_id),
            "nid": str(fixture.node_id) if fixture.node_id else None,
            "name": fixture.name,
            "eval_type": fixture.eval_type,
            "cfg": json.dumps(fixture.config or {}),
            "aid": str(fixture.account_id),
        },
    )


# NOTE (FAR-595, D6 fix-Worker lesson): under heavy machine load, the
# container's postgres initdb can exceed testcontainers' default wait and the
# fixture fails before Postgres accepts connections. testcontainers reads
# TC_MAX_TRIES from the environment (env-only, no code change): raise it (e.g.
# TC_MAX_TRIES=300) for LOCAL integration runs, and give the local pytest
# invocation a wide per-test budget (--timeout=1800). CI runners are not
# contended and need neither.
@pytest_asyncio.fixture(scope="package", autouse=True)
async def _isolate_integration_process_engines() -> AsyncGenerator[None, None]:
    """Keep integration's Postgres engines isolated from neighbouring suites.

    Unit and BDD tests may exercise audited routes against a process-shared
    SQLite engine.  If those suites run before integration in one pytest
    process, the cached engine survives the suite boundary and the integration
    tests can accidentally use SQLite (whose lightweight fixture schema does
    not include ``audit_chain_heads``) instead of the migrated testcontainer.
    Reset on both sides of the session so suite order cannot determine which
    database a later suite observes.
    """
    await dispose_process_global_engines()
    try:
        yield
    finally:
        await dispose_process_global_engines()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def db_url(
    postgres_container: PostgresContainer,
    session_monkeypatch: pytest.MonkeyPatch,
) -> str:
    url = postgres_container.get_connection_url()
    # Convert to asyncpg driver
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")
    # Point every settings consumer (including the auth dependency's
    # process-global engine, which reads get_settings().database_url directly
    # and cannot be reached via app.dependency_overrides) at the migrated
    # testcontainer. CI sets DATABASE_URL to a separate empty postgres
    # (deploy.yml "Start Postgres"), so without this the live-role re-read in
    # auth.dependencies._verify_identity hits tables that don't exist there and
    # every API-backed integration test fails with a 503.
    session_monkeypatch.setenv("DATABASE_URL", url)
    # Point the system (BYPASSRLS) engine at the same migrated testcontainer.
    # Without MODULO_SYSTEM_DATABASE_URL set, the process-global system engine
    # falls back to the scoped app role and system_engine_is_fallback() becomes
    # True, so pre-auth paths (webhook trigger delivery, cross-org reads) refuse
    # with 503 (system_bootstrap_degraded) and every integration test that
    # exercises them fails.
    #
    # It MUST carry the dedicated ``modulo_system`` role's credentials, not the
    # container superuser's: ``bootstrap_role._bootstrap`` derives the system
    # role NAME from this URL's username (``sys_user = _parse_role(sys_url) or
    # _SYSTEM_ROLE``). Pointing it at the superuser URL makes bootstrap
    # provision the superuser instead, ``modulo_system`` is never created, and
    # the fatal role-posture assertion fails the boot for every integration
    # test ("modulo_system does not have BYPASSRLS"). ``bootstrap_roles``
    # creates the role LOGIN BYPASSRLS with exactly this password, which
    # mirrors production: deploy/fly/bootstrap_db.derive_system_database_url
    # swaps the runtime URL's username to ``modulo_system``.
    session_monkeypatch.setenv("MODULO_SYSTEM_DATABASE_URL", _with_credentials(url, "modulo_system", "syspass"))
    return url


@pytest.fixture(scope="session")
def migrated_db_url(db_url: str, session_monkeypatch: pytest.MonkeyPatch) -> str:
    # The setup helpers below each run on a PRIVATE event loop via
    # ``asyncio.run``. A pooled engine disposed in that loop schedules its
    # connections' graceful close as a task that ``asyncio.run`` then discards
    # when it closes the loop, so the asyncpg connection surfaces later as
    # ``ResourceWarning: unclosed connection`` (a PytestUnraisableExceptionWarning,
    # which the deploy gate escalates to an error) attributed to whatever
    # unrelated test happens to be collecting garbage. NullPool opens and closes
    # each connection on the same loop, so teardown never races - the same
    # reasoning as ``db_engine`` / ``app_engine`` below.
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    # script_location is CWD-relative in alembic.ini; pin it to the absolute
    # backend path so integration tests also run from the repo root (the
    # pre-commit run-changed-tests hook invokes pytest from the repo root).
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    # Skip fileConfig (like main.py does) so env.py doesn't call
    # logging.config.fileConfig, whose default disable_existing_loggers=True
    # disables every module logger not listed in alembic.ini for the rest of
    # the pytest session (breaks caplog assertions in later tests).
    config.config_file_name = None

    async def _ensure_alembic_table() -> None:
        """Pre-create alembic_version with VARCHAR(255) to support branch migration IDs."""
        eng = create_async_engine(db_url, poolclass=NullPool)
        async with eng.connect() as conn:
            await conn.execute(
                text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"),
            )
            await conn.commit()
        await eng.dispose()

    asyncio.run(_ensure_alembic_table())

    async def _provision_break_glass_roles() -> None:
        """Provision the break-glass DB roles BEFORE the migrations run.

        The reconciliation chain (0108_schema_org_identity) re-owns tables to
        ``modulo_migrate`` and grants EXECUTE on ``deactivate_break_glass`` to
        ``modulo_app``/``modulo_breakglass``, so all three roles must exist
        before ``alembic upgrade heads``.
        """
        eng = create_async_engine(db_url, poolclass=NullPool)
        async with eng.connect() as conn:
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_migrate"'))
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_breakglass"'))
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_app"'))
            await conn.execute(text("CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
            await conn.execute(text("CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
            await conn.execute(text("CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'"))
            await conn.commit()
        await eng.dispose()

    app_url = _with_credentials(db_url, "modulo_app", "apppass")
    bg_url = _with_credentials(db_url, "modulo_breakglass", "bgpass")
    # modulo_system: cross-org system-cron role (fail-closed since #2162 —
    # cron_helpers/saq_worker raise RuntimeError when MODULO_SYSTEM_DATABASE_URL
    # is unset). Provision it with a KNOWN password (bootstrap_role.py parses
    # the role/password out of this URL) and expose it to the session so
    # get_settings() resolves the system engine exactly as in production.
    sys_url = _with_credentials(db_url, "modulo_system", "syspass")

    from modulo.db.bootstrap_role import bootstrap_roles

    async def _migrate_shared_db() -> None:
        """Apply the full schema setup once, serialised across xdist workers.

        Under ``-n 2`` each worker runs this session fixture against the SAME
        shared Postgres. ``alembic upgrade`` is already serialised by env.py's
        own advisory lock, but the surrounding ``bootstrap_role`` grants and the
        ``FORCE ROW LEVEL SECURITY`` schema patch were not — so two workers
        mutated the shared schema concurrently, and a half-applied setup left the
        DB in a state that cascaded into 503s (app DB reads) and FK/unique
        violations across the integration suite. A second session advisory lock
        (distinct from env.py's) makes a single worker perform the entire setup
        while the other blocks until it commits.
        """
        eng = create_async_engine(db_url, poolclass=NullPool)
        async with eng.connect() as lock_conn:
            await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
            await lock_conn.execute(text("SELECT pg_advisory_lock(72002, 1)"))
            try:
                await _provision_break_glass_roles()
                with patch.dict(
                    os.environ,
                    {
                        "DATABASE_URL": db_url,
                        "DATABASE_ADMIN_URL": db_url,
                        "MODULO_BREAK_GLASS_DATABASE_URL": bg_url,
                        "MODULO_SYSTEM_DATABASE_URL": sys_url,
                    },
                ):
                    await bootstrap_roles(db_url, app_url)
                    # Override DATABASE_URL so alembic env.py uses the
                    # testcontainer, not any CI env var pointing at the service
                    # postgres.
                    command.upgrade(config, "heads")
                    await bootstrap_roles(db_url, app_url)
                await _patch_schema()
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(72002, 1)"))
        await eng.dispose()

    # Persist for the session: settings consumers (cron_helpers/saq_worker
    # system engine) read get_settings().modulo_system_database_url at test
    # time, after the per-test get_settings.cache_clear().
    session_monkeypatch.setenv("MODULO_SYSTEM_DATABASE_URL", sys_url)

    async def _patch_schema() -> None:
        """Add the test-only schema surface the migrations don't cover.

        The old ORM column hacks (pipelines.default_autonomy_level,
        organisations.otel_config_json default, webhook_payloads
        raw_body/raw_payload, run_daily_facts.telemetry_bytes) are gone — the
        reconciliation chain (0006/0007/0008) now creates them. Only the
        runtime-managed LangGraph checkpoint tables and the test-only FORCE RLS
        remain.
        """
        eng = create_async_engine(db_url, poolclass=NullPool)
        async with eng.connect() as conn:
            # LangGraph checkpoint tables — created at production startup by
            # ``ModuloPostgresSaver.setup()`` (main.py lifespan), not by any
            # Alembic migration. ``dispatcher_reconcile``'s nodeless-zombie
            # predicate reads the bare ``checkpoints`` table, so the test DB
            # must have it or every reconcile scan fails with
            # UndefinedTableError (breaking the SAQ reconcile integration tests).
            #
            # Replay MIGRATIONS (schema DDL + the boot-time autovacuum
            # tuning), not the schema-only ``_MIGRATION_SQL``, so the shared
            # DB mirrors what ``setup()`` leaves behind in production AND a
            # change that breaks the tuning SQL is executed here. The unit
            # twin tests only compare STRINGS, and ``integration-changed``
            # runs only changed integration files — so without this replay a
            # tuning statement with a syntax error could reach main unnoticed
            # by PR CI. Every statement is idempotent and MIGRATIONS orders
            # the CREATEs before the tuning ALTERs, so replaying them is safe.
            from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver

            for _ddl in ModuloPostgresSaver.MIGRATIONS:
                await conn.execute(text(_ddl))

            # Force RLS on all org-scoped tables so it applies to the testcontainers
            # superuser role too. In production, the modulo_app role is not a superuser
            # so RLS applies automatically — but testcontainers run as the DB superuser
            # which bypasses ENABLE RLS, hence the explicit FORCE in tests.
            for _tbl in (
                "org_daily_run_counts",
                "org_memberships",
                "audit_events",
                "schemas",
                "teams",
                "connector_instances",
                "library_primitives",
                "model_backends",
                "org_api_keys",
                "schema_versions",
                "agents",
                "pipelines",
                "pipeline_edges",
                "pipeline_snapshots",
                "triggers",
                "runs",
                "run_node_outputs",
                "webhook_dedup_hashes",
                "hitl_claims",
                "notification_delivery_log",
                "trigger_events",
                "webhook_payloads",
                "environment_profiles",
                "cost_components",
            ):
                await conn.execute(text(f"ALTER TABLE {_tbl} FORCE ROW LEVEL SECURITY"))

            await conn.commit()
        await eng.dispose()

    asyncio.run(_migrate_shared_db())
    return db_url


@pytest_asyncio.fixture(scope="session")
async def db_engine(migrated_db_url: str) -> AsyncEngine:
    # NullPool: the session-scoped engine is created on the session loop but
    # tests (and the auth dependency's process-global engine) run on function
    # loops. A pooled connection checked out here would later be reused on a
    # different event loop, raising "attached to a different loop" asyncpg
    # errors. NullPool opens a fresh connection per checkout.
    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def non_superuser_role(db_engine: AsyncEngine) -> str:
    """Provision a non-superuser role with full DML privileges.

    Postgres RLS never applies to superusers/BYPASSRLS roles — even under
    ``FORCE ROW LEVEL SECURITY`` (verified empirically against the
    testcontainers ``test`` user). Tests that assert cross-org isolation must
    run their DB sessions as a non-superuser role so the RLS policies actually
    filter rows. This mirrors production, where the ``modulo_app`` runtime role
    is a non-owner. Returns the role name; ``app_engine`` SET ROLEs to it on
    every connection checkout.
    """
    role = "modulo_integration_app"
    async with db_engine.connect() as conn:
        await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await conn.execute(text(f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOLOGIN'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO "{role}"'))
        # Grant DML on future tables. The reconciliation chain's guarded DDL
        # recreates tables idempotently (e.g. run_number_counters) if the chain
        # ever runs below a table's creation point, so the recreated table must
        # keep the app role's privileges. ``GRANT ... ON ALL TABLES`` above
        # only covers tables that exist at fixture setup, so a recreated table
        # loses the app role's privileges and trigger fire paths fail with
        # "permission denied for table run_number_counters". Default privileges
        # keep every future/recreated table granted to the role, mirroring the
        # ``ALTER DEFAULT PRIVILEGES`` bootstrap_role.py applies for modulo_app.
        await conn.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{role}"'))
        await conn.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{role}"'))
        await conn.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO "{role}"'))
        await conn.execute(text("COMMIT"))
    yield role
    async with db_engine.connect() as conn:
        await conn.execute(text(f'DROP OWNED BY "{role}"'))
        await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await conn.execute(text("COMMIT"))


@pytest_asyncio.fixture(scope="session", autouse=True)
async def cron_helpers_rls_engine(non_superuser_role: str) -> Generator[None, None, None]:
    """Make the ``cron_helpers`` engine run under RLS (mirrors production).

    ``fire_due_triggers`` / ``dispatcher_reconcile`` build their own engine from
    ``get_settings().database_url`` — the testcontainer ``test`` superuser, which
    BYPASSES row-level security. Without RLS the per-org scoping inside
    ``fire_due_triggers`` is a no-op: a due trigger in a PAUSED org is processed
    under the first *unpaused* org's iteration and gets enqueued (skip-not-defer
    broken). In production the app connects as ``modulo_app`` (NOBYPASSRLS), so
    RLS applies. This fixture patches ``cron_helpers._get_engine`` to
    ``SET ROLE`` to the non-superuser ``modulo_integration_app`` role on every
    checkout — the same pattern ``app_engine`` uses — restoring RLS for the SAQ
    scheduler/reconcile code paths.
    """
    from modulo.core import cron_helpers

    original_get_engine = cron_helpers._get_engine
    role = non_superuser_role
    # Engines already instrumented with the SET ROLE checkout listener. A
    # checkout listener executes SQL through the asyncpg sync proxy, which
    # yields to the event loop mid-dispatch; registering another listener from
    # a concurrent coroutine mutates the pool's listener deque while it is
    # being iterated ("RuntimeError: deque mutated during iteration", deploy
    # run 36583273069 / test_two_connections_no_double_redispatch). Register at
    # most once per engine so repeated _get_engine() calls cannot race.
    instrumented_engines: set[AsyncEngine] = set()

    def _rls_get_engine() -> AsyncEngine:
        from sqlalchemy import event

        engine = original_get_engine()
        if engine in instrumented_engines:
            return engine

        @event.listens_for(engine.sync_engine, "checkout")
        def _set_role_on_checkout(
            dbapi_connection: object,
            _connection_record: object,
            _connection_proxy: object,
        ) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f'SET ROLE "{role}"')
            finally:
                cursor.close()

        instrumented_engines.add(engine)

        return engine

    cron_helpers._get_engine = _rls_get_engine
    yield
    cron_helpers._get_engine = original_get_engine


# ---------------------------------------------------------------------------
# Unclosed asyncpg connections: deterministic attribution
# ---------------------------------------------------------------------------
# An asyncpg connection that nothing closes is only reported by
# ``Connection.__del__`` when the CYCLIC collector happens to reclaim it — which
# is during an arbitrary LATER test's setup. Under the integration warning gate
# (``-W error::pytest.PytestUnraisableExceptionWarning``) that surfaces as a
# failure naming an innocent test (the deploy pre-deploy gate failed for ~14h on
# main, always blaming ``test_eval_backfill_cutover``, across four one-site-at-a-
# time fixes), is order/GC dependent (the same SHA passed then failed), and
# poisons every test after it on the same worker.
#
# Force the cyclic collection at THIS test's teardown instead, so the leak is
# surfaced against the test (or fixture) that actually caused it, and consume it
# there so it cannot fail an unrelated test afterwards. The leak is never
# swallowed silently: it is logged at ERROR with the test id and repeated in the
# session summary. Set ``MODULO_INTEGRATION_STRICT_CONNECTION_LEAKS=1`` to make
# a detected leak fail the leaking test outright (the deterministic replacement
# for the old random-attribution failure) once the remaining sites are fixed.
_leaked_asyncpg_connections: list[tuple[str, str]] = []
_leak_logger = logging.getLogger("modulo.tests.integration.connection_leaks")


def _strict_connection_leaks() -> bool:
    """True when an unclosed asyncpg connection must fail the test that leaked it."""
    return os.environ.get("MODULO_INTEGRATION_STRICT_CONNECTION_LEAKS", "").strip().lower() in {"1", "true", "yes"}


def _is_unclosed_asyncpg_connection(unraisable: Any) -> bool:
    """True for the unraisable asyncpg raises from ``Connection.__del__``.

    CPython reports an error raised inside ``__del__`` with the *``__del__``
    function*, not the instance, as ``object`` — the leaked instance itself is
    unreachable from the hook (it is already being collected), so the event is
    identified by its type and message instead.
    """
    exc_value = getattr(unraisable, "exc_value", None)
    return isinstance(exc_value, ResourceWarning) and str(exc_value).startswith(
        "unclosed connection <asyncpg.connection.Connection"
    )


def _record_unclosed_asyncpg_connection(item: pytest.Item, unraisable: Any) -> None:
    """Log and remember a leaked asyncpg connection against the leaking test."""
    detail = str(unraisable.exc_value)
    _leaked_asyncpg_connections.append((item.nodeid, detail))
    _leak_logger.error(
        "Unclosed asyncpg connection surfaced in the teardown of %s: %s. Close the "
        "session/connection that owns it (per-test engines must use NullPool or be "
        "disposed on the loop that created them).",
        item.nodeid,
        detail,
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, None, None]:
    """Attribute GC-discovered unclosed asyncpg connections to the leaking test.

    ``tryfirst`` + ``wrapper`` makes this the OUTERMOST teardown hook, so the
    capture spans every fixture finaliser for the test, and the forced cyclic
    collection runs once they have all finished.

    The hook is CHAINED, not replaced: only asyncpg-shaped events are captured
    and consumed here; every other unraisable is passed straight to pytest's
    own hook so it lands in pytest's per-session deque *before* the ``trylast``
    ``collect_unraisable`` drains it inside THIS item's teardown. Replacing the
    hook outright left the deque empty at the drain point and forwarded the
    events afterwards, which made pytest raise them against the NEXT (innocent)
    test at setup — the exact misattribution this hook exists to remove.

    The forced ``gc.collect()`` runs after that drain point, so events it
    surfaces are captured locally and re-emitted below: asyncpg leaks are
    attributed to this item, and any other unraisable is raised as a
    ``PytestUnraisableExceptionWarning`` here rather than being left in the
    deque for the next item.
    """
    leaked: list[Any] = []
    previous_hook = sys.unraisablehook

    def chained(unraisable: Any) -> None:
        if _is_unclosed_asyncpg_connection(unraisable):
            leaked.append(unraisable)
        else:
            previous_hook(unraisable)

    sys.unraisablehook = chained
    result: Any = None
    teardown_completed = False
    try:
        result = yield
        teardown_completed = True
    finally:
        collateral: list[Any] = []

        def collect_after_drain(unraisable: Any) -> None:
            if _is_unclosed_asyncpg_connection(unraisable):
                leaked.append(unraisable)
            else:
                collateral.append(unraisable)

        sys.unraisablehook = collect_after_drain
        try:
            with contextlib.suppress(Exception):
                gc.collect()
        finally:
            sys.unraisablehook = previous_hook

        for unraisable in leaked:
            _record_unclosed_asyncpg_connection(item, unraisable)
        for unraisable in collateral:
            warnings.warn(
                pytest.PytestUnraisableExceptionWarning(str(unraisable.exc_value)),
                stacklevel=2,
            )
        # A teardown error is already failing this item; do not mask it, but the
        # leak is still logged and listed in the session summary above.
        # ``teardown_completed`` is only True when the wrapped hook chain
        # returned normally, so any propagating exception (including
        # KeyboardInterrupt/SystemExit) leaves it False and keeps its failure.
        if leaked and teardown_completed and _strict_connection_leaks():
            detail = str(leaked[0].exc_value)
            pytest.fail(
                f"unclosed asyncpg connection leaked by {item.nodeid}: {detail}",
                pytrace=False,
            )
    return result


@pytest_asyncio.fixture(autouse=True)
async def _reset_shared_test_org_pause(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> AsyncGenerator[None, None]:
    """Restore the session-scoped shared ``test_org`` to not-paused after each test.

    ``saq/test_fire_due_triggers.py::test_paused_org_skips_enqueue_but_advances_next_fire``
    sets ``triggers_paused = TRUE`` on the SHARED session-scoped ``test_org`` and
    never restores it. Every later test that fires a trigger through the shared
    org re-reads the pause state via ``settings_resolver.org_is_paused`` (fail-closed),
    so the leaked pause makes them raise ``TriggersPausedError`` regardless of their
    own setup. This function-scoped autouse teardown re-establishes the not-paused
    invariant after every test, so a test that intentionally pauses the shared org
    during its own body is cleaned up before the next test runs.
    """
    yield
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE organisations SET triggers_paused = false, triggers_paused_at = NULL WHERE id = :oid",
            ),
            {"oid": str(test_org)},
        )


@pytest_asyncio.fixture(scope="session")
async def app_engine(migrated_db_url: str, non_superuser_role: str) -> AsyncEngine:
    """Engine whose connections run as a non-superuser role (RLS applies).

    ``SET ROLE`` is applied on every connection checkout via a sync-cursor
    event, mirroring ``db.rls.register_rls_reset_hook``. Used by tests that
    assert RLS isolation (cross-tenant HTTP clients, environment-profile RLS).
    """
    from sqlalchemy import event

    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "checkout")
    def _set_role_on_checkout(
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f'SET ROLE "{non_superuser_role}"')
        finally:
            cursor.close()

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def modulo_app_engine(migrated_db_url: str) -> AsyncEngine:
    """Engine whose connections run as the real modulo_app role (RLS applies).

    ``SET ROLE modulo_app`` sets ``current_user`` only (``session_user`` stays
    the superuser), which is exactly how the REST + SCIM routes run. The
    caller-bound SECURITY DEFINER's non-operator branch is exercised this way.
    """
    from sqlalchemy import event

    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "checkout")
    def _set_role_on_checkout(
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute('SET ROLE "modulo_app"')
        finally:
            cursor.close()

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def breakglass_engine(migrated_db_url: str) -> AsyncEngine:
    """A REAL LOGIN engine connecting as modulo_breakglass.

    Connecting directly (not via SET ROLE) makes ``session_user =
    'modulo_breakglass'``, which is the ONLY path that satisfies the SECURITY
    DEFINER's operator branch — a SET ROLE session does NOT (negative assertion
    covered in test_break_glass.py).
    """
    bg_url = _with_credentials(migrated_db_url, "modulo_breakglass", "bgpass")
    engine = create_async_engine(bg_url, echo=False, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncSession:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# Shared entity fixtures — session-scoped to avoid recreating for every test
# These are inherited by all subdirectories (crud/, bdd/, feedback_manager/,
# trigger_engine/) via pytest conftest discovery.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def test_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Committed organisation row available for the whole test session."""
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Integration Test Org",
                "slug": f"int-{org_id.hex[:8]}",
            },
        )
    return org_id


@pytest_asyncio.fixture(scope="session")
async def test_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    """Committed account + org_membership row in test_org."""
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {
                "id": str(account_id),
                "email": "integration-test@example.com",
                "name": "Integration Test User",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(test_org),
            },
        )
    return account_id


@pytest_asyncio.fixture
async def rls_session(migrated_db_url: str, test_org: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    """AsyncSession with RLS set to test_org; all ORM changes are rolled back.

    Creates a dedicated engine + session on the current event loop so that
    connection operations never cross event loop boundaries.

    The engine uses ``NullPool`` (matching ``db_engine`` / ``app_engine``).
    A pooled engine is created and disposed once per test, so pooling buys
    nothing but introduces a race: the pool's graceful-close task can still be
    pending when the engine is disposed, leaving an asyncpg connection
    unclosed. Garbage collection then surfaces it as a
    ``PytestUnraisableExceptionWarning`` attributed to whatever unrelated test
    happens to be setting up — the flake documented in
    ``crud/test_connector_instance.py``. NullPool opens a fresh connection per
    checkout and closes it on the same event loop, so teardown never races.
    """
    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            from modulo.db.rls import set_rls_org

            await set_rls_org(session, test_org)
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def test_pipeline(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> uuid.UUID:
    """Committed pipeline row in test_org."""
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(test_org),
                "name": "Integration Test Pipeline",
                "uid": str(test_user),
            },
        )
    return pipeline_id


@pytest_asyncio.fixture(scope="session")
async def test_snapshot(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
) -> uuid.UUID:
    """Committed pipeline_snapshot row in test_org."""
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(test_pipeline), "oid": str(test_org)},
        )
    return snapshot_id


@pytest_asyncio.fixture(scope="session")
async def test_trigger(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_user: uuid.UUID,
) -> uuid.UUID:
    """Committed trigger row (webhook type) in test_org."""
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, '{}'::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(test_org),
                "pid": str(test_pipeline),
                "uid": str(test_user),
            },
        )
    return trigger_id


@pytest_asyncio.fixture(scope="session")
async def test_suite_run(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
) -> uuid.UUID:
    """Committed suite_run row in test_org."""
    suite_run_id = uuid.uuid4()
    # Need to create a test eval_suite and eval_dataset first
    suite_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    model_backend_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO eval_suites (id, organisation_id, name, visibility, owner_team_id) "
                "VALUES (:id, :oid, 'Test Suite', 'org', NULL)"
            ),
            {"id": str(suite_id), "oid": str(test_org)},
        )
        await conn.execute(
            text(
                "INSERT INTO eval_datasets (id, organisation_id, name, visibility, owner_team_id, version) "
                "VALUES (:id, :oid, 'Test Dataset', 'org', NULL, 1)"
            ),
            {"id": str(dataset_id), "oid": str(test_org)},
        )
        await conn.execute(
            text(
                "INSERT INTO model_backends ("
                "id, organisation_id, name, display_name, provider, model_id, "
                "default_params, visibility, credentials_ciphertext, status, account_id) "
                "VALUES (:id, :oid, 'Test Backend', 'Test Backend', 'opencode', "
                "'test-model', '{}'::jsonb, 'org', '\\x00', 'active', :aid)"
            ),
            {"id": str(model_backend_id), "oid": str(test_org), "aid": str(test_user)},
        )
        await conn.execute(
            text(
                "INSERT INTO suite_runs ("
                "id, organisation_id, suite_id, dataset_id, dataset_version, "
                "definition_checksum, model_backend_id, state, total_cases, "
                "passed_cases, failed_cases, excluded_case_count, created_at, completed_at) "
                "VALUES (:id, :oid, :sid, :did, 1, 'checksum', :mbid, "
                "'completed', 10, 7, 3, 0, now(), now())"
            ),
            {
                "id": str(suite_run_id),
                "oid": str(test_org),
                "sid": str(suite_id),
                "did": str(dataset_id),
                "mbid": str(model_backend_id),
            },
        )
    return suite_run_id


# ---------------------------------------------------------------------------
# Shared ASGI client fixture
# ---------------------------------------------------------------------------
# ``integration_client`` was previously copy-pasted into every integration test
# module, which SonarCloud flagged as duplicated new code. It is now defined
# once here and inherited by all integration tests (pytest resolves fixtures
# from parent conftest files). The variant below wires the app's dependency
# overrides against ``app_engine`` with an all-features plan context.


_VALID_32 = "a" * 32


class _AllFeatures:
    """Plan-context stub that reports every feature as enabled (enterprise tier)."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


@pytest_asyncio.fixture
async def integration_client(db_url: str, app_engine: AsyncEngine) -> AsyncClient:
    """ASGI ``AsyncClient`` wired with the app's dependency overrides."""
    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.core.feature_flags import PlanContext
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
        factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
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


async def dispose_process_global_engines() -> None:
    """Dispose and clear every process-global async-engine cache.

    Unit and BDD suites can leave SQLite engines in any of the lazy singleton
    caches below. A mixed pytest invocation then reaches integration tests with
    a process-global engine still bound to the wrong database. Keep this list
    exhaustive: every production module that owns an async-engine cache is
    represented, including caches used only by reports, snapshot locks, and
    break-glass operations.

    Integration helpers that drive a scenario on a PRIVATE event loop
    (``asyncio.run``, e.g. the sync pytest-bdd steps in
    ``test_hitl_reject_bdd.py``) may create one of those engines on that loop;
    ``asyncio.run`` then closes the loop with asyncpg connections still open, so
    their graceful-close tasks never complete and the pending
    ``Connection._cancel`` coroutine surfaces as a
    ``PytestUnraisableExceptionWarning`` under the warning gate.

    Call this from the coroutine that runs on the private loop, BEFORE the loop
    closes. Resetting each cache makes the next caller recreate an engine bound
    to the then-current loop and database URL.
    """
    from modulo.api import dependencies as api_dependencies
    from modulo.cli import break_glass
    from modulo.core import cron_helpers, saq_worker
    from modulo.core.error_tracking import saq_hooks
    from modulo.core.reports import scheduler
    from modulo.db import session as db_session
    from modulo.db.crud import pipeline_snapshot

    single_engine_caches: tuple[tuple[object, str], ...] = (
        (db_session, "_shared_engine"),
        (api_dependencies, "_engine"),
        (api_dependencies, "_SYSTEM_ASYNC_ENGINE"),
        (cron_helpers, "_ENGINE"),
        (cron_helpers, "_SYSTEM_ENGINE"),
        (saq_hooks, "_ENGINE"),
        (saq_worker, "_ASYNC_ENGINE"),
        (saq_worker, "_SYSTEM_ASYNC_ENGINE"),
        (scheduler, "_ENGINE"),
        (scheduler, "_TEST_ENGINE"),
        (break_glass, "_bg_engine"),
    )
    seen: set[int] = set()
    for module, attr in single_engine_caches:
        engine = getattr(module, attr, None)
        if engine is None:
            continue
        if id(engine) not in seen:
            seen.add(id(engine))
            # A cache may hold an engine already disposed by an earlier test; a
            # second dispose is a no-op, but never let it mask the reset.
            with contextlib.suppress(Exception):
                await engine.dispose()
        setattr(module, attr, None)

    for engine in tuple(pipeline_snapshot._SNAPSHOT_LOCK_ENGINES.values()):
        if id(engine) not in seen:
            seen.add(id(engine))
            with contextlib.suppress(Exception):
                await engine.dispose()
    pipeline_snapshot._SNAPSHOT_LOCK_ENGINES.clear()

    # Session factories retain an engine even after the corresponding engine
    # cache is cleared; clear them as well so they cannot resurrect the polluted
    # SQLite binding.
    api_dependencies._session_factory = None
    api_dependencies._SYSTEM_SESSION_FACTORY = None
    api_dependencies._SYSTEM_ENGINE_IS_FALLBACK = False
    break_glass._bg_engine_url = None
    break_glass._bg_factory = None
    break_glass._bg_factory_url = None
