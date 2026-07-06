"""Integration test for demo first-run experience.

Tests the _seed_demo_data lifespan function, demo user authentication,
onboarding flow, and demo pipeline lifecycle with a real database via testcontainers.
"""

import os
import uuid
from collections.abc import AsyncGenerator

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The onboarding route stores state at backend-root/.onboarding-state.json
# which is 4 levels up from its own file (backend/src/modulo/api/routes/onboarding.py)
_ONBOARDING_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".onboarding-state.json",
)


def _clean_onboarding_state() -> None:
    if os.path.exists(_ONBOARDING_STATE_PATH):
        os.remove(_ONBOARDING_STATE_PATH)


# ---------------------------------------------------------------------------
# Module-level autouse: clean onboarding state before every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_onboarding_before_test() -> None:
    _clean_onboarding_state()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def test_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Default organisation for demo mode tests."""
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Demo First-Run Test Org",
                "slug": f"demo-fr-{org_id.hex[:8]}",
            },
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def test_demo_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    """Create a demo user with known password for auth tests.

    Cleans up any existing 'demo' users first to avoid MultipleResultsFound
    from seed data tests that also create users with email 'demo'.
    """
    from modulo.auth.passwords import hash_password

    async with db_engine.connect() as conn:
        await conn.execute(text("DELETE FROM users WHERE email = 'demo'"))
        await conn.commit()

    user_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO users (id, organisation_id, email, display_name, "
                "password_hash, org_role, auth_provider, active) "
                "VALUES (:id, :oid, :email, :name, :hash, :role, 'local', true)",
            ),
            {
                "id": str(user_id),
                "oid": str(test_org),
                "email": "demo",
                "name": "Demo User",
                "hash": hash_password("demo"),
                "role": "viewer",
            },
        )
    return user_id


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
                "INSERT INTO pipelines (id, organisation_id, name, description, created_by, "
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
        fernet_key="a" * 32,
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
    from modulo.auth.passwords import verify_password
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
    )

    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, '{}'::json)",
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
            text("SELECT email, display_name, org_role, password_hash FROM users WHERE email = 'demo'"),
        )
        row = result.one_or_none()
        assert row is not None, "Demo user was not created by _seed_demo_data"
        _email, display_name, org_role, password_hash = row
        assert display_name == "Demo User"
        assert org_role == "viewer"
        assert password_hash is not None
        assert verify_password("demo", password_hash)

    # Clean up the seed-created user to avoid cross-test contamination
    async with db_engine.connect() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE email = 'demo' AND organisation_id = :oid"),
            {"oid": str(org_id)},
        )
        await conn.commit()

    deps._engine = None
    deps._session_factory = None


async def test_seed_demo_data_no_org_no_crash(db_engine: AsyncEngine, db_url: str) -> None:
    """_seed_demo_data should not crash when no org exists — just log and return."""
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
    )

    import modulo.api.dependencies as deps
    deps._engine = None
    deps._session_factory = None

    await _seed_demo_data(settings)

    deps._engine = None
    deps._session_factory = None


async def test_seed_demo_data_skipped_when_disabled(db_engine: AsyncEngine, db_url: str) -> None:
    """_seed_demo_data should be a no-op when MODULO_DEMO_MODE is False."""
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_demo_mode=False,
        modulo_csrf_enabled=False,
    )

    # Use a unique email for this test to avoid cross-test contamination

    # First, clean the demo user from previous test
    async with db_engine.connect() as conn:
        await conn.execute(text("DELETE FROM users WHERE email = 'demo'"))
        await conn.commit()

    import modulo.api.dependencies as deps
    deps._engine = None
    deps._session_factory = None

    await _seed_demo_data(settings)

    deps._engine = None
    deps._session_factory = None

    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT email FROM users WHERE email = 'demo'"),
        )
        assert result.one_or_none() is None, "No demo user should exist when MODULO_DEMO_MODE is disabled"


async def test_seed_demo_data_idempotent(db_engine: AsyncEngine, db_url: str) -> None:
    """Calling _seed_demo_data twice should not create duplicate users."""
    from modulo.api.main import _seed_demo_data
    from modulo.settings import Settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_demo_mode=True,
        modulo_csrf_enabled=False,
    )

    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, '{}'::json)",
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
            text("SELECT COUNT(*) FROM users WHERE email = 'demo'"),
        )
        count = result.scalar()
        assert count == 1, f"Expected 1 demo user, got {count}"

    # Clean up the seed-created user to avoid cross-test contamination
    async with db_engine.connect() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE email = 'demo' AND organisation_id = :oid"),
            {"oid": str(org_id)},
        )
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
        assert isinstance(token, str) and len(token) > 20

    async def test_demo_user_invalid_password(self, demo_client: AsyncClient) -> None:
        resp = await demo_client.post(
            "/api/v1/auth/login", json={"email": "demo", "password": "wrongpassword"},
        )
        assert resp.status_code == 401

    async def test_demo_user_invalid_email(self, demo_client: AsyncClient) -> None:
        resp = await demo_client.post(
            "/api/v1/auth/login", json={"email": "nonexistent", "password": "demo"},
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
    """Test the onboarding/first-run flow with a demo user."""

    @pytest_asyncio.fixture(autouse=True)
    async def _ensure_demo_user(self, test_demo_user: uuid.UUID) -> None:
        pass

    async def test_onboarding_status_first_run(self, demo_client: AsyncClient) -> None:
        """Onboarding should report is_first_run=True when no pipelines exist."""
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"/onboarding/status failed: {resp.text}"
        data = resp.json()
        assert data["is_first_run"] is True
        assert data["completed_steps"] == []
        assert data["current_step"] == 1
        assert data["total_steps"] == 4

    async def test_onboarding_mark_step(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.post(
            "/api/v1/onboarding/step",
            json={"step_id": "connect_tools"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Mark step failed: {resp.text}"
        data = resp.json()
        assert data["step_id"] == "connect_tools"
        assert data["completed"] is True
        assert "connect_tools" in data["completed_steps"]

    async def test_onboarding_mark_invalid_step(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.post(
            "/api/v1/onboarding/step",
            json={"step_id": "invalid_step"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    async def test_onboarding_get_step_data(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/step/connect_tools",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Get step data failed: {resp.text}"
        data = resp.json()
        assert data["step_id"] == "connect_tools"
        assert "data" in data
        assert "connectors" in data["data"]

    async def test_onboarding_get_invalid_step_data(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/step/invalid_step",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    async def test_onboarding_complete_all_steps(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)
        for step_id in ["connect_tools", "select_template", "configure_agent", "run_demo"]:
            resp = await demo_client.post(
                "/api/v1/onboarding/step",
                json={"step_id": step_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, f"Mark step {step_id} failed: {resp.text}"

        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        assert data["is_first_run"] is False

    async def test_onboarding_current_step_updates(self, demo_client: AsyncClient) -> None:
        token = await _demo_login(demo_client)

        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.json()["current_step"] == 1

        await demo_client.post(
            "/api/v1/onboarding/step",
            json={"step_id": "connect_tools"},
            headers={"Authorization": f"Bearer {token}"},
        )

        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.json()["current_step"] == 2

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
        token = await _demo_login(demo_client)
        resp = await demo_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_first_run"] is False
