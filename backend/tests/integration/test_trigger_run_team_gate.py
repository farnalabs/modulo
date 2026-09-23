"""Integration tests for the team-scope gate on POST /api/v1/runs (trigger_run).

FAR-946 wired ``require_team_membership_or_admin_any_credential(
resolve_trigger_run_team_scope)`` as a dependency on ``trigger_run``.  The unit
test (``test_runs_team_scope.py``) mocks the session and principal; this
integration test exercises the REAL dependency chain against a real Postgres
(testcontainers) — team membership rows, RLS policies, and the full HTTP path.

Coverage:
  - non-member org runner -> denied on a team-private pipeline (404): the
    ``rls_team_isolation`` policy hides the row from a non-member, so the
    team-scope resolver SELECT returns no row and the dependency raises 404
    before the membership check.  The membership gate itself is proven at the
    wiring level (see ``TestTriggerRunTeamGateWiring``) because the 403 branch
    is unreachable end-to-end under RLS — this mirrors the established
    RLS-parity behaviour asserted in ``test_pipeline_team_visibility.py``.
  - team member -> 202
  - org-admin -> 202 for any pipeline
  - org-visible pipeline -> 202 for any org member with the role floor
  - denial body does NOT leak pipeline details (team name, visibility, etc.)
"""

import inspect
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
from modulo.settings import Settings, get_settings

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

# A single flat ``manual`` node (no agent_id) — enough for
# ``create_snapshot_from_live_graph`` to produce a runnable snapshot and for
# the ``POST /api/v1/runs`` basic graph validation (entry node exists) to pass.
_MINIMAL_GRAPH_NODES = [
    {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
    }
]


# ---------------------------------------------------------------------------
# Helpers
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


def _token(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    *,
    is_system_admin: bool = False,
) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
        is_system_admin=is_system_admin,
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
    """Create a team through the CRUD layer.

    ``teams`` has NOT NULL ``account_id`` (owner), ``notification_endpoints``
    and ``settings`` columns with no SQL server defaults, so a raw INSERT must
    supply them. The ORM ``create_team`` helper applies the Python-side defaults
    and keeps this seed in step with the model.
    """
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
    """Add a team member via the CRUD layer.

    ``team_memberships.role`` is constrained to ``viewer``/``runner``/``operator``
    (``ck_team_memberships_role``); ``member`` is not a valid value.
    """
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        await add_team_member(session, org_id=org_id, team_id=team_id, account_id=account_id, role="runner")


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    *,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, "
                "visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, (:graph)::json, 'manual_approval', :vis, :tid)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(user_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
                "vis": visibility,
                "tid": str(owner_team_id) if owner_team_id else None,
            },
        )
    return pipeline_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "TeamGate-Org")


@pytest_asyncio.fixture(scope="module")
async def admin_user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "admin@teamgate.test", role="admin")


@pytest_asyncio.fixture(scope="module")
async def member_user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "member@teamgate.test", role="runner")


@pytest_asyncio.fixture(scope="module")
async def non_member_user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    """Org member who is NOT in the team that owns the private pipeline."""
    return await _seed_user(db_engine, org, "outsider@teamgate.test", role="runner")


@pytest_asyncio.fixture(scope="module")
async def team(db_engine: AsyncEngine, org: uuid.UUID, admin_user: uuid.UUID) -> uuid.UUID:
    return await _seed_team(db_engine, org, admin_user, "ci-team")


@pytest_asyncio.fixture(scope="module")
async def _add_member_to_team(db_engine: AsyncEngine, org: uuid.UUID, team: uuid.UUID, member_user: uuid.UUID) -> None:
    await _seed_team_membership(db_engine, org, team, member_user)


@pytest_asyncio.fixture(scope="module")
async def team_private_pipeline(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    admin_user: uuid.UUID,
    team: uuid.UUID,
    _add_member_to_team: None,
) -> uuid.UUID:
    return await _seed_pipeline(
        db_engine,
        org,
        admin_user,
        "ci-pipeline-team",
        visibility="team",
        owner_team_id=team,
    )


@pytest_asyncio.fixture(scope="module")
async def org_visible_pipeline(db_engine: AsyncEngine, org: uuid.UUID, admin_user: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(
        db_engine,
        org,
        admin_user,
        "ci-pipeline-org",
        visibility="org",
        owner_team_id=None,
    )


@pytest_asyncio.fixture
async def team_gate_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client wired with the app's dependency overrides (real DB)."""

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
    """Stub the background dispatch so a 202 does not require Redis/SAQ.

    ``trigger_run`` awaits ``dispatch_run`` after the run row is committed; with
    ``redis_url=""`` in the test settings the real dispatch has no queue and
    would fail. The route returns 202 once the run exists, so the dispatch side
    effect is irrelevant to these gate assertions.
    """
    with patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTriggerRunTeamGate:
    """Denial-path integration coverage for the trigger_run team gate (FAR-946).

    These tests exercise the REAL dependency chain:
      HTTP request -> trigger_run route -> require_permission_any_credential
      -> require_team_membership_or_admin_any_credential (with
      resolve_trigger_run_team_scope) -> real DB membership check.

    Under Postgres RLS a non-member never sees a team-private pipeline row, so
    the end-to-end denial is a 404 raised by the team-scope resolver before the
    membership check. The membership gate's own 403 branch is covered by the
    unit tests (``test_runs_team_scope.py``) and asserted at the wiring level
    below.
    """

    @pytest.mark.asyncio
    async def test_non_member_denied_on_team_private(
        self,
        team_gate_client: AsyncClient,
        org: uuid.UUID,
        non_member_user: uuid.UUID,
        team_private_pipeline: uuid.UUID,
    ) -> None:
        """Non-member org runner -> denied on a team-private pipeline.

        ``rls_team_isolation`` hides the pipeline from a non-member, so the
        team-scope resolver SELECT returns no row and the dependency 404s
        (RLS-parity behaviour — see ``test_pipeline_team_visibility.py``).
        """
        token = _token(org, non_member_user, "runner")
        resp = await team_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(team_private_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_non_member_denial_body_no_leak(
        self,
        team_gate_client: AsyncClient,
        org: uuid.UUID,
        non_member_user: uuid.UUID,
        team_private_pipeline: uuid.UUID,
    ) -> None:
        """The denial body must NOT leak pipeline details (name, team, visibility)."""
        token = _token(org, non_member_user, "runner")
        resp = await team_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(team_private_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
        detail = resp.json().get("detail", "")
        # The detail must be the generic not-found message — never the pipeline
        # name, the owning team name, or the visibility value.
        assert "ci-pipeline-team" not in detail.lower()
        assert "ci-team" not in detail.lower()
        assert "team" not in detail.lower()

    @pytest.mark.asyncio
    async def test_member_can_trigger_team_private(
        self,
        team_gate_client: AsyncClient,
        org: uuid.UUID,
        member_user: uuid.UUID,
        team_private_pipeline: uuid.UUID,
    ) -> None:
        """Team member -> 202 on a team-private pipeline."""
        token = _token(org, member_user, "runner")
        resp = await team_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(team_private_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "run_id" in body
        assert body["status"] == "pending"

    @pytest.mark.asyncio
    async def test_admin_bypasses_team_gate(
        self,
        team_gate_client: AsyncClient,
        org: uuid.UUID,
        admin_user: uuid.UUID,
        team_private_pipeline: uuid.UUID,
    ) -> None:
        """Org-admin -> 202 on any pipeline (team gate bypassed)."""
        token = _token(org, admin_user, "admin")
        resp = await team_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(team_private_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_org_visible_pipeline_allowed_for_non_member(
        self,
        team_gate_client: AsyncClient,
        org: uuid.UUID,
        non_member_user: uuid.UUID,
        org_visible_pipeline: uuid.UUID,
    ) -> None:
        """Org-visible pipeline -> 202 for any org member with the role floor."""
        token = _token(org, non_member_user, "runner")
        resp = await team_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(org_visible_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_member_can_trigger_org_visible(
        self,
        team_gate_client: AsyncClient,
        org: uuid.UUID,
        member_user: uuid.UUID,
        org_visible_pipeline: uuid.UUID,
    ) -> None:
        """Team member -> 202 on an org-visible pipeline."""
        token = _token(org, member_user, "runner")
        resp = await team_gate_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(org_visible_pipeline)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"


class TestTriggerRunTeamGateWiring:
    """Wiring-level proof that the team gate is attached to ``trigger_run``.

    Under Postgres RLS the 403 branch of
    ``require_team_membership_or_admin_any_credential`` is unreachable
    end-to-end — the team-scope resolver's SELECT is filtered by
    ``rls_team_isolation``, so a non-member gets 404 before the membership
    check. A splice-and-observe test therefore cannot exercise the gate.

    Instead assert the route function declares the gate dependency tagged
    ``permission='team.membership_or_admin'`` (the tag ``_tagged_dep`` attaches
    and the ADR 047 route-introspection sweep reads). Removing the
    ``require_team_membership_or_admin_any_credential(...)`` line from
    ``trigger_run`` makes this test fail.
    """

    def test_team_gate_dependency_declared(self) -> None:
        from modulo.api.routes.runs import trigger_run

        tagged = [
            getattr(param.default, "permission", None) for param in inspect.signature(trigger_run).parameters.values()
        ]
        assert "team.membership_or_admin" in tagged, (
            "trigger_run is missing the team.membership_or_admin gate dependency"
        )
