"""Tests for TOCTOU / concurrency fixes (FAR-895).

Covers:
- #326: admin_create_user IntegrityError → 409 (not 503) on concurrent duplicate
- #132: rotation guard rejects concurrent start, releases on failure
- #328: create_pipeline_from_template reads primitive inside transaction
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings(**overrides: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url=overrides.get("redis_url", "redis://localhost:6379/0"),
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.info = {}
    session.in_transaction = MagicMock(return_value=True)
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    session.add = MagicMock()
    session.execute = AsyncMock()
    return session


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: _principal()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# #326: admin_create_user IntegrityError → 409
# ---------------------------------------------------------------------------


class TestCreateUserIntegrityErrorMapping:
    """When a concurrent duplicate-email race fires IntegrityError,
    the endpoint must return 409 Conflict, not fall through to 503."""

    @patch("modulo.api.routes.admin._existing_account_or_conflict", new_callable=AsyncMock, return_value=None)
    @patch("modulo.api.routes.admin._create_or_adopt_account", new_callable=AsyncMock)
    @patch("modulo.api.routes.admin.hash_password", return_value="hashed")
    def test_integrity_error_returns_409(
        self,
        mock_hash: MagicMock,
        mock_create: AsyncMock,
        mock_existing: AsyncMock,
        client: TestClient,
    ) -> None:
        """Simulate a race: _existing_account_or_conflict sees no conflict,
        but _create_or_adopt_account hits a DB unique constraint."""
        mock_create.side_effect = IntegrityError("duplicate key", {}, Exception("unique violation"))

        resp = client.post(
            "/api/v1/admin/users",
            json={
                "email": "dup@example.com",
                "display_name": "Dup User",
                "password": "SecureP@ss123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"

    @patch("modulo.api.routes.admin._existing_account_or_conflict", new_callable=AsyncMock, return_value=None)
    @patch("modulo.api.routes.admin._create_or_adopt_account", new_callable=AsyncMock)
    @patch("modulo.api.routes.admin.hash_password", return_value="hashed")
    def test_sqlalchemy_error_still_returns_503(
        self,
        mock_hash: MagicMock,
        mock_create: AsyncMock,
        mock_existing: AsyncMock,
        client: TestClient,
    ) -> None:
        """A non-IntegrityError SQLAlchemyError should still return 503."""
        mock_create.side_effect = SQLAlchemyError("connection lost")

        resp = client.post(
            "/api/v1/admin/users",
            json={
                "email": "dbfail@example.com",
                "display_name": "DB Fail User",
                "password": "SecureP@ss123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# #132: rotation guard — concurrent start rejected, release on failure
# ---------------------------------------------------------------------------


class TestRotationGuardRedisLock:
    """The Redis-based rotation lock must reject concurrent starts and
    release on failure."""

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_acquire_lock_succeeds_when_free(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """SET NX succeeds when the lock key does not exist."""
        from modulo.api.routes.admin_rotation import _acquire_rotation_lock

        mock_r = AsyncMock()
        mock_r.set = AsyncMock(return_value=True)  # SET NX succeeded
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        result = await _acquire_rotation_lock(settings)
        assert result is True

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_acquire_lock_fails_when_held(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """SET NX returns False when the lock key already exists."""
        from modulo.api.routes.admin_rotation import _acquire_rotation_lock

        mock_r = AsyncMock()
        mock_r.set = AsyncMock(return_value=None)  # SET NX failed (key exists)
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        result = await _acquire_rotation_lock(settings)
        assert result is False

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_release_lock_calls_lua_script(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Release uses a Lua script to atomically delete only if owned."""
        from modulo.api.routes.admin_rotation import _release_rotation_lock

        mock_r = AsyncMock()
        mock_r.eval = AsyncMock(return_value=1)  # delete succeeded
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        await _release_rotation_lock(settings)

        mock_r.eval.assert_called_once()
        call_args = mock_r.eval.call_args
        assert call_args[0][1] == 1  # 1 key argument

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_release_lock_noop_when_no_redis(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Release is a no-op when Redis is not configured."""
        from modulo.api.routes.admin_rotation import _release_rotation_lock

        settings = _make_settings(redis_url="")
        await _release_rotation_lock(settings)
        mock_redis_cls.from_url.assert_not_called()

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_is_rotation_active_checks_redis(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Status check reads from Redis."""
        from modulo.api.routes.admin_rotation import _is_rotation_active

        mock_r = AsyncMock()
        mock_r.exists = AsyncMock(return_value=1)  # lock exists
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        result = await _is_rotation_active(settings)
        assert result is True


# ---------------------------------------------------------------------------
# #328: create_pipeline_from_template reads primitive inside transaction
# ---------------------------------------------------------------------------


class TestTemplatePipelineCreateReadInsideTxn:
    """The primitive read in create_pipeline_from_template must happen
    inside the same transaction as the pipeline create."""

    def _make_primitive(self, *, pid: uuid.UUID | None = None) -> MagicMock:
        p = MagicMock()
        p.id = pid or uuid.uuid4()
        p.organisation_id = _ORG_ID
        p.primitive_type = "pipeline_template"
        p.name = "Test Template"
        p.slug = "test-template"
        p.description = "A test template"
        p.content_json = {
            "agents": [{"name": "Agent A"}],
            "graph_nodes": [{"id": "node1", "node_type": "agent", "position": {"x": 0, "y": 0}}],
            "edges": [],
        }
        return p

    @patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock)
    @patch("modulo.api.routes.library.create_pipeline", new_callable=AsyncMock)
    @patch("modulo.api.routes.library._set_rls_context", new_callable=AsyncMock)
    def test_missing_primitive_returns_404_not_creates_pipeline(
        self,
        mock_rls: AsyncMock,
        mock_create_pipeline: AsyncMock,
        mock_get_primitive: AsyncMock,
    ) -> None:
        """When get_primitive returns None inside the transaction,
        the endpoint returns 404 without calling create_pipeline."""
        from fastapi.testclient import TestClient

        from modulo.api.main import app as _app

        mock_get_primitive.return_value = None  # primitive deleted concurrently

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.info = {}

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        _app.dependency_overrides[get_settings] = _make_settings
        _app.dependency_overrides[get_db_session] = override_session
        _app.dependency_overrides[_get_engine] = lambda: MagicMock()
        _app.dependency_overrides[get_current_user] = lambda: _principal()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        _app.dependency_overrides[get_plan_context] = lambda: mock_plan

        try:
            tc = TestClient(_app)
            resp = tc.post(
                f"/api/v1/libraries/{uuid.uuid4()}/create-pipeline",
                json={},
            )
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
            mock_create_pipeline.assert_not_called()
        finally:
            _app.dependency_overrides.clear()

    @patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock)
    @patch("modulo.api.routes.library.create_pipeline", new_callable=AsyncMock)
    @patch("modulo.api.routes.library._set_rls_context", new_callable=AsyncMock)
    def test_wrong_primitive_type_returns_400(
        self,
        mock_rls: AsyncMock,
        mock_create_pipeline: AsyncMock,
        mock_get_primitive: AsyncMock,
    ) -> None:
        """When the primitive is not a pipeline_template, 400 is returned."""
        from fastapi.testclient import TestClient

        from modulo.api.main import app as _app

        prim = self._make_primitive()
        prim.primitive_type = "workflow"  # wrong type
        mock_get_primitive.return_value = prim

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.info = {}

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        _app.dependency_overrides[get_settings] = _make_settings
        _app.dependency_overrides[get_db_session] = override_session
        _app.dependency_overrides[_get_engine] = lambda: MagicMock()
        _app.dependency_overrides[get_current_user] = lambda: _principal()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        _app.dependency_overrides[get_plan_context] = lambda: mock_plan

        try:
            tc = TestClient(_app)
            resp = tc.post(
                f"/api/v1/libraries/{prim.id}/create-pipeline",
                json={},
            )
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
            mock_create_pipeline.assert_not_called()
        finally:
            _app.dependency_overrides.clear()
