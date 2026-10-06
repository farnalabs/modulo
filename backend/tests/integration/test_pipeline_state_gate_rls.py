"""Integration tests for the pipeline-state gate's VISIBILITY semantics (FAR-1528).

The gate (``modulo.db.crud.run._enforce_pipeline_state_gate``) reads
``archived_at``/``deleted_at`` with a raw ``text()`` statement. On Postgres
that read is scoped by the ``rls_team_isolation`` policy on ``pipelines``
(migration 0124 dropped the org-only policy on team-scoped tables, so the
policy is org-gated AND team-visibility-gated). These tests pin what each
KIND of RLS session therefore sees, against a real Postgres (testcontainers):

  * a non-member USER session cannot see a team-private pipeline at all, so
    ``POST /api/v1/runs`` is refused UPSTREAM (404 from the team-scope
    resolver) — the gate is never reached, and no run row exists;
  * a team MEMBER's request reaches the gate and comes back 409 Conflict
    (the REST mapping of ``PipelineNotRunnableError``);
  * an EXECUTION-CONTEXT session (the background fire-job shape: org scope,
    team-blind) DOES see the archived row, so ``create_run`` raises
    ``PipelineNotRunnableError``;
  * the same non-member session's direct gate read sees NO row — the gate
    never refuses a row the session cannot see (row-absent, not refusal).

The archive is the subject: soft-deleted pipelines are filtered out by
``SoftDeleteMixin`` before most callers reach ``create_run``, so an ARCHIVED
pipeline is the state that actually exercises this gate end-to-end.
"""

import json
import os
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.jwt import create_access_token
from modulo.core.exceptions import PipelineNotRunnableError
from modulo.db.crud.run import _enforce_pipeline_state_gate, create_run
from modulo.db.rls import set_rls_execution_context, set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

# A single flat ``manual`` node (no agent_id) — enough for
# ``create_snapshot_from_live_graph`` to produce a runnable snapshot.
_MINIMAL_GRAPH_NODES = [
    {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
    }
]


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/integration/test_trigger_run_team_gate.py)
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


def _token(org_id: uuid.UUID, user_id: uuid.UUID, role: str) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
        is_system_admin=False,
        client_kind="browser",
    )


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str, role: str = "admin") -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, "
                "auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": email, "name": f"User {email}"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, :role)"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id), "role": role},
        )
    return account_id


async def _seed_team(db_engine: AsyncEngine, org_id: uuid.UUID, owner_id: uuid.UUID, name: str) -> uuid.UUID:
    from modulo.db.crud.team import create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        team = await create_team(session, org_id=org_id, name=name, account_id=owner_id)
        return team.id


async def _seed_team_membership(
    db_engine: AsyncEngine, org_id: uuid.UUID, team_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        await add_team_member(session, org_id=org_id, team_id=team_id, account_id=account_id, role="runner")


async def _seed_archived_team_private_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    *,
    owner_team_id: uuid.UUID,
) -> uuid.UUID:
    """Team-private pipeline that is ALREADY archived (``archived_at`` set)."""
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, "
                "visibility, owner_team_id, archived_at) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, (:graph)::json, 'manual_approval', 'team', :tid, now())"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(user_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
                "tid": str(owner_team_id),
            },
        )
    return pipeline_id


async def _run_count_for(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM runs WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "StateGate-Org")


@pytest_asyncio.fixture(scope="module")
async def admin_user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "admin@stategate.test", role="admin")


@pytest_asyncio.fixture(scope="module")
async def member_user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "member@stategate.test", role="runner")


@pytest_asyncio.fixture(scope="module")
async def non_member_user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    """Org member who is NOT in the team that owns the private pipeline."""
    return await _seed_user(db_engine, org, "outsider@stategate.test", role="runner")


@pytest_asyncio.fixture(scope="module")
async def team(db_engine: AsyncEngine, org: uuid.UUID, admin_user: uuid.UUID) -> uuid.UUID:
    return await _seed_team(db_engine, org, admin_user, "state-gate-team")


@pytest_asyncio.fixture(scope="module")
async def _add_member_to_team(db_engine: AsyncEngine, org: uuid.UUID, team: uuid.UUID, member_user: uuid.UUID) -> None:
    await _seed_team_membership(db_engine, org, team, member_user)


@pytest_asyncio.fixture(scope="module")
async def archived_team_private_pipeline(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    admin_user: uuid.UUID,
    team: uuid.UUID,
    _add_member_to_team: None,
) -> uuid.UUID:
    return await _seed_archived_team_private_pipeline(
        db_engine,
        org,
        admin_user,
        "archived-team-pipeline",
        owner_team_id=team,
    )


@pytest_asyncio.fixture
async def state_gate_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client wired with the app's dependency overrides (real DB, RLS on)."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _AllFeatures()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_run_dispatch() -> AsyncGenerator[None, None]:
    """Stub the background dispatch so an unexpected 202 needs no Redis/SAQ."""
    with patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineStateGateVisibility:
    @pytest.mark.asyncio
    async def test_non_member_is_refused_upstream_with_404(
        self,
        state_gate_client: AsyncClient,
        org: uuid.UUID,
        non_member_user: uuid.UUID,
        archived_team_private_pipeline: uuid.UUID,
        db_engine: AsyncEngine,
    ) -> None:
        """A non-member RLS session never SEES a team-private pipeline, so the
        team-scope resolver 404s BEFORE ``create_run`` — the gate is not the
        refuser here, and no run row is created."""
        token = _token(org, non_member_user, "runner")
        resp = await state_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(archived_team_private_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        assert await _run_count_for(db_engine, archived_team_private_pipeline) == 0

    @pytest.mark.asyncio
    async def test_team_member_gets_409_from_the_gate(
        self,
        state_gate_client: AsyncClient,
        org: uuid.UUID,
        member_user: uuid.UUID,
        archived_team_private_pipeline: uuid.UUID,
        db_engine: AsyncEngine,
    ) -> None:
        """A team member CAN see the archived row, so the request reaches
        ``create_run`` and the gate refuses it: 409 Conflict (the REST mapping
        of ``PipelineNotRunnableError``), never a 500."""
        token = _token(org, member_user, "runner")
        resp = await state_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(archived_team_private_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
        assert "archived" in resp.json()["detail"].lower()
        assert await _run_count_for(db_engine, archived_team_private_pipeline) == 0

    @pytest.mark.asyncio
    async def test_execution_context_session_raises_pipeline_not_runnable(
        self,
        app_engine: AsyncEngine,
        org: uuid.UUID,
        archived_team_private_pipeline: uuid.UUID,
    ) -> None:
        """The background fire-job shape (org scope + execution context, team
        blind) sees the archived row, so ``create_run`` refuses it with
        ``PipelineNotRunnableError`` — the typed error the SAQ fire jobs and
        the agent_signal path catch and turn into a skip."""
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org)
            await set_rls_execution_context(session)

            with pytest.raises(PipelineNotRunnableError) as excinfo:
                await create_run(
                    session,
                    org_id=org,
                    pipeline_id=archived_team_private_pipeline,
                    snapshot_id=uuid.uuid4(),
                    trigger_type="cron",
                    input_payload={},
                )

        assert excinfo.value.state == "archived"
        assert excinfo.value.pipeline_id == archived_team_private_pipeline

    @pytest.mark.asyncio
    async def test_non_member_gate_read_sees_no_row(
        self,
        app_engine: AsyncEngine,
        org: uuid.UUID,
        non_member_user: uuid.UUID,
        archived_team_private_pipeline: uuid.UUID,
    ) -> None:
        """RLS parity for the gate's own read: a user session that cannot see
        the team-private row gets ROW ABSENT, so the gate does NOT refuse
        (refusal would leak that the row exists)."""
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org)
            await set_rls_user_context(session, non_member_user, "runner")

            await _enforce_pipeline_state_gate(session, archived_team_private_pipeline, org)
