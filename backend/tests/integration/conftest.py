"""Integration test fixtures — spins up a real Postgres via Testcontainers."""

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
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
    return url


@pytest.fixture(scope="session")
def migrated_db_url(db_url: str, session_monkeypatch: pytest.MonkeyPatch) -> str:
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
        eng = create_async_engine(db_url)
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
        eng = create_async_engine(db_url)
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
        eng = create_async_engine(db_url)
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
        eng = create_async_engine(db_url)
        async with eng.connect() as conn:
            # LangGraph checkpoint tables — created at production startup by
            # ``ModuloPostgresSaver.setup()`` (main.py lifespan), not by any
            # Alembic migration. ``dispatcher_reconcile``'s nodeless-zombie
            # predicate reads the bare ``checkpoints`` table, so the test DB
            # must have it or every reconcile scan fails with
            # UndefinedTableError (breaking the SAQ reconcile integration tests).
            from modulo.core.pipeline_engine.modulo_saver import _MIGRATION_SQL as _CHECKPOINT_MIGRATION_SQL

            for _ddl in _CHECKPOINT_MIGRATION_SQL:
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
                "webhook_dedup_hashes",
                "hitl_claims",
                "notification_delivery_log",
                "trigger_events",
                "webhook_payloads",
                "environment_profiles",
                "workspace_leases",
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
    import modulo.core.cron_helpers as cron_helpers

    original_get_engine = cron_helpers._get_engine
    role = non_superuser_role

    def _rls_get_engine() -> AsyncEngine:
        from sqlalchemy import event

        engine = original_get_engine()

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

        return engine

    cron_helpers._get_engine = _rls_get_engine
    yield
    cron_helpers._get_engine = original_get_engine


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
    """
    engine = create_async_engine(migrated_db_url, echo=False)
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
