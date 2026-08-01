"""Integration tests for org API key (``mk_``) auth against real Postgres RLS.

Regression coverage for the API-key RLS blocker: ``org_api_keys`` has RLS
enabled (migration 0005, ``_STRICT_RLS``), so a prefix lookup in a session
without an ``app.organisation_id`` context sees zero rows and every valid key
would be rejected with 401. These tests exercise
``get_current_tenant_user_or_api_key`` against a real Postgres to prove the
RLS-off prefix lookup + org-context re-validation path works end to end.
"""

import os
import uuid
from collections.abc import Generator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.api_key import _MK_PREFIX, generate_api_key
from modulo.auth.dependencies import InvalidToken, get_current_tenant_user_or_api_key
from modulo.settings import Settings

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture(autouse=True)
def _reset_engine_globals() -> Generator[None, None, None]:
    """Point the auth dependency's process-global engine at this test's DB."""
    import modulo.api.dependencies as deps

    deps._engine = None
    deps._session_factory = None
    yield
    deps._engine = None
    deps._session_factory = None


@pytest_asyncio.fixture
async def auth_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
    )


@pytest_asyncio.fixture(params=["runner", "operator"])
async def org_api_key(
    request: pytest.FixtureRequest,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> dict[str, Any]:
    """Insert a committed org API key row and return its parts."""
    role = request.param
    full_key, prefix, hashed = generate_api_key()
    key_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO org_api_keys (id, organisation_id, name, lookup_prefix, "
                "hashed_secret, role, account_id, expires_at) "
                "VALUES (:id, :oid, :name, :prefix, :hashed, :role, :uid, "
                "(CURRENT_TIMESTAMP + INTERVAL '30 days'))"
            ),
            {
                "id": str(key_id),
                "oid": str(test_org),
                "name": f"integration-{role}",
                "prefix": prefix,
                "hashed": hashed,
                "role": role,
                "uid": str(test_user),
            },
        )
    return {"id": str(key_id), "full_key": full_key, "prefix": prefix, "role": role}


async def test_valid_api_key_accepted(
    org_api_key: dict[str, Any],
    auth_settings: Settings,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A real Postgres key record validates and resolves to a TenantPrincipal."""
    principal = await get_current_tenant_user_or_api_key(
        credentials=_credentials(org_api_key["full_key"]),
        settings=auth_settings,
    )
    assert principal.organisation_id == test_org
    assert principal.account_id == test_user
    assert principal.org_role == org_api_key["role"]
    assert principal.is_system_admin is False


async def test_revoked_key_rejected(
    org_api_key: dict[str, Any],
    auth_settings: Settings,
    db_engine: AsyncEngine,
) -> None:
    """A revoked key returns 401 even though the prefix lookup still matches."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE org_api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE id = :kid"),
            {"kid": org_api_key["id"]},
        )
    with pytest.raises(InvalidToken):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(org_api_key["full_key"]),
            settings=auth_settings,
        )


async def test_wrong_secret_rejected(
    org_api_key: dict[str, Any],
    auth_settings: Settings,
) -> None:
    """A key with the right prefix but wrong secret returns 401."""
    wrong_token = f"{_MK_PREFIX}{org_api_key['prefix']}_{'z' * 32}"
    with pytest.raises(InvalidToken):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(wrong_token),
            settings=auth_settings,
        )


async def test_prefix_lookup_without_org_context_sees_no_rows(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    org_api_key: dict[str, Any],
) -> None:
    """Regression: RLS hides org_api_keys rows when no org context is set.

    A plain session without ``app.organisation_id`` cannot see the key record —
    this is exactly why the auth dependency disables RLS for the prefix lookup
    before re-validating inside the key's org context.

    testcontainers connects as a superuser, which bypasses RLS regardless of
    ``FORCE ROW LEVEL SECURITY`` (FORCE only removes the table-owner exemption,
    not the superuser/BYPASSRLS exemption). Drop to a non-superuser role with
    SELECT on ``org_api_keys`` so the policy actually filters — the same
    ``SET LOCAL ROLE`` pattern ``test_rls_isolation`` uses.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from modulo.db.models.api_key import OrgApiKey

    role = f"test_apikey_{uuid.uuid4().hex[:8]}"
    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}"'))
        await conn.execute(text(f'GRANT SELECT ON org_api_keys TO "{role}"'))
        await conn.execute(text("COMMIT"))

    try:

        engine = create_async_engine(migrated_db_url, echo=False)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await session.execute(text(f'SET LOCAL ROLE "{role}"'))
                # Explicitly clear any org context so the RLS policy filters.
                await session.execute(text("SELECT set_config('app.organisation_id', '', true)"))
                result = await session.execute(
                    select(OrgApiKey).where(OrgApiKey.lookup_prefix == org_api_key["prefix"])
                )
                assert result.scalar_one_or_none() is None
        finally:
            await engine.dispose()

    finally:
        async with db_engine.connect() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            await conn.execute(text("COMMIT"))
