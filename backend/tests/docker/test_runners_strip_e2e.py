"""E2E coverage for the Runners status strip states (FAR-772).

Three scenarios drive the real probe tick (``run_runner_health_probe``)
against a real Docker engine and read the result through the running FastAPI
app's ``GET /api/v1/runners/status`` read model:

1. engine down — the tick records ``engine_unreachable``; the strip shows
   ``engine_unreachable`` and the runner profile reads unavailable;
2. rig up — the tick against the socket-proxy engine records ``healthy``
   with the pinned image present; the strip shows ``healthy`` and the
   profile is offered;
3. engine kill — with a workspace provisioned inside the nested dind
   engine, killing the engine flips the strip to ``engine_unreachable`` and
   the healthy→unreachable transition surfaces BOTH an in-app notification
   and an error-dashboard event (``signal=runner_unavailable``).

The split is deliberate and mirrors production posture:

- the probe WRITES org-scoped cache rows as the DB superuser (BYPASSRLS —
  the testcontainer equivalent of ``modulo_system``);
- the status read runs on the FastAPI app wired to a NON-superuser engine
  that ``SET ROLE``s to a dedicated ``modulo_runners_e2e_app`` role, so RLS
  actually scopes what the strip sees, exactly like the ``modulo_app``
  runtime role in production.

Scenario 1 needs only a reachable local engine (the autouse ``_require_engine``
skip below). Scenarios 2 and 3 additionally need the runner-ci compose rig
(``MODULO_RUNNER_HARNESS_PROXY_HOST`` / ``MODULO_RUNNER_HARNESS_DIND_HOST``)
and skip cleanly without it.
"""

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiodocker
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from modulo.core.bundled_runner.health_probe import run_runner_health_probe
from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider

pytestmark = [pytest.mark.docker]

_MACHINE_ID = "e2e-runner-machine"
_IMAGE_REF = "alpine:3.20"
_DEAD_PORT = 9
_APP_ROLE = "modulo_runners_e2e_app"
_VALID_32 = "a" * 32
_BACKEND_ROOT = Path(__file__).parents[2]
_STATUS_ENDPOINT = "/api/v1/runners/status"

_HARNESS_PROXY_ENV = "MODULO_RUNNER_HARNESS_PROXY_HOST"
_HARNESS_DIND_ENV = "MODULO_RUNNER_HARNESS_DIND_HOST"
_DINO_LABEL = "runner-ci-dind"


@pytest.fixture(scope="module")
def session_monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Module-scoped monkeypatch (session-scoped fixtures need env mirrors)."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Generator[None, None, None]:
    """Keep settings derived from one test out of the next."""
    from modulo.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _proxy_host() -> str | None:
    return os.environ.get(_HARNESS_PROXY_ENV)


def _dind_host() -> str | None:
    return os.environ.get(_HARNESS_DIND_ENV)


def _require_proxy() -> str:
    host = _proxy_host()
    if not host:
        pytest.skip(
            f"runner-ci compose rig not up (set {_HARNESS_PROXY_ENV} to the "
            "socket-proxy endpoint; see deploy/compose/README-runner-ci.md)"
        )
    return host


def _require_dind() -> str:
    host = _dind_host()
    if not host:
        pytest.skip(
            f"runner-ci nested dind not up (set {_HARNESS_DIND_ENV} to the "
            "dind tcp:// endpoint; see deploy/compose/README-runner-ci.md)"
        )
    return host


# ---------------------------------------------------------------------------
# Postgres (module-scoped): container + full alembic migration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="module")
def db_url(
    postgres_container: PostgresContainer,
    session_monkeypatch: pytest.MonkeyPatch,
) -> str:
    url = postgres_container.get_connection_url()
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")
    session_monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture(scope="module")
def migrated_db_url(db_url: str) -> str:
    from alembic import command
    from alembic.config import Config

    config = Config(_BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(_BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None

    async def _migrate() -> None:
        eng = create_async_engine(db_url)
        try:
            async with eng.connect() as conn:
                await conn.execute(
                    text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"),
                )
                await conn.execute(text('DROP ROLE IF EXISTS "modulo_migrate"'))
                await conn.execute(text('DROP ROLE IF EXISTS "modulo_breakglass"'))
                await conn.execute(text('DROP ROLE IF EXISTS "modulo_app"'))
                await conn.execute(text("CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
                await conn.execute(text("CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
                await conn.execute(text("CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'"))
                await conn.commit()
            with patch.dict(os.environ, {"DATABASE_ADMIN_URL": db_url, "DATABASE_URL": db_url}):
                command.upgrade(config, "heads")
            async with eng.connect() as conn:
                await conn.execute(text(f'DROP ROLE IF EXISTS "{_APP_ROLE}"'))
                await conn.execute(text(f'CREATE ROLE "{_APP_ROLE}" NOSUPERUSER NOBYPASSRLS NOLOGIN'))
                await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{_APP_ROLE}"'))
                await conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{_APP_ROLE}"'))
                await conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{_APP_ROLE}"'))
                await conn.execute(text(f'GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO "{_APP_ROLE}"'))
                await conn.execute(
                    text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{_APP_ROLE}"')
                )
                await conn.execute(
                    text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{_APP_ROLE}"')
                )
                await conn.execute(
                    text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO "{_APP_ROLE}"')
                )
                await conn.commit()
        finally:
            await eng.dispose()

    asyncio.run(_migrate())
    return db_url


@pytest_asyncio.fixture(scope="module")
async def superuser_engine(migrated_db_url: str) -> AsyncEngine:
    # NullPool: the module-scoped engine cannot carry pooled connections
    # across the per-test sections of the session loop.
    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="module")
def probe_session_factory(
    superuser_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory for the probe tick (superuser / BYPASSRLS writer)."""
    return async_sessionmaker(superuser_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module")
async def app_engine(migrated_db_url: str) -> AsyncEngine:
    """Engine whose connections SET ROLE to the non-superuser app role.

    RLS therefore applies on every read, mirroring ``modulo_app`` production
    posture for the HTTP strip.
    """
    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "checkout")
    def _set_role_on_checkout(
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f'SET ROLE "{_APP_ROLE}"')
        finally:
            cursor.close()

    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# Shared entity fixtures (module-scoped; written via the superuser engine)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def test_org(superuser_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with superuser_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(org_id),
                "name": "Runners Strip E2E Org",
                "slug": f"strip-{org_id.hex[:8]}",
            },
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def test_user(
    superuser_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with superuser_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(account_id),
                "email": "runners-strip-e2e@example.com",
                "name": "Runners Strip E2E User",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(test_org),
            },
        )
    return account_id


@pytest_asyncio.fixture(scope="module")
async def test_profile(
    superuser_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> uuid.UUID:
    profile_id = uuid.uuid4()
    async with superuser_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO environment_profiles (id, organisation_id, name, provider_type, "
                "image_ref, capabilities_json, config_json, network_policy, "
                "initialisation_strategy, secret_refs_json, persistence_policy, account_id) "
                "VALUES (:id, :oid, :name, 'runner_docker', :ref, '[]'::json, '{}'::json, "
                "'outbound', 'git_clone', '[]'::json, 'ephemeral', :uid)"
            ),
            {
                "id": str(profile_id),
                "oid": str(test_org),
                "name": "Bundled Runner (e2e)",
                "ref": _IMAGE_REF,
                "uid": str(test_user),
            },
        )
    return profile_id


# ---------------------------------------------------------------------------
# ASGI client wired the same way the integration suite wires the app
# ---------------------------------------------------------------------------


class _AllFeatures:
    """Plan-context stub that reports every feature as enabled."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


@pytest_asyncio.fixture
async def client(
    migrated_db_url: str,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> AsyncGenerator[Any, None]:
    from httpx import ASGITransport, AsyncClient

    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_tenant_user, get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
    from modulo.core.feature_flags import PlanContext
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=migrated_db_url,
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

    def _all_features_ctx() -> PlanContext:
        return _AllFeatures()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = _all_features_ctx
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin-e2e",
        organisation_id=test_org,
        account_id=test_user,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin-e2e",
        organisation_id=test_org,
        account_id=test_user,
        org_role="admin",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as http:
        yield http
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Engine helpers (local engine; bounded polls with literal sleeps)
# ---------------------------------------------------------------------------


async def _engine_reachable_at(url: str) -> bool:
    try:
        async with aiodocker.Docker(url=url) as docker:
            await docker.version()
        return True
    except Exception:
        return False


async def _wait_for_engine_down(url: str, attempts: int = 20) -> None:
    for _ in range(attempts):
        if not await _engine_reachable_at(url):
            return
        await asyncio.sleep(1.0)
    raise AssertionError(f"engine at {url} never went down after the kill")


async def _wait_for_engine_up(url: str, attempts: int = 60) -> None:
    for _ in range(attempts):
        if await _engine_reachable_at(url):
            return
        await asyncio.sleep(1.0)
    raise AssertionError(f"engine at {url} never became ready")


async def _ensure_image_present(url: str | None, image: str) -> None:
    for _attempt in range(3):
        try:
            async with aiodocker.Docker(url=url) as docker:
                try:
                    await docker.images.inspect(image)
                except aiodocker.exceptions.DockerError:
                    await docker.images.pull(image)
            return
        except (aiodocker.exceptions.DockerError, OSError, RuntimeError, TimeoutError):
            await asyncio.sleep(2.0)
    raise AssertionError(f"image {image} could not be ensured on engine {url}")


async def _kill_local_container_by_label(label: str) -> None:
    async with aiodocker.Docker() as docker:
        containers = await docker.containers.list(filters={"label": [f"modulo.test={label}"]})
        for container in containers:
            with contextlib.suppress(Exception):
                await container.kill()


def _workspace_spec() -> WorkspaceSpec:
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        image_ref=_IMAGE_REF,
        capabilities=[],
        timeout_seconds=300,
        resource_limits={"memory_mb": 256},
        egress_policy="outbound",
        persistence_policy="ephemeral",
        labels={},
        workspace_metadata={
            "modulo.run.id": "strip-e2e-run-1",
            "modulo.org.id": "strip-e2e-org-1",
        },
        workspace_network="bridge",  # dind has no compose networks
    )


async def _count_notifications(
    superuser_engine: AsyncEngine,
    org_id: uuid.UUID,
    category: str,
) -> int:
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM notifications WHERE organisation_id = :oid AND category = :cat"),
            {"oid": str(org_id), "cat": category},
        )
        return int(result.scalar_one())


async def _count_error_events(
    superuser_engine: AsyncEngine,
    org_id: uuid.UUID,
    signal: str,
) -> int:
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM error_events WHERE organisation_id = :oid AND signal = :sig"),
            {"oid": str(org_id), "sig": signal},
        )
        return int(result.scalar_one())


async def _strip(client: Any) -> dict[str, Any]:
    resp = await client.get(_STATUS_ENDPOINT)
    assert resp.status_code == 200, f"GET {_STATUS_ENDPOINT} failed: {resp.status_code}"
    return resp.json()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_engine_down_maps_to_unreachable_and_profile_unavailable(
    client: Any,
    probe_session_factory: Any,
    test_profile: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 1: a dead engine target maps to engine_unreachable.

    READ (no engine calls on the request path) -> the strip aggregate reads
    the just-written probe row: machine state ``engine_unreachable``, worst-of
    aggregate ``engine_unreachable``, profile unavailable, and the resource
    preflight reads ``unknown`` (no engine info was recorded).
    """
    monkeypatch.setenv("MODULO_DOCKER_HOST", f"tcp://127.0.0.1:{_DEAD_PORT}")

    result = await run_runner_health_probe(probe_session_factory, machine_id=_MACHINE_ID)
    assert result["reachable"] is False
    assert result["transitions"] == 1
    assert result["orgs_failed"] == 0

    payload = await _strip(client)
    assert payload["aggregate_state"] == "engine_unreachable"
    machines = payload["machines"]
    assert machines, "the strip must expose at least the probed machine"
    assert machines[0]["machine_id"] == _MACHINE_ID
    assert machines[0]["state"] == "engine_unreachable"
    assert machines[0]["engine_reachable"] is False
    assert machines[0]["images_present"] is None
    assert not machines[0]["engine_info"], "a dead engine records no engine info"
    assert machines[0]["probe_error"] is not None

    profiles = payload["profiles"]
    assert profiles, "the org must expose the seeded runner profile"
    assert profiles[0]["provider_type"] == "runner_docker"
    assert profiles[0]["health_state"] == "engine_unreachable"
    assert profiles[0]["available"] is False

    preflight = payload["concurrency"]["preflight"]
    assert preflight["state"] == "unknown"


async def test_rig_up_maps_to_healthy_and_profile_offered(
    client: Any,
    probe_session_factory: Any,
    test_profile: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 2: the socket-proxy engine (rig up) maps to healthy/offered.

    The pinned image is present on the outer store (pre-pulled locally, the
    same seeding the CI job performs), the tick records ``healthy``, and the
    strip shows the profile offered with the resource preflight ``ok``.
    """
    proxy_host = _require_proxy()
    await _ensure_image_present(None, _IMAGE_REF)
    monkeypatch.setenv("MODULO_DOCKER_HOST", f"tcp://{proxy_host}")

    result = await run_runner_health_probe(probe_session_factory, machine_id=_MACHINE_ID)
    assert result["reachable"] is True
    assert result["transitions"] == 0
    assert result["orgs_failed"] == 0

    payload = await _strip(client)
    assert payload["aggregate_state"] == "healthy"
    machines = payload["machines"]
    assert machines, "the strip must expose at least the probed machine"
    assert machines[0]["machine_id"] == _MACHINE_ID
    assert machines[0]["state"] == "healthy"
    assert machines[0]["engine_reachable"] is True
    assert machines[0]["images_present"] is True
    assert machines[0]["image_checks"] == {_IMAGE_REF: True}
    assert machines[0]["probe_error"] is None
    assert machines[0]["engine_info"].get("cpu_count") is not None
    assert machines[0]["engine_info"].get("mem_total_mb") is not None

    profiles = payload["profiles"]
    assert profiles, "the org must expose the seeded runner profile"
    assert profiles[0]["provider_type"] == "runner_docker"
    assert profiles[0]["health_state"] == "healthy"
    assert profiles[0]["available"] is True

    preflight = payload["concurrency"]["preflight"]
    assert preflight["state"] == "ok"
    assert preflight["engine_cpu_count"] is not None
    assert preflight["engine_mem_total_mb"] is not None


async def test_engine_kill_flips_strip_and_emits_notification_and_error_event(
    client: Any,
    probe_session_factory: Any,
    superuser_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_profile: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 3: killing the nested dind engine flips the strip and alerts.

    With a workspace provisioned inside dind, the first tick reads healthy;
    after killing the dind engine the second tick reads ``engine_unreachable``
    and the healthy→unreachable transition emits BOTH an in-app notification
    (category ``runner``) and an error-dashboard event
    (``signal=runner_unavailable``) — asserted as +1 count deltas so prior
    rows never matter.
    """
    dind_host = _require_dind()
    monkeypatch.setenv("MODULO_DOCKER_HOST", dind_host)
    await _wait_for_engine_up(dind_host)
    await _ensure_image_present(dind_host, _IMAGE_REF)

    provider = DockerRuntimeProvider(docker_host=dind_host, default_image=_IMAGE_REF)
    ref: str | None = None
    try:
        for _attempt in range(2):
            try:
                ref = await provider.create_workspace(_workspace_spec())
                break
            except aiodocker.exceptions.DockerError:
                await asyncio.sleep(2.0)
        assert ref is not None, "workspace could not be provisioned inside dind"
        status = await provider.get_workspace_status(ref)
        assert status == "running"

        first = await run_runner_health_probe(probe_session_factory, machine_id=_MACHINE_ID)
        assert first["reachable"] is True

        await _kill_local_container_by_label(_DINO_LABEL)
        await _wait_for_engine_down(dind_host)
    finally:
        if ref is not None:
            with contextlib.suppress(Exception):
                await provider.destroy_workspace(ref)
        with contextlib.suppress(Exception):
            await provider.close()

    notifications_before = await _count_notifications(superuser_engine, test_org, "runner")
    events_before = await _count_error_events(superuser_engine, test_org, "runner_unavailable")

    second = await run_runner_health_probe(probe_session_factory, machine_id=_MACHINE_ID)
    assert second["reachable"] is False
    assert second["transitions"] == 1
    assert second["orgs_failed"] == 0

    payload = await _strip(client)
    assert payload["aggregate_state"] == "engine_unreachable"
    machines = payload["machines"]
    assert machines, "the strip must expose the probed machine"
    assert machines[0]["machine_id"] == _MACHINE_ID
    assert machines[0]["state"] == "engine_unreachable"
    assert machines[0]["engine_reachable"] is False
    assert payload["profiles"][0]["health_state"] == "engine_unreachable"
    assert payload["profiles"][0]["available"] is False
    preflight = payload["concurrency"]["preflight"]
    assert preflight["state"] == "unknown"

    notifications_after = await _count_notifications(superuser_engine, test_org, "runner")
    events_after = await _count_error_events(superuser_engine, test_org, "runner_unavailable")
    assert notifications_after - notifications_before == 1
    assert events_after - events_before == 1
