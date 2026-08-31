"""Integration test: change_password revokes ALL token families of the account.

Regression guard for the review finding on PR #2155. ``me.change_password`` must
blacklist every refresh-token family belonging to the account, including families
minted under a DIFFERENT organisation (or a NULL org) than the caller's current
org. ``token_families`` keeps a fail-open ``rls_org_isolation`` policy, so leaving
the org context UNSET while listing families returns all of them; scoping the
transaction to the caller's current org would silently skip cross-org families and
leave stale refresh tokens live after a password change (a token-invalidation gap).

Drives the real ``change_password`` handler against a NOBYPASSRLS role (the
production ``modulo_app`` scenario) and asserts every family ends up blacklisted.

Requires a real Postgres (Testcontainers) — see conftest. The unit tests mock the
CRUD so they cannot catch the RLS-scoping regression; only a real Postgres path can.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes.me import PasswordChangeRequest, change_password
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.passwords import hash_password

pytestmark = pytest.mark.integration

_STRONG_PW = "correct-horse-battery"
_NEW_PW = "new-strong-password-42"


@pytest_asyncio.fixture
async def rls_session(app_engine: AsyncEngine) -> AsyncSession:
    """Session whose connections run as a NOBYPASSRLS role, so RLS applies.

    Mirrors production where the app connects as ``modulo_app`` (NOBYPASSRLS).
    The handler establishes its own org context for the audit write; the
    token-family reads intentionally run with the org context unset.
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _create_org(db_engine: AsyncEngine, name: str, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": name, "slug": slug},
        )
    return org_id


async def _create_account(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, :pw, 'local', true)",
            ),
            {
                "id": str(account_id),
                "email": f"tf-{account_id.hex[:8]}@example.com",
                "name": "Token-family test user",
                "pw": hash_password(_STRONG_PW),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _create_family(db_engine: AsyncEngine, account_id: uuid.UUID, org_id: uuid.UUID | None) -> uuid.UUID:
    family_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO token_families (family_id, account_id, organisation_id, max_sequence, is_blacklisted) "
                "VALUES (:fid, :aid, :oid, 0, false)",
            ),
            {
                "fid": str(family_id),
                "aid": str(account_id),
                "oid": str(org_id) if org_id is not None else None,
            },
        )
    return family_id


async def _is_blacklisted(db_engine: AsyncEngine, family_id: uuid.UUID) -> bool:
    """Read blacklist state via a superuser connection (bypasses RLS)."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT is_blacklisted FROM token_families WHERE family_id = :fid"),
            {"fid": str(family_id)},
        )
        row = result.scalar_one_or_none()
    return bool(row)


async def test_change_password_blacklists_cross_org_token_families(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    """A password change must revoke EVERY token family of the account.

    The caller's current org is ``test_org``; one family is minted under a
    DIFFERENT org and one under a NULL org. All three must be blacklisted — the
    prior org-scoped behaviour left the cross-org / NULL families live, so a stale
    refresh token from another org kept working after the credential change.
    """
    other_org = await _create_org(db_engine, "Other org", f"other-{uuid.uuid4().hex[:8]}")
    account_id = await _create_account(db_engine, test_org)
    same_org_family = await _create_family(db_engine, account_id, test_org)
    other_org_family = await _create_family(db_engine, account_id, other_org)
    null_org_family = await _create_family(db_engine, account_id, None)

    principal = TenantPrincipal(
        username="token-family-test",
        organisation_id=test_org,
        account_id=account_id,
        org_role="admin",
    )

    resp = await change_password(
        req=PasswordChangeRequest(current_password=_STRONG_PW, new_password=_NEW_PW),
        current_user=principal,
        session=rls_session,
    )
    assert resp["detail"] == "Password changed successfully"

    assert await _is_blacklisted(db_engine, same_org_family) is True
    assert await _is_blacklisted(db_engine, other_org_family) is True
    assert await _is_blacklisted(db_engine, null_org_family) is True


async def test_change_password_blacklists_both_families_for_single_account(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    """Two same-org families for one account are both revoked on password change.

    Sanity check that the cross-org assertion above is not vacuously passing
    because the listing returned nothing — here both families share the caller's
    org and must still be blacklisted.
    """
    account_id = await _create_account(db_engine, test_org)
    family_a = await _create_family(db_engine, account_id, test_org)
    family_b = await _create_family(db_engine, account_id, test_org)

    principal = TenantPrincipal(
        username="token-family-test",
        organisation_id=test_org,
        account_id=account_id,
        org_role="admin",
    )

    resp = await change_password(
        req=PasswordChangeRequest(current_password=_STRONG_PW, new_password=_NEW_PW),
        current_user=principal,
        session=rls_session,
    )
    assert resp["detail"] == "Password changed successfully"

    assert await _is_blacklisted(db_engine, family_a) is True
    assert await _is_blacklisted(db_engine, family_b) is True
