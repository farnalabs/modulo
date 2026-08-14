"""Integration test for demo first-run experience.

Tests the _seed_demo_data lifespan function, demo user authentication,
onboarding flow, and demo pipeline lifecycle with a real database via testcontainers.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Disable auth rate limiting and Redis before any imports from modulo.api.main
# trigger middleware initialization with default settings.
os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

# Valid Fernet key (matches the FERNET_KEY default set in integration/conftest.py
# and ci.yml). _seed_demo_data encrypts the demo model-backend credentials with
# Fernet, so any Settings constructed here must carry a valid key — "a" * 32 is
# not url-safe base64 and raises ValueError.
_VALID_FERNET_KEY = "vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI="


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The onboarding route stores state at backend-root/.onboarding-state.json
# which is 4 levels up from its own file (backend/src/modulo/api/routes/onboarding.py)
_ONBOARDING_STATE_PATH = Path(__file__).resolve().parent.parent.parent / ".onboarding-state.json"


def _clean_onboarding_state() -> None:
    _ONBOARDING_STATE_PATH.unlink(missing_ok=True)


# Tables with a RESTRICT (blocking) foreign key to accounts.id that the demo
# seed data or schema seed tests may populate for the demo account. Child rows
# must be deleted before the account itself or the DELETE fails with
# ForeignKeyViolationError. SET NULL / CASCADE FKs need no explicit cleanup.
# Ordered so a table is deleted after the tables that reference it (RESTRICT
# FKs between these tables: agents -> schema_versions/model_backends/
# parameter_schemas, composite_templates & parameter_sets -> parameter_schemas,
# nodes/eval_definitions/triggers -> pipelines, connector_instances/
# environment_profiles/lifecycle_maps -> teams). Statements are fully static
# (no interpolation) so ruff S608 cannot flag them.
_DEMO_ACCOUNT_CHILD_SQL: tuple[str, ...] = (
    "DELETE FROM feedback_records WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM agents WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM composite_templates WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM parameter_sets WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM schema_versions WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM parameter_schemas WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM node_categories WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM nodes WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM eval_definitions WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM triggers WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM org_api_keys WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM connector_instances WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM environment_profiles WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM lifecycle_maps WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM mcp_setup_tokens WHERE created_by IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM pipeline_folders WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM saved_views WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM model_backends WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM pipelines WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM teams WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
    "DELETE FROM schemas WHERE account_id IN (SELECT id FROM accounts WHERE email = 'demo')",
)

# Runs, snapshots and variant groups reference pipelines with RESTRICT FKs.
# Workspace leases reference runs and environment profiles with RESTRICT FKs.
# None of these have an account_id (or it is SET NULL), so they are keyed off
# the demo account's pipelines / environment profiles instead of the account.
# workspace_leases must be deleted before both runs and environment_profiles.
_DEMO_PIPELINE_CHILD_SQL: tuple[str, ...] = (
    "DELETE FROM workspace_leases WHERE run_id IN (SELECT id FROM runs WHERE "
    "pipeline_id IN (SELECT id FROM pipelines WHERE account_id IN "
    "(SELECT id FROM accounts WHERE email = 'demo'))) OR environment_profile_id "
    "IN (SELECT id FROM environment_profiles WHERE account_id IN "
    "(SELECT id FROM accounts WHERE email = 'demo'))",
    "DELETE FROM runs WHERE pipeline_id IN ("
    "SELECT id FROM pipelines WHERE account_id IN "
    "(SELECT id FROM accounts WHERE email = 'demo'))",
    "DELETE FROM pipeline_snapshots WHERE pipeline_id IN ("
    "SELECT id FROM pipelines WHERE account_id IN "
    "(SELECT id FROM accounts WHERE email = 'demo'))",
    "DELETE FROM variant_groups WHERE pipeline_id IN ("
    "SELECT id FROM pipelines WHERE account_id IN "
    "(SELECT id FROM accounts WHERE email = 'demo'))",
)


async def _delete_demo_accounts(conn: Any) -> None:
    """Delete every demo account after first removing rows that reference it.

    Integration tests run with ``pytest -n 2`` against a shared Postgres, so a
    prior module (demo seed data, schema seed tests) may have created child
    rows (e.g. ``schemas``) that FK to the ``demo`` account. Deleting the
    account before those child rows raises ForeignKeyViolationError. This
    helper clears every RESTRICT-FK child row first, then the account.
    """
    for sql in _DEMO_PIPELINE_CHILD_SQL:
        await conn.execute(text(sql))
    for sql in _DEMO_ACCOUNT_CHILD_SQL:
        await conn.execute(text(sql))
    await conn.execute(text("DELETE FROM accounts WHERE email = 'demo'"))


# ---------------------------------------------------------------------------
# Module-level autouse: clean onboarding state before every test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _clean_onboarding_before_test(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    """Reset onboarding state before every test (file-based + DB progress rows)."""
    _clean_onboarding_state()
    async with db_engine.connect() as conn:
        await conn.execute(
            text("DELETE FROM onboarding_progress WHERE organisation_id = :oid"),
            {"oid": str(test_org)},
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Fixtures — inherited from top-level conftest: test_org
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def test_demo_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    """Create a demo user with known password for auth tests.

    Cleans up any existing 'demo' accounts first to avoid MultipleResultsFound
    from seed data tests that also create accounts with email 'demo'.
    """
    from modulo.auth.passwords import hash_password

    async with db_engine.connect() as conn:
        await _delete_demo_accounts(conn)
        await conn.commit()

    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, "
                "password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, :hash, 'local', true)",
            ),
            {
                "id": str(account_id),
                "email": "demo",
                "name": "Demo User",
                "hash": hash_password("demo"),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'viewer')",
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(test_org),
            },
        )
    return account_id


@pytest_asyncio.fixture
async def demo_pipeline(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_demo_user: uuid.UUID,
) -> uuid.UUID:
    """Create a demo pipeline that can be viewed."""
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
                "oid": str(test_org),
                "name": "PRD to Requirements",
                "desc": "Demo pipeline: convert PRDs to requirements documents",
                "uid": str(test_demo_user),
            },
        )
    return pipeline_id


@pytest_asyncio.fixture
async def demo_client(
    db_url: str,
    db_engine: AsyncEngine,
) -> AsyncClient:
    """FastAPI AsyncClient wired to the testcontainer database."""
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key=_VALID_FERNET_KEY,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: db_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: _seed_demo_data
# ---------------------------------------------------------------------------


async def test_seed_demo_data_creates_demo_user(db_engine: AsyncEngine, db_url: str) -> None:
    """Call _seed_demo_data with MODULO_DEMO_MODE=true and verify the demo user is created."""
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key=_VALID_FERNET_KEY,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
    )

    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Seed Demo Test Org",
                "slug": f"seed-demo-{org_id.hex[:8]}",
            },
        )

    import modulo.api.dependencies as deps

    deps._engine = None
    deps._session_factory = None

    await _seed_demo_data(settings)

    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT email, display_name FROM accounts WHERE email = 'demo'"),
        )
        row = result.one_or_none()
        assert row is not None, "Demo user was not created by _seed_demo_data"
        _email, display_name = row
        assert display_name == "Demo User"

    # Clean up the seed-created user to avoid cross-test contamination
    async with db_engine.connect() as conn:
        await _delete_demo_accounts(conn)
        await conn.commit()

    deps._engine = None
    deps._session_factory = None


async def test_seed_demo_data_runs_to_completion(db_engine: AsyncEngine, db_url: str) -> None:
    """_seed_demo_data should complete without crashing.

    The shared integration database always contains at least the
    session-scoped ``test_org`` organisation (pulled in by the module's
    autouse onboarding-cleanup fixture), so the org-free early-return path in
    ``_seed_demo_data`` cannot be exercised here — this test only verifies the
    seeder runs to completion without raising.
    """
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key=_VALID_FERNET_KEY,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
    )

    import modulo.api.dependencies as deps

    deps._engine = None
    deps._session_factory = None

    await _seed_demo_data(settings)

    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT email FROM accounts WHERE email = 'demo'"),
        )
        row = result.one_or_none()
        assert row is not None, "Demo user was not created by _seed_demo_data"

    # Clean up the seed-created user to avoid cross-test contamination
    async with db_engine.connect() as conn:
        await _delete_demo_accounts(conn)
        await conn.commit()

    deps._engine = None
    deps._session_factory = None


async def test_seed_demo_data_skipped_when_disabled(db_engine: AsyncEngine, db_url: str) -> None:
    """_seed_demo_data should be a no-op when MODULO_DEMO_MODE is False."""
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key=_VALID_FERNET_KEY,
        modulo_demo_mode=False,
        modulo_csrf_enabled=False,
    )

    # Use a unique email for this test to avoid cross-test contamination

    # First, clean the demo user from previous test
    async with db_engine.connect() as conn:
        await _delete_demo_accounts(conn)
        await conn.commit()

    import modulo.api.dependencies as deps

    deps._engine = None
    deps._session_factory = None

    await _seed_demo_data(settings)

    deps._engine = None
    deps._session_factory = None

    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT email FROM accounts WHERE email = 'demo'"),
        )
        assert result.one_or_none() is None, "No demo user should exist when MODULO_DEMO_MODE is disabled"


async def test_seed_demo_data_idempotent(db_engine: AsyncEngine, db_url: str) -> None:
    """Calling _seed_demo_data twice should not create duplicate users."""
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key=_VALID_FERNET_KEY,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
    )

    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Idempotent Test Org",
                "slug": f"idempotent-{org_id.hex[:8]}",
            },
        )

    import modulo.api.dependencies as deps

    deps._engine = None
    deps._session_factory = None

    await _seed_demo_data(settings)
    await _seed_demo_data(settings)

    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM accounts WHERE email = 'demo'"),
        )
        count = result.scalar()
        assert count == 1, f"Expected 1 demo user, got {count}"

    # Clean up the seed-created user to avoid cross-test contamination
    async with db_engine.connect() as conn:
        await _delete_demo_accounts(conn)
        await conn.commit()

    deps._engine = None
    deps._session_factory = None


# ---------------------------------------------------------------------------
# Tests: demo user authentication
# ---------------------------------------------------------------------------


async def _demo_login(client: AsyncClient) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": "demo", "password": "demo"})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


class TestDemoAuth:
    """Test that the demo user can authenticate via the auth API."""

    @pytest_asyncio.fixture(autouse=True)
    async def _ensure_demo_user(self, test_demo_user: uuid.UUID) -> None:
        pass

    async def test_demo_user_can_login(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        assert isinstance(token, str)
        assert len(token) > 20

    async def test_demo_user_invalid_password(self, demo_client: AsyncClient) -> None:
        resp = await demo_client.post(
            "/api/v1/auth/login",
            json={"email": "demo", "password": "wrongpassword"},
        )
        assert resp.status_code == 401

    async def test_demo_user_invalid_email(self, demo_client: AsyncClient) -> None:
        resp = await demo_client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent", "password": "demo"},
        )
        assert resp.status_code == 401

    async def test_demo_user_me(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"/me failed: {resp.text}"
        data = resp.json()
        assert data["email"] == "demo"
        assert data["display_name"] == "Demo User"
        assert data["org_role"] == "viewer"

    async def test_demo_user_refresh_token(self, demo_client: AsyncClient) -> None:
        login_resp = await demo_client.post("/api/v1/auth/login", json={"email": "demo", "password": "demo"})
        assert login_resp.status_code == 200
        refresh_token = login_resp.json()["refresh_token"]

        resp = await demo_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 200, f"Refresh failed: {resp.text}"
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data


# ---------------------------------------------------------------------------
# Tests: onboarding / first-run experience
# ---------------------------------------------------------------------------


class TestDemoOnboarding:
    """Test the action-based first-run onboarding flow with a demo user."""

    _ACTION_IDS = (
        "login",
        "add_ai_model",
        "create_first_agent",
        "create_first_schema",
        "create_first_pipeline",
        "run_first_pipeline",
    )

    @pytest_asyncio.fixture(autouse=True)
    async def _ensure_demo_user(self, test_demo_user: uuid.UUID) -> None:
        pass

    async def test_onboarding_status_first_run(self, demo_client: AsyncClient) -> None:
        """A fresh org reports is_first_run=True and lists all six actions."""
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"/onboarding/status failed: {resp.text}"
        data = resp.json()
        assert data["is_first_run"] is True
        assert data["dismissed"] is False
        assert len(data["actions"]) == 6
        assert {a["id"] for a in data["actions"]} == set(self._ACTION_IDS)

    async def test_onboarding_mark_step(self, demo_client: AsyncClient) -> None:
        """Completing an action is reflected in the response."""
        token = await _demo_login(demo_client)
        resp = await demo_client.post(
            "/api/v1/onboarding/actions/add_ai_model/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Mark action failed: {resp.text}"
        data = resp.json()
        assert data["action_id"] == "add_ai_model"
        assert data["completed"] is True

    async def test_onboarding_mark_invalid_step(self, demo_client: AsyncClient) -> None:
        """Unknown action ids are rejected with 422."""
        token = await _demo_login(demo_client)
        resp = await demo_client.post(
            "/api/v1/onboarding/actions/invalid_step/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    async def test_onboarding_get_step_data(self, demo_client: AsyncClient) -> None:
        """Status lists every onboarding action with its metadata."""
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        actions = {a["id"]: a for a in resp.json()["actions"]}
        assert "add_ai_model" in actions
        assert actions["add_ai_model"]["route"] == "/settings/model-backends"

    async def test_onboarding_complete_all_steps(self, demo_client: AsyncClient) -> None:
        """Completing every action flips is_first_run to False."""
        token = await _demo_login(demo_client)
        for action_id in self._ACTION_IDS:
            resp = await demo_client.post(
                f"/api/v1/onboarding/actions/{action_id}/complete",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, f"Mark action {action_id} failed: {resp.text}"

        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        assert data["is_first_run"] is False

    async def test_onboarding_current_step_updates(self, demo_client: AsyncClient) -> None:
        """Completing one action is persisted and shown in the status response."""
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.json()["is_first_run"] is True

        await demo_client.post(
            "/api/v1/onboarding/actions/add_ai_model/complete",
            headers={"Authorization": f"Bearer {token}"},
        )

        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        assert "add_ai_model" in data["completed_actions"]
        assert data["is_first_run"] is False

    async def test_onboarding_unauthorized(self, demo_client: AsyncClient) -> None:
        """Unauthenticated requests to onboarding should be rejected."""
        resp = await demo_client.get("/api/v1/onboarding/status")
        # FastAPI's get_current_user raises 401 when no credentials provided
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: demo pipeline lifecycle
# ---------------------------------------------------------------------------


class TestDemoPipeline:
    """Test that demo pipelines can be loaded and viewed."""

    @pytest_asyncio.fixture(autouse=True)
    async def _ensure_demo_user(self, test_demo_user: uuid.UUID) -> None:
        pass

    async def test_list_pipelines_shows_demo_pipeline(
        self,
        demo_client: AsyncClient,
        demo_pipeline: uuid.UUID,
    ) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"List pipelines failed: {resp.text}"
        data = resp.json()
        assert data["total"] >= 1
        pipeline_ids = {p["id"] for p in data["items"]}
        assert str(demo_pipeline) in pipeline_ids

    async def test_get_demo_pipeline_by_id(
        self,
        demo_client: AsyncClient,
        demo_pipeline: uuid.UUID,
    ) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            f"/api/v1/pipelines/{demo_pipeline}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Get pipeline failed: {resp.text}"
        data = resp.json()
        assert data["id"] == str(demo_pipeline)
        assert data["name"] == "PRD to Requirements"
        assert "Demo pipeline" in data["description"]

    async def test_get_nonexistent_pipeline_returns_404(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        fake_id = uuid.uuid4()
        resp = await demo_client.get(
            f"/api/v1/pipelines/{fake_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    async def test_onboarding_auto_skip_when_pipeline_exists(
        self,
        demo_client: AsyncClient,
        demo_pipeline: uuid.UUID,
    ) -> None:
        """An existing pipeline auto-completes the create_first_pipeline action."""
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        actions = {a["id"]: a for a in data["actions"]}
        assert actions["create_first_pipeline"]["completed"] is True
