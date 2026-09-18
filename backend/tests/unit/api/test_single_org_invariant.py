"""Tests for the single-org invariant (FAR-929 / ADR 005).

When MODULO_MULTI_ORG_ENABLED is False (default):
  - POST /api/v1/admin/orgs returns 403
  - POST /api/v1/auth/login ignores org_slug (uses memberships[0])
  - GET /api/v1/auth/login-context always reports multi_org=false
  - GET /api/v1/auth/org-login/{slug} returns 404 for any slug
  - seed_demo skips the demo org creation

When MODULO_MULTI_ORG_ENABLED is True:
  - All existing behaviour is preserved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="admin@test",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="admin",
    is_system_admin=True,
)


def _make_settings(**overrides: object) -> Settings:
    kwargs: dict = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_admin_password": "testpass",
        "modulo_auth_rate_limit_enabled": False,
        "redis_url": "",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock()
    session.begin.return_value.__aenter__.return_value = session
    session.begin.return_value.__aexit__.return_value = None
    session.in_transaction = MagicMock(return_value=True)
    session.execute.return_value = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = None
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.add = MagicMock()
    session.flush.return_value = None
    return session


def _make_plan() -> MagicMock:
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    return plan


# ---------------------------------------------------------------------------
# admin_create_org — disabled (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_create_org_blocked_when_multi_org_disabled() -> None:
    """POST /api/v1/admin/orgs returns 403 when multi-org is disabled."""
    settings = _make_settings()
    assert settings.modulo_multi_org_enabled is False

    mock_session = _make_session()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    app.dependency_overrides[get_plan_context] = _make_plan
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/orgs",
                json={"name": "New Org", "slug": "new-org"},
            )
        assert resp.status_code == 403
        assert "not enabled" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_create_org_allowed_when_multi_org_enabled() -> None:
    """POST /api/v1/admin/orgs proceeds when multi-org is enabled."""
    settings = _make_settings(modulo_multi_org_enabled=True)
    assert settings.modulo_multi_org_enabled is True

    mock_session = _make_session()
    # Mock the slug uniqueness check to return None (no conflict)
    mock_session.execute.return_value.scalar_one_or_none.return_value = None

    # Mock create_organisation to return a fake org
    fake_org = SimpleNamespace(
        id=uuid.uuid4(),
        name="New Org",
        slug="new-org",
        status="active",
        created_at=_NOW,
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    app.dependency_overrides[get_plan_context] = _make_plan
    try:
        with patch("modulo.api.routes.admin_orgs.create_organisation", new_callable=AsyncMock, return_value=fake_org):
            with patch(
                "modulo.api.routes.admin_orgs.seed_cost_components_for_org",
                new_callable=AsyncMock,
            ):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.post(
                        "/api/v1/admin/orgs",
                        json={"name": "New Org", "slug": "new-org"},
                    )
            # Should NOT be 403 — the gate is lifted
            assert resp.status_code != 403
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# login — org_slug rejected when multi-org disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_org_slug_ignored_when_multi_org_disabled() -> None:
    """When multi-org is disabled, org_slug is passed as None to _run_login_transaction."""
    settings = _make_settings()
    assert settings.modulo_multi_org_enabled is False

    mock_session = _make_session()

    mock_account = MagicMock()
    mock_account.id = _USER_ID
    mock_account.email = "user@test.com"
    mock_account.active = True
    mock_account.is_system_admin = False
    mock_account.is_break_glass = False
    mock_account.password_hash = "$2b$12$test"
    mock_account.auth_provider = "local"
    mock_account.must_change_password = False
    mock_account.break_glass_expires_at = None
    mock_account.break_glass_deactivated_at = None

    mock_membership = MagicMock()
    mock_membership.organisation_id = _ORG_ID
    mock_membership.role = "admin"
    mock_membership.deactivated_at = None

    mock_family = MagicMock()
    mock_family.family_id = str(uuid.uuid4())

    call_count = 0

    async def mock_execute(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one_or_none.return_value = mock_account
        elif call_count == 3:
            # list_memberships_for_account (call 2 is update_last_login)
            result.scalars.return_value.all.return_value = [mock_membership]
        else:
            result.scalar_one_or_none.return_value = None
        return result

    mock_session.execute = mock_execute

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    app.dependency_overrides[get_plan_context] = _make_plan
    try:
        with (
            patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
            patch("modulo.api.routes.auth.create_family", new_callable=AsyncMock, return_value=mock_family),
            patch("modulo.api.routes.auth.create_access_token", return_value="fake-token"),
            patch("modulo.api.routes.auth.create_refresh_token", return_value="fake-refresh"),
            patch("modulo.api.routes.auth._run_login_transaction", new_callable=AsyncMock) as mock_login_txn,
        ):
            # Pre-populate the mock return value for _run_login_transaction
            mock_login_ctx = MagicMock()
            mock_login_ctx.account = mock_account
            mock_login_ctx.org_id = _ORG_ID
            mock_login_ctx.org_role = "admin"
            mock_login_ctx.memberships = [mock_membership]
            mock_login_ctx.family = mock_family
            mock_login_txn.return_value = mock_login_ctx

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    "/api/v1/auth/login",
                    json={
                        "email": "user@test.com",
                        "password": "testpass",
                        "org_slug": "some-other-org",
                    },
                )
            # Verify _run_login_transaction was called with org_slug=None
            # (multi-org disabled → org_slug is stripped regardless of what the client sends)
            mock_login_txn.assert_called_once()
            call_kwargs = mock_login_txn.call_args
            assert call_kwargs.kwargs.get("org_slug") is None or (
                len(call_kwargs.args) > 0 and call_kwargs[1].get("org_slug") is None
            )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# login-context — always reports single-org when disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_context_single_org_when_disabled() -> None:
    """GET /api/v1/auth/login-context returns multi_org=false when disabled."""
    settings = _make_settings()
    mock_session = _make_session()

    # Return 2 login-active orgs (would normally be multi_org=true)
    mock_org1 = SimpleNamespace(slug="org-a", name="Org A")
    mock_org2 = SimpleNamespace(slug="org-b", name="Org B")
    mock_session.execute.return_value.scalars.return_value.all.return_value = [mock_org1, mock_org2]

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_plan_context] = _make_plan
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_context_single_org_enabled_preserves_existing() -> None:
    """GET /api/v1/auth/login-context preserves existing multi-org logic when enabled."""
    settings = _make_settings(modulo_multi_org_enabled=True)
    mock_session = _make_session()

    # Return 2 login-active orgs → multi_org=true
    mock_org1 = SimpleNamespace(slug="org-a", name="Org A")
    mock_org2 = SimpleNamespace(slug="org-b", name="Org B")
    mock_session.execute.return_value.scalars.return_value.all.return_value = [mock_org1, mock_org2]

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_plan_context] = _make_plan
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is True
        assert data["org"] is None
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# org-login/{slug} — 404 when multi-org disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_login_slug_404_when_multi_org_disabled() -> None:
    """GET /api/v1/auth/org-login/{slug} returns 404 when multi-org is disabled."""
    settings = _make_settings()
    mock_session = _make_session()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_plan_context] = _make_plan
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/auth/org-login/my-org")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_org_login_slug_allowed_when_multi_org_enabled() -> None:
    """GET /api/v1/auth/org-login/{slug} works when multi-org is enabled."""
    settings = _make_settings(modulo_multi_org_enabled=True)
    mock_session = _make_session()

    mock_org = SimpleNamespace(
        slug="my-org",
        name="My Org",
        id=uuid.uuid4(),
    )

    async def mock_execute(*args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_org
        return result

    mock_session.execute = mock_execute

    # Mock list_enabled_oidc_providers
    with (
        patch(
            "modulo.api.routes.org_login.list_enabled_oidc_providers",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "modulo.api.routes.org_login.is_saml_available",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_db_session] = lambda: mock_session
        app.dependency_overrides[get_plan_context] = _make_plan
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/v1/auth/org-login/my-org")
            assert resp.status_code == 200
            data = resp.json()
            assert data["org"]["slug"] == "my-org"
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# seed_demo — skipped when multi-org disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_demo_skipped_when_multi_org_disabled() -> None:
    """seed_demo returns None when multi-org is disabled (demo creates a second org)."""
    settings = _make_settings(
        modulo_demo_enabled=True,
        modulo_demo_user="demo@test.com",
        modulo_demo_password="secret123",
    )
    assert settings.modulo_multi_org_enabled is False

    mock_session = AsyncMock()

    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with patch("modulo.db.seed_demo.get_settings", return_value=settings):
            from modulo.db.seed_demo import seed_demo

            result = await seed_demo(mock_session)
        assert result is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_seed_demo_proceeds_when_multi_org_enabled() -> None:
    """seed_demo proceeds when multi-org is enabled."""
    settings = _make_settings(
        modulo_multi_org_enabled=True,
        modulo_demo_enabled=True,
        modulo_demo_user="demo@test.com",
        modulo_demo_password="secret123",
    )
    assert settings.modulo_multi_org_enabled is True

    mock_session = AsyncMock()

    fake_org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="demo",
        deleted_at=None,
    )
    fake_account = SimpleNamespace(
        id=uuid.uuid4(),
        email="demo@test.com",
        auth_provider="local",
        password_hash="$2b$12$test",
    )

    # Mock session.execute to return a result with .scalars().all() = []
    # (no stray memberships) for the stray-org query
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=execute_result)

    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with (
            patch("modulo.db.seed_demo.get_settings", return_value=settings),
            patch(
                "modulo.db.seed_demo._get_or_create_demo_org",
                new_callable=AsyncMock,
                return_value=fake_org,
            ),
            patch(
                "modulo.db.seed_demo._seed_demo_account",
                new_callable=AsyncMock,
                return_value=fake_account,
            ),
            patch(
                "modulo.db.seed_demo._seed_demo_membership",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.db.seed_demo.set_rls_org",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.db.seed_demo.set_rls_execution_context",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.db.seed_demo._seed_demo_schemas",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.db.seed_demo._seed_demo_pipeline_and_runs",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.db.seed_demo._seed_demo_triggers",
                new_callable=AsyncMock,
            ),
        ):
            from modulo.db.seed_demo import seed_demo

            result = await seed_demo(mock_session)
        assert result is not None
        assert "demo" in result
    finally:
        app.dependency_overrides.clear()
