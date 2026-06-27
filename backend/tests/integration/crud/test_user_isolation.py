"""Cross-user data isolation integration tests.

Proves that users in different organisations cannot see each other's data,
and that user queries respect RLS boundaries.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.passwords import hash_password, password_entropy_bits, validate_password_strength
from modulo.db.crud.user import (
    get_user_by_email,
    get_user_by_id_org,
    list_users_for_org,
)

pytestmark = pytest.mark.integration


async def _create_org(db_engine: AsyncEngine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) "
                    "VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {
                    "id": str(org_id),
                    "name": f"Org {slug}",
                    "slug": slug,
                },
            )
    return org_id


async def _create_user_in_org(
    db_engine: AsyncEngine, org_id: uuid.UUID, email: str, role: str = "runner"
) -> uuid.UUID:
    pw_hash = hash_password("CorrectHorseBattery99!")
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO users (id, organisation_id, email, display_name, "
                    "password_hash, org_role, auth_provider, active) "
                    "VALUES (:id, :org_id, :email, :name, :pw_hash, :role, 'local', true)"
                ),
                {
                    "id": str(user_id),
                    "org_id": str(org_id),
                    "email": email,
                    "name": email.split("@")[0],
                    "pw_hash": pw_hash,
                    "role": role,
                },
            )
    return user_id


# ---------------------------------------------------------------------------
# RLS-based cross-org user isolation
# ---------------------------------------------------------------------------


async def test_users_in_different_orgs_are_isolated(db_engine: AsyncEngine) -> None:
    """Users created in org A must not be visible when querying as org B."""
    org_a = await _create_org(db_engine, f"iso-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"iso-b-{uuid.uuid4().hex[:8]}")

    await _create_user_in_org(db_engine, org_a, "alice@iso-test.com")
    await _create_user_in_org(db_engine, org_b, "bob@iso-test.com")

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Query as org_a — should only see alice
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        users = await list_users_for_org(session, org_a)
        emails = [u.email for u in users]
        assert "alice@iso-test.com" in emails
        assert "bob@iso-test.com" not in emails
        assert len(users) == 1

    # Query as org_b — should only see bob
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        users = await list_users_for_org(session, org_b)
        emails = [u.email for u in users]
        assert "bob@iso-test.com" in emails
        assert "alice@iso-test.com" not in emails
        assert len(users) == 1


async def test_get_user_by_id_org_respects_rls(db_engine: AsyncEngine) -> None:
    """get_user_by_id_org must return None when the user is in a different org."""
    org_a = await _create_org(db_engine, f"rls-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"rls-b-{uuid.uuid4().hex[:8]}")
    user_a = await _create_user_in_org(db_engine, org_a, "charlie@rls-test.com")

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Query for user_a from org_b context
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        found = await get_user_by_id_org(session, user_a, org_b)
        assert found is None, "Should not find user from another org via RLS-scoped query"

    # Query from correct org should work
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        found = await get_user_by_id_org(session, user_a, org_a)
        assert found is not None
        assert found.email == "charlie@rls-test.com"


async def test_login_bypasses_rls(db_engine: AsyncEngine) -> None:
    """get_user_by_email must work across orgs (login flow needs no RLS)."""
    org_a = await _create_org(db_engine, f"login-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"login-b-{uuid.uuid4().hex[:8]}")

    await _create_user_in_org(db_engine, org_a, "dave@login-test.com")
    await _create_user_in_org(db_engine, org_b, "dave@login-test.com")

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Login should find the user regardless of org context
    async with factory() as session:
        found = await get_user_by_email(session, "dave@login-test.com")
        assert found is not None
        assert found.email == "dave@login-test.com"


# ---------------------------------------------------------------------------
# Password entropy tests
# ---------------------------------------------------------------------------


def test_password_entropy_strong() -> None:
    """A strong password must meet the entropy threshold."""
    validate_password_strength("CorrectHorseBattery99!")
    bits = password_entropy_bits("CorrectHorseBattery99!")
    assert bits >= 30


def test_password_entropy_weak_short() -> None:
    """A short password must be rejected."""
    with pytest.raises(ValueError, match="at least 8 characters"):
        validate_password_strength("Ab1!")


def test_password_entropy_weak_low_entropy() -> None:
    """A long but low-entropy password (only lowercase) must be rejected."""
    with pytest.raises(ValueError, match="too weak"):
        validate_password_strength("aaaaaaaa")


def test_password_entropy_bits_calculation() -> None:
    """Shannon entropy calculation must produce sensible values."""
    assert password_entropy_bits("") == 0.0
    assert password_entropy_bits("a") > 0.0
    # Single-char repeated should have entropy = len * log2(26)
    # For 8 chars of lowercase: 8 * log2(26) ≈ 37.6
    bits = password_entropy_bits("abcdefgh")
    assert 35 < bits < 40


# ---------------------------------------------------------------------------
# Password hashing and verification
# ---------------------------------------------------------------------------


def test_hash_and_verify() -> None:
    """hash_password + verify_password round-trip must work."""
    pw = "SuperSecret123!"
    h = hash_password(pw)
    assert h != pw
    assert h.startswith("$2b$")

    from modulo.auth.passwords import verify_password
    assert verify_password(pw, h) is True
    assert verify_password("wrong", h) is False


def test_authenticate_db_user_none() -> None:
    """authenticate_db_user must return False for None user."""
    from modulo.auth.passwords import authenticate_db_user
    assert authenticate_db_user("any", None) is False
