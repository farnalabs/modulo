"""Cross-tenant data isolation integration tests.

Tests RLS enforcement and system admin cross-tenant operations
through the HTTP API layer using a real Postgres via Testcontainers.
"""

import json
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.core.feature_flags import LicenseData, LicenseKeyTier
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _bypass_triggers(conn: AsyncConnection) -> AsyncGenerator[None, None]:
    """Temporarily disable tenant-enforcement triggers for one seed transaction.

    Several tests intentionally seed *referentially non-compliant* cross-org
    rows (e.g. a pipeline snapshot owned by org A referencing org B's pipeline)
    to model the pre-hardening state and prove the IDOR deny paths. The
    ``enforce_same_organisation`` triggers (migration 0110) legitimately reject
    such rows, so seeding must run with ``session_replication_role = replica``
    (which disables triggers). This mirrors how production backfills cross-org
    rows with a BYPASSRLS/maintenance role.

    ``SET LOCAL`` is scoped to the current transaction and is automatically
    reset when the transaction ends (commit or rollback), so no explicit restore
    is performed here. Restoring on the error path would raise a secondary
    "current transaction is aborted" error that masks the real seed failure.
    """
    await conn.execute(text("SET LOCAL session_replication_role = replica"))
    yield


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": name,
                "slug": f"{name}-{org_id.hex[:8]}",
            },
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    """Create an account + org membership, or reuse an existing account by email.

    The fixed test emails (``admin-a@test.local`` / ``admin-b@test.local``) are
    seeded by multiple integration modules. Under pytest-xdist (`-n 2`) the
    modules run against a shared Postgres, so the second INSERT violates the
    unique ``accounts.email`` constraint. Reusing an existing account keeps the
    seeding idempotent while preserving the fixed emails the tests depend on.
    """
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(
            text("SELECT id FROM accounts WHERE email = :email"),
            {"email": email},
        )
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, "
                    "auth_provider, active, password_hash) "
                    "VALUES (:id, :email, :name, 'local', true, 'hash')",
                ),
                {
                    "id": str(account_id),
                    "email": email,
                    "name": f"Admin {email}",
                },
            )

        # The account may already exist from another module's seeding (possibly
        # in a different org). Ensure it has an org_membership for THIS org.
        membership = await conn.execute(
            text(
                "SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid",
            ),
            {"aid": str(account_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')",
                ),
                {
                    "mid": str(uuid.uuid4()),
                    "aid": str(account_id),
                    "oid": str(org_id),
                },
            )
    return account_id


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :desc, :uid, 5, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')",
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


async def _seed_pipeline_snapshot(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_version: int = 1,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin(), _bypass_triggers(conn):
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, :version, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "version": snapshot_version,
            },
        )
    return snapshot_id


async def _seed_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    run_number: int = 1,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    snapshot_id = await _seed_pipeline_snapshot(db_engine, org_id, pipeline_id, snapshot_version=run_number)
    async with db_engine.connect() as conn, conn.begin(), _bypass_triggers(conn):
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, "
                "snapshot_id, status, trigger_type, langgraph_thread_id, "
                "input_hash, run_number) "
                "VALUES (:id, :oid, :pid, :sid, 'complete', 'manual', "
                ":thread, :hash, :rn)",
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": f"thread-{run_id.hex}",
                "hash": "0" * 64,
                "rn": run_number,
            },
        )
    return run_id


async def _seed_eval_definition(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    node_id: uuid.UUID | None = None,
) -> uuid.UUID:
    eval_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin(), _bypass_triggers(conn):
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, "
                "node_id, name, eval_type, config_json, failure_behaviour, "
                "suite_id, account_id) "
                "VALUES (:id, :oid, :pid, :nid, :name, 'regex', '{}'::json, "
                "'warn', NULL, :uid)",
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "nid": str(node_id) if node_id else None,
                "name": name,
                "uid": str(user_id),
            },
        )
    return eval_id


async def _seed_sso_provider(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    provider_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO sso_providers (id, organisation_id, provider_type, name, "
                "client_id, discovery_url, enabled, auto_provision, default_role) "
                "VALUES (:id, :oid, 'oidc', :name, 'client-a', "
                "'https://idp.example.com/.well-known/openid-configuration', "
                "true, true, 'runner')",
            ),
            {"id": str(provider_id), "oid": str(org_id), "name": name},
        )
    return provider_id


async def _seed_pipeline_with_nodes(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    node_id: uuid.UUID,
) -> uuid.UUID:
    pipeline_id = await _seed_pipeline(db_engine, org_id, user_id, name)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE pipelines SET graph_nodes_json = :nodes WHERE id = :pid"),
            {
                "nodes": json.dumps([{"id": str(node_id), "name": "eval-node"}]),
                "pid": str(pipeline_id),
            },
        )
    return pipeline_id


async def _seed_trigger(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, (:config)::json, :uid)"
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "config": json.dumps({"hmac_secret": uuid.uuid4().hex}),
                "uid": str(user_id),
            },
        )
    return trigger_id


async def _seed_trigger_event(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    *,
    received_at: datetime,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO trigger_events (id, organisation_id, trigger_id, trigger_type, "
                "raw_payload_hash, validation_result, received_at) "
                "VALUES (:id, :oid, :tid, 'webhook', :hash, 'accepted', :received_at)"
            ),
            {
                "id": str(event_id),
                "oid": str(org_id),
                "tid": str(trigger_id),
                # 64-char payload hash (two uuid4 hex halves).
                "hash": uuid.uuid4().hex + uuid.uuid4().hex,
                "received_at": received_at,
            },
        )
    return event_id


# ---------------------------------------------------------------------------
# HTTP client fixture — FastAPI app wired to the testcontainer database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def integration_client(
    db_url: str,
    app_engine: AsyncEngine,
) -> AsyncClient:
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

    # CommunityTier does not include the ``sso`` feature flag (it is a
    # team-tier feature). The SSO admin endpoints are gated by
    # ``require_feature('sso')``; override the plan context with a licensed
    # team-tier plan so the cross-tenant SSO isolation tests can reach the
    # handlers and exercise their deny paths.
    plan = LicenseKeyTier(
        LicenseData(
            tier="team",
            features=["sso"],
            expires_at="",
            org_id="",
            raw_payload={},
            raw_key="test-license-key",
        )
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # app_engine sessions run as a non-superuser role, so the RLS policies
        # actually filter cross-org rows (the testcontainers superuser bypasses
        # RLS even under FORCE ROW LEVEL SECURITY).
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: plan

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# JWT token factory
# ---------------------------------------------------------------------------


def _token(
    org_id: uuid.UUID | None,
    user_id: uuid.UUID,
    role: str,
    is_system_admin: bool = False,
) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(user_id),
        org_role=role,
        is_system_admin=is_system_admin,
    )


# ---------------------------------------------------------------------------
# Fixtures: orgs, users, pipelines (module-scoped to reuse across tests)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "CrossTenant-OrgA")


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "CrossTenant-OrgB")


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "admin-a@test.local")


@pytest_asyncio.fixture(scope="module")
async def user_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "admin-b@test.local")


@pytest_asyncio.fixture(scope="module")
async def pipeline_a(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_a, user_a, "CrossTenant-PipelineA")


@pytest_asyncio.fixture(scope="module")
async def pipeline_b(
    db_engine: AsyncEngine,
    org_b: uuid.UUID,
    user_b: uuid.UUID,
) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_b, user_b, "CrossTenant-PipelineB")


@pytest_asyncio.fixture(scope="module")
async def trigger_a(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    pipeline_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    return await _seed_trigger(db_engine, org_a, pipeline_a, user_a)


@pytest_asyncio.fixture(scope="module")
async def trigger_b(
    db_engine: AsyncEngine,
    org_b: uuid.UUID,
    pipeline_b: uuid.UUID,
    user_b: uuid.UUID,
) -> uuid.UUID:
    return await _seed_trigger(db_engine, org_b, pipeline_b, user_b)


@pytest_asyncio.fixture(scope="module")
async def sso_provider_b(
    db_engine: AsyncEngine,
    org_b: uuid.UUID,
) -> uuid.UUID:
    return await _seed_sso_provider(db_engine, org_b, "OrgB-Provider")


@pytest_asyncio.fixture(scope="module")
async def run_b(
    db_engine: AsyncEngine,
    org_b: uuid.UUID,
    pipeline_b: uuid.UUID,
) -> uuid.UUID:
    return await _seed_run(db_engine, org_b, pipeline_b, run_number=1)


@pytest_asyncio.fixture(scope="module")
async def run_a_cross_pipeline(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    pipeline_b: uuid.UUID,
) -> uuid.UUID:
    """A run owned by org A but pointing at org B's pipeline.

    Seeded directly (bypassing RLS) to model the pre-hardening state where a
    run's pipeline was not ownership-checked. The from-run endpoint must reject
    this with the new pipeline-ownership check.
    """
    return await _seed_run(db_engine, org_a, pipeline_b, run_number=2)


@pytest.fixture(scope="module")
def coverage_node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture(scope="module")
async def coverage_pipeline_a(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
    coverage_node_id: uuid.UUID,
) -> uuid.UUID:
    return await _seed_pipeline_with_nodes(db_engine, org_a, user_a, "CrossTenant-CoveragePipelineA", coverage_node_id)


@pytest_asyncio.fixture(scope="module")
async def coverage_eval_a(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    coverage_pipeline_a: uuid.UUID,
    user_a: uuid.UUID,
    coverage_node_id: uuid.UUID,
) -> uuid.UUID:
    return await _seed_eval_definition(
        db_engine, org_a, coverage_pipeline_a, user_a, "OrgA-CoverageEval", node_id=coverage_node_id
    )


@pytest_asyncio.fixture(scope="module")
async def coverage_eval_b_cross(
    db_engine: AsyncEngine,
    org_b: uuid.UUID,
    coverage_pipeline_a: uuid.UUID,
    user_b: uuid.UUID,
    coverage_node_id: uuid.UUID,
) -> uuid.UUID:
    """An eval definition on org A's coverage pipeline owned by org B."""
    return await _seed_eval_definition(
        db_engine, org_b, coverage_pipeline_a, user_b, "OrgB-CrossCoverageEval", node_id=coverage_node_id
    )


# ===================================================================
# Test 1: Org data isolation via RLS
# ===================================================================


class TestOrgDataIsolation:
    """Org A must not see Org B's data, and vice versa."""

    async def test_org_a_cannot_see_org_b_pipelines(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_b) not in ids, "OrgA should not see OrgB's pipeline"

    async def test_org_b_cannot_see_org_a_pipelines(
        self,
        integration_client: AsyncClient,
        org_b: uuid.UUID,
        user_b: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_b, user_b, "admin")
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_a) not in ids, "OrgB should not see OrgA's pipeline"

    async def test_org_a_sees_own_pipeline(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_a) in ids, "OrgA should see its own pipeline"


# ===================================================================
# Test 2: System admin can access any org's data
# ===================================================================


class TestSystemAdminAccess:
    """System admin (no org_id claim) bypasses tenant scoping via admin routes."""

    async def test_system_admin_lists_all_orgs(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
    ) -> None:
        sys_admin_id = uuid.uuid4()
        token = _token(
            org_id=None,
            user_id=sys_admin_id,
            role="system_admin",
            is_system_admin=True,
        )
        resp = await integration_client.get(
            "/api/v1/admin/orgs",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        org_ids = {o["id"] for o in resp.json()}
        assert str(org_a) in org_ids, "System admin should see org A"
        assert str(org_b) in org_ids, "System admin should see org B"


# ===================================================================
# Test 3: Org admin cannot access other org's admin endpoints
# ===================================================================


class TestOrgAdminCrossOrgForbidden:
    """Org-scoped user gets 403 on admin routes for a different org."""

    async def test_org_admin_gets_403_on_admin_create_user_in_other_org(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            f"/api/v1/admin/orgs/{org_b}/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "cross-org@test.local",
                "display_name": "Cross Org",
                "password": "testpassword123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


# ===================================================================
# Test 4: System admin uses explicit org_id parameter
# ===================================================================


# ===================================================================
# Test 5: Cross-org single-resource fetch returns 404
# ===================================================================


class TestCrossOrgSingleResourceFetch:
    """Getting a resource by ID from another org must return 404 (not 403)."""

    async def test_get_other_org_pipeline_by_id_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_b}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, (
            f"Expected 404 for cross-org pipeline fetch, got {resp.status_code}: {resp.text}"
        )

    async def test_get_own_org_pipeline_by_id_succeeds(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_a}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200 for own org pipeline fetch, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["id"] == str(pipeline_a)


# ===================================================================
# Test 5b: SECURITY #1185 — cross-tenant password-account adoption denied
# ===================================================================


class TestCrossTenantPasswordAdoptionDenied:
    """An admin must not adopt a local-password account that lives in another org.

    ``admin_create_user`` (admin.py /admin/users) must 409 with the
    ``EMAIL_ACCOUNT_EXISTS ... Password-based adoption is not allowed`` detail
    when the email already exists in a DIFFERENT org with a local password.
    """

    async def test_admin_create_user_rejects_cross_org_local_password_account(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        # user_b lives in org_b with a local password; admin of org_a must not
        # be able to adopt that account via /admin/users.
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "admin-b@test.local",
                "display_name": "Cross Org Adoption",
                "password": "testpassword123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
        assert "Password-based adoption is not allowed" in resp.json()["detail"]


# ===================================================================
# Test 5c: SECURITY #1186/#1188 — cross-tenant membership guards on admin
#         user mutation return 404
# ===================================================================


class TestCrossTenantMembershipGuards:
    """admin_update_user / admin_reactivate_user / admin_reset_password must 404
    when the target user has NO membership in the caller's org."""

    async def test_admin_update_user_rejects_cross_org_account(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.put(
            f"/api/v1/admin/users/{user_b}",
            headers={"Authorization": f"Bearer {token}"},
            json={"org_role": "runner"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        assert "User not found in this organisation" in resp.json()["detail"]

    async def test_admin_reactivate_user_rejects_cross_org_account(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            f"/api/v1/admin/users/{user_b}/reactivate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        assert "User not found in this organisation" in resp.json()["detail"]

    async def test_admin_reset_password_rejects_cross_org_account(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            f"/api/v1/admin/users/{user_b}/reset-password",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        assert "User not found in this organisation" in resp.json()["detail"]

    async def test_admin_update_user_succeeds_for_own_org(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.put(
            f"/api/v1/admin/users/{user_a}",
            headers={"Authorization": f"Bearer {token}"},
            json={"org_role": "admin"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


# ===================================================================
# Test 6: System admin uses explicit org_id parameter
# ===================================================================


class TestSystemAdminExplicitOrgParam:
    """System admin's JWT org_id is ignored; the path org_id is used."""

    async def test_system_admin_can_create_user_in_any_org(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
    ) -> None:
        sys_admin_id = uuid.uuid4()
        token = _token(
            org_id=None,
            user_id=sys_admin_id,
            role="system_admin",
            is_system_admin=True,
        )
        resp = await integration_client.post(
            f"/api/v1/admin/orgs/{org_a}/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "sysadmin-created@test.local",
                "display_name": "Created By SysAdmin",
                "password": "securepassword123",
                "org_role": "operator",
            },
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["email"] == "sysadmin-created@test.local"
        assert data["org_role"] == "operator"


# ===================================================================
# Test 7: Evals cross-tenant pipeline IDOR (POST /api/v1/evals)
# ===================================================================


class TestEvalsCreateCrossTenant:
    """Creating eval definitions against another org's pipeline must fail."""

    async def test_create_eval_against_other_org_pipeline_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            "/api/v1/evals",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "pipeline_id": str(pipeline_b),
                "name": "cross-tenant-eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 404, (
            f"Expected 404 for eval against another org's pipeline, got {resp.status_code}: {resp.text}"
        )

    async def test_create_eval_against_own_org_pipeline_succeeds(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            "/api/v1/evals",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "pipeline_id": str(pipeline_a),
                "name": "own-org-eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        assert resp.json()["pipeline_id"] == str(pipeline_a)


# ===================================================================
# Test 8: Evals from-run cross-tenant pipeline IDOR
# ===================================================================


class TestEvalsCreateFromRunCrossTenant:
    """Creating a run-derived eval against a cross-tenant pipeline must fail."""

    async def test_from_run_referencing_other_org_pipeline_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        run_a_cross_pipeline: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            "/api/v1/evals/from-run",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "run_id": str(run_a_cross_pipeline),
                "node_id": str(uuid.uuid4()),
                "name": "from-run-cross-tenant",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 404, (
            f"Expected 404 for from-run against another org's pipeline, got {resp.status_code}: {resp.text}"
        )

    async def test_from_run_other_org_run_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        run_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            "/api/v1/evals/from-run",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "run_id": str(run_b),
                "node_id": str(uuid.uuid4()),
                "name": "from-run-other-org-run",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 404, f"Expected 404 for other-org run, got {resp.status_code}: {resp.text}"


# ===================================================================
# Test 9: SSO provider cross-tenant IDOR
# ===================================================================


class TestSsoProviderCrossTenant:
    """Org A admin must not access Org B's SSO provider through any surface."""

    async def test_list_providers_does_not_return_other_org_provider(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/admin/sso/providers",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        provider_ids = {p["id"] for p in resp.json()}
        assert str(sso_provider_b) not in provider_ids, "OrgA should not see OrgB's SSO provider in the list"

    async def test_get_group_mappings_other_org_provider_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/admin/sso/providers/{sso_provider_b}/group-mappings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    async def test_update_other_org_provider_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.put(
            f"/api/v1/admin/sso/providers/{sso_provider_b}",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "hijacked"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    async def test_toggle_other_org_provider_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.put(
            f"/api/v1/admin/sso/providers/{sso_provider_b}/toggle",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    async def test_delete_other_org_provider_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.delete(
            f"/api/v1/admin/sso/providers/{sso_provider_b}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    async def test_test_connection_other_org_provider_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            f"/api/v1/admin/sso/providers/{sso_provider_b}/test",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    async def test_set_group_mappings_other_org_provider_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        sso_provider_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.put(
            f"/api/v1/admin/sso/providers/{sso_provider_b}/group-mappings",
            headers={"Authorization": f"Bearer {token}"},
            json={"mappings": []},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


# ===================================================================
# Test 10: eval_coverage org filter
# ===================================================================


class TestEvalCoverageOrgFilter:
    """Another org's eval definitions must not appear in a pipeline's coverage."""

    async def test_coverage_excludes_other_org_eval_definitions(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        coverage_pipeline_a: uuid.UUID,
        coverage_node_id: uuid.UUID,
        coverage_eval_a: uuid.UUID,
        coverage_eval_b_cross: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/evals/coverage?pipeline_id={coverage_pipeline_a}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        node = next(
            (n for n in resp.json()["nodes"] if n["node_id"] == str(coverage_node_id)),
            None,
        )
        assert node is not None, "Coverage should include the seeded node"
        assert node["eval_count"] == 1, (
            f"Coverage should count only OrgA's eval definition (1), got {node['eval_count']}"
        )


# ===================================================================
# Test 11: FAR-523 — trigger_events retention RLS context
# ===================================================================


class TestTriggerEventRetentionRls:
    """FAR-523: cross-org trigger_events retention must run on the system
    (BYPASSRLS) session factory, and an org-scoped app-role session without an
    RLS org context must see ZERO rows — the silent no-op that both the
    removed web-process cleanup loop and the pre-fix notifier endpoint read
    suffered on Postgres."""

    async def test_system_cleanup_purges_old_events_across_orgs(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        trigger_a: uuid.UUID,
        trigger_b: uuid.UUID,
    ) -> None:
        """The SAQ ``trigger_events_cleanup`` system cron purges BOTH orgs'
        expired events through a BYPASSRLS-equivalent factory. The plain
        ``modulo_app`` factory (NOBYPASSRLS, no ``app.organisation_id``) would
        silently match zero rows and delete nothing."""
        import modulo.core.saq_worker as sw

        old_a = await _seed_trigger_event(
            db_engine, org_a, trigger_a, received_at=datetime.now(UTC) - timedelta(days=100)
        )
        old_b = await _seed_trigger_event(
            db_engine, org_b, trigger_b, received_at=datetime.now(UTC) - timedelta(days=100)
        )

        # The testcontainers superuser bypasses RLS entirely — the same
        # effective semantics as the production modulo_system (BYPASSRLS)
        # role the system session factory connects with. autobegin=False
        # mirrors the real system factory (the cron opens per-batch
        # transactions itself).
        system_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        with patch.object(sw, "_make_system_session_factory", return_value=system_factory):
            result = await sw.trigger_events_cleanup({})

        assert result["deleted"] >= 2, "both orgs' expired events must be purged"
        async with db_engine.connect() as conn:
            for event_id in (old_a, old_b):
                row = await conn.execute(
                    text("SELECT count(*) FROM trigger_events WHERE id = :eid"), {"eid": str(event_id)}
                )
                assert int(row.scalar_one()) == 0, f"expired event {event_id} must be purged cross-org"

    async def test_app_role_session_without_rls_context_sees_zero_rows(
        self,
        db_engine: AsyncEngine,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        trigger_a: uuid.UUID,
        trigger_b: uuid.UUID,
    ) -> None:
        """Prove-the-fix, both directions, against the FORCE-RLS NOBYPASSRLS
        role:

        (a) a bare app-role session with NO org context — what the removed
            web-process cleanup loop and the pre-fix notifier read ran as —
            sees ZERO trigger_events rows (the silent no-op bug class); and
        (b) the same session with ``set_rls_org(org_a)`` sees exactly org A's
            event and none of org B's.
        """
        event_a = await _seed_trigger_event(db_engine, org_a, trigger_a, received_at=datetime.now(UTC))
        event_b = await _seed_trigger_event(db_engine, org_b, trigger_b, received_at=datetime.now(UTC))

        app_factory = async_sessionmaker(app_engine, expire_on_commit=False)

        # (a) No org context: FORCE RLS filters everything.
        async with app_factory() as session, session.begin():
            visible = (await session.execute(select(TriggerEvent))).scalars().all()
            assert visible == [], "app-role session without org context must see zero trigger_events"

        # (b) With set_rls_org(org_a): exactly org A's event, org B's invisible.
        async with app_factory() as session, session.begin():
            await set_rls_org(session, org_a)
            visible_ids = {ep.id for ep in (await session.execute(select(TriggerEvent))).scalars()}
            assert event_a in visible_ids, "org A session must see its own event"
            assert event_b not in visible_ids, "org A session must not see org B's event"
