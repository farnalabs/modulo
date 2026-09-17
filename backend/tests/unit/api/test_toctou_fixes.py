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


def _principal(*, is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
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
        """SET NX succeeds when the lock key does not exist.

        The acquire returns a unique owner token (not a bare sentinel) so
        release can discriminate owners.
        """
        from modulo.api.routes.admin_rotation import _acquire_rotation_lock

        mock_r = AsyncMock()
        mock_r.set = AsyncMock(return_value=True)  # SET NX succeeded
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        result = await _acquire_rotation_lock(settings)
        assert isinstance(result, str) and result

        # The stored value must be the returned owner token, with NX + TTL.
        stored_value = mock_r.set.call_args[0][1]
        assert stored_value == result
        assert mock_r.set.call_args[1]["nx"] is True
        assert mock_r.set.call_args[1]["ex"] == 1800

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_acquire_lock_fails_when_held(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """SET NX returns None when the lock key already exists."""
        from modulo.api.routes.admin_rotation import _acquire_rotation_lock

        mock_r = AsyncMock()
        mock_r.set = AsyncMock(return_value=None)  # SET NX failed (key exists)
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        result = await _acquire_rotation_lock(settings)
        assert result is None

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_acquire_lock_redis_error_returns_503(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """A Redis blip during acquire must surface as 503, not an unhandled 500."""
        from fastapi import HTTPException

        from modulo.api.routes.admin_rotation import _acquire_rotation_lock

        mock_r = AsyncMock()
        mock_r.set = AsyncMock(side_effect=ConnectionError("redis down"))
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        with pytest.raises(HTTPException) as exc_info:
            await _acquire_rotation_lock(settings)
        assert exc_info.value.status_code == 503

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
        await _release_rotation_lock(settings, "owner-token-abc")

        mock_r.eval.assert_called_once()
        call_args = mock_r.eval.call_args
        assert call_args[0][1] == 1  # 1 key argument
        # The owner token (not a shared sentinel) is passed as ARGV[1].
        assert call_args[0][3] == "owner-token-abc"

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_release_lock_noop_when_no_redis(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Release is a no-op when Redis is not configured."""
        from modulo.api.routes.admin_rotation import _release_rotation_lock

        settings = _make_settings(redis_url="")
        await _release_rotation_lock(settings, "owner-token-abc")
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


class TestRotationGuardInMemoryFallback:
    """Without Redis the in-process guard must still serialise rotations.

    Regression for the M finding on PR #662: the asyncio.Lock in ``rotate_key``
    is released before ``asyncio.create_task(_run_rotation_background)``, so
    the no-Redis fallback needs a token held until the background task
    releases it — otherwise two concurrent rotate_key calls both start.
    """

    @pytest.fixture(autouse=True)
    def _reset_in_memory_guard(self) -> Generator[None, None, None]:
        from modulo.api.routes import admin_rotation

        admin_rotation._rotation_owner = None
        yield
        admin_rotation._rotation_owner = None

    @pytest.mark.asyncio
    async def test_second_acquire_fails_while_held_without_redis(self) -> None:
        from modulo.api.routes.admin_rotation import _acquire_rotation_lock

        settings = _make_settings(redis_url="")
        first = await _acquire_rotation_lock(settings)
        assert isinstance(first, str) and first
        # While the guard is held the second caller must be refused.
        assert await _acquire_rotation_lock(settings) is None

    @pytest.mark.asyncio
    async def test_is_rotation_active_reports_in_memory_guard(self) -> None:
        from modulo.api.routes import admin_rotation
        from modulo.api.routes.admin_rotation import _is_rotation_active

        settings = _make_settings(redis_url="")
        assert await _is_rotation_active(settings) is False
        admin_rotation._rotation_owner = "held"
        assert await _is_rotation_active(settings) is True

    @pytest.mark.asyncio
    async def test_release_clears_guard_only_for_owner(self) -> None:
        from modulo.api.routes.admin_rotation import (
            _acquire_rotation_lock,
            _is_rotation_active,
            _release_rotation_lock,
        )

        settings = _make_settings(redis_url="")
        owner = await _acquire_rotation_lock(settings)
        assert owner is not None

        # A stale rotation must not clear a successor's guard.
        await _release_rotation_lock(settings, "some-other-owner")
        assert await _is_rotation_active(settings) is True

        await _release_rotation_lock(settings, owner)
        assert await _is_rotation_active(settings) is False


def test_rotate_key_conflict_writes_no_started_audit_event() -> None:
    """A request that loses the lock race must not record a 'started' audit
    event for a rotation that never runs."""
    from collections.abc import AsyncGenerator

    from modulo.api.routes import admin_rotation

    admin_rotation._rotation_owner = "held-by-another-rotation"

    def _no_redis_settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_system_database_url="postgresql+asyncpg://localhost/system",
            redis_url="",
        )

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield _make_mock_session()

    app.dependency_overrides[get_settings] = _no_redis_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: _principal(is_system_admin=True)
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        tc = TestClient(app)
        with (
            patch("modulo.api.routes.admin_rotation.append_audit_event", new=AsyncMock()) as mock_audit,
            patch("modulo.api.routes.admin_rotation._run_rotation_background", new=AsyncMock()) as mock_bg,
        ):
            resp = tc.post(
                "/api/v1/admin/rotation/rotate-key",
                json={"new_fernet_key": _VALID_32},
            )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
        mock_audit.assert_not_awaited()
        mock_bg.assert_not_called()
    finally:
        app.dependency_overrides.clear()
        admin_rotation._rotation_owner = None


class _FakeRedisStore:
    """In-memory emulation of the SET NX + Lua compare-and-delete semantics."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def set_raw(self, key: str, value: str) -> None:
        """Simulate a successor taking over (TTL already expired)."""
        self.store[key] = value

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def aclose(self) -> None:
        return None


def _no_redis_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_system_database_url="postgresql+asyncpg://localhost/system",
        redis_url="",
    )


class TestRotationLockOwnership:
    """Major-1 fix: lock ownership via per-acquisition unique tokens.

    A rotation that outlived its TTL (lock taken over by a successor) must
    NOT be able to delete the successor's lock.
    """

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_stale_owner_cannot_release_successors_lock(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Rotation A's stale release must not delete rotation B's lock."""
        from modulo.api.routes.admin_rotation import (
            _ROTATION_LOCK_KEY,
            _acquire_rotation_lock,
            _release_rotation_lock,
        )

        fake_store = _FakeRedisStore()
        mock_r = AsyncMock()
        mock_r.set = AsyncMock(side_effect=fake_store.set)
        mock_r.eval = AsyncMock(side_effect=fake_store.eval)
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        token_a = await _acquire_rotation_lock(settings)
        assert token_a is not None
        assert await fake_store.get(_ROTATION_LOCK_KEY) == token_a

        # Rotation A exceeds the TTL: the key expires and rotation B
        # re-acquires the lock with its own fresh token.
        token_b = uuid.uuid4().hex
        await fake_store.set_raw(_ROTATION_LOCK_KEY, token_b)

        # Stale owner A releases — must NOT delete B's lock.
        await _release_rotation_lock(settings, token_a)
        assert await fake_store.get(_ROTATION_LOCK_KEY) == token_b, "stale owner deleted the successor's lock"

        # The real owner B releases its own lock — succeeds.
        await _release_rotation_lock(settings, token_b)
        assert _ROTATION_LOCK_KEY not in fake_store.store

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_tokens_are_unique_per_acquisition(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Successive acquisitions must produce distinct ownership tokens."""
        from modulo.api.routes.admin_rotation import (
            _acquire_rotation_lock,
            _release_rotation_lock,
        )

        fake_store = _FakeRedisStore()
        mock_r = AsyncMock()
        mock_r.set = AsyncMock(side_effect=fake_store.set)
        mock_r.eval = AsyncMock(side_effect=fake_store.eval)
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        settings = _make_settings()
        token_1 = await _acquire_rotation_lock(settings)
        assert token_1 is not None
        await _release_rotation_lock(settings, token_1)

        token_2 = await _acquire_rotation_lock(settings)
        assert token_2 is not None
        assert token_2 != token_1

    @pytest.mark.asyncio
    @patch("modulo.api.routes.admin_rotation.aioredis.Redis")
    async def test_background_task_releases_with_acquired_token(
        self,
        mock_redis_cls: MagicMock,
    ) -> None:
        """Wiring proof: rotate_key's token reaches the background task and is
        used for release — the lock key is gone after the background completes."""
        from modulo.api.routes import admin_rotation
        from modulo.api.routes.admin_rotation import (
            _ROTATION_LOCK_KEY,
            _acquire_rotation_lock,
        )

        fake_store = _FakeRedisStore()
        mock_r = AsyncMock()
        mock_r.set = AsyncMock(side_effect=fake_store.set)
        mock_r.eval = AsyncMock(side_effect=fake_store.eval)
        mock_r.aclose = AsyncMock()
        mock_redis_cls.from_url.return_value = mock_r

        token = await _acquire_rotation_lock(_make_settings())
        assert token is not None

        fake_session = AsyncMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        fake_session.begin = MagicMock(return_value=begin_cm)

        rotation_result = MagicMock()
        rotation_result.tables_processed = ["secrets"]
        rotation_result.total_rows_reencrypted = 1
        rotation_result.details = {}
        settings = _make_settings()

        with (
            patch.object(
                admin_rotation,
                "_make_system_session_factory",
                side_effect=lambda: lambda: fake_session,
            ),
            patch.object(
                admin_rotation,
                "rotate_all_encrypted_data",
                new=AsyncMock(return_value=rotation_result),
            ),
            patch.object(admin_rotation, "append_audit_event", new=AsyncMock()),
            patch.object(admin_rotation, "get_settings", return_value=settings),
        ):
            await admin_rotation._run_rotation_background(
                new_key=_VALID_32,
                old_key="",
                org_id=_ORG_ID,
                actor_user_id=_USER_ID,
                lock_owner=token,
            )

        assert _ROTATION_LOCK_KEY not in fake_store.store, "background task did not release the lock with its own token"


class TestNoRedisInProcessGuard:
    """Major-2 fix: without Redis, a module-level in-process guard set BEFORE
    create_task and cleared in the background task's finally must stop a
    second concurrent rotate_key from starting a parallel rotation."""

    def _install_no_redis_settings(self, test_client: TestClient) -> None:
        test_client.app.dependency_overrides[get_settings] = _no_redis_settings
        # rotate_key requires system.config.manage → is_system_admin.
        test_client.app.dependency_overrides[get_current_user] = lambda: _principal(is_system_admin=True)

    def test_second_concurrent_rotate_key_without_redis_409(self, client: TestClient) -> None:
        from modulo.api.routes import admin_rotation

        self._install_no_redis_settings(client)

        try:
            with (
                patch("modulo.api.routes.admin_rotation.append_audit_event", new=AsyncMock()),
                patch(
                    "modulo.api.routes.admin_rotation._run_rotation_background",
                    new=AsyncMock(),
                ),
            ):
                resp1 = client.post("/api/v1/admin/rotation/rotate-key", json={"new_fernet_key": _VALID_32})
                assert resp1.status_code == 202, resp1.text
                # The guard is set synchronously, before the background task.
                assert admin_rotation._rotation_owner is not None

                resp2 = client.post("/api/v1/admin/rotation/rotate-key", json={"new_fernet_key": _VALID_32})
                assert resp2.status_code == 409, (
                    f"second concurrent rotate must be rejected, got {resp2.status_code}: {resp2.text}"
                )
                admin_rotation._run_rotation_background.assert_called_once()

                # The status endpoint consults the same guard.
                status_resp = client.get("/api/v1/admin/rotation/status")
                assert status_resp.status_code == 200, status_resp.text
                assert status_resp.json()["rotation_in_progress"] is True
        finally:
            admin_rotation._rotation_owner = None

    @pytest.mark.asyncio
    async def test_guard_cleared_after_background_rotation_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard's scope covers the ENTIRE background rotation: it is only
        cleared by _run_rotation_background's finally, alongside the lock
        release."""
        from modulo.api.routes import admin_rotation

        monkeypatch.setattr(admin_rotation, "_rotation_owner", None)
        token = await admin_rotation._acquire_rotation_lock(_no_redis_settings())
        assert token is not None
        assert admin_rotation._rotation_owner is not None

        fake_session = AsyncMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        fake_session.begin = MagicMock(return_value=begin_cm)

        rotation_result = MagicMock()
        rotation_result.tables_processed = ["secrets"]
        rotation_result.total_rows_reencrypted = 1
        rotation_result.details = {}

        try:
            with (
                patch.object(
                    admin_rotation,
                    "_make_system_session_factory",
                    side_effect=lambda: lambda: fake_session,
                ),
                patch.object(
                    admin_rotation,
                    "rotate_all_encrypted_data",
                    new=AsyncMock(return_value=rotation_result),
                ),
                patch.object(admin_rotation, "append_audit_event", new=AsyncMock()),
                patch.object(admin_rotation, "get_settings", return_value=_no_redis_settings()),
            ):
                assert admin_rotation._rotation_owner is not None
                await admin_rotation._run_rotation_background(
                    new_key=_VALID_32,
                    old_key="",
                    org_id=_ORG_ID,
                    actor_user_id=_USER_ID,
                    lock_owner=token,
                )
        finally:
            admin_rotation._rotation_owner = None

        assert admin_rotation._rotation_owner is None, "guard must be cleared by the background task's finally"


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
