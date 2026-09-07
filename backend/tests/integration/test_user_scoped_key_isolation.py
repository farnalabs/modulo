"""FAR-620: two-account row isolation of user-scoped keys (testcontainers).

The guarantee behind user-scoped MCP keys is that a ``scope='user'``
credential acts ONLY as its own account. Against a real Postgres (real
``FOR UPDATE`` row locks, real JSON column serialisation):

1. **(a) preferences row isolation** - the caller's MCP-context preference
   write (the shared row-locked helper ``set_hitl_email_preference``, exactly
   what the MCP ``set_hitl_email_alerts`` tool routes through with the key's
   ``account_id``) mutates ONLY account A's preferences row; account B's row
   is byte-identical before/after.
2. **(b) the lock actually serialises** - while A's write transaction holds
   the account row, a second connection's ``FOR UPDATE NOWAIT`` on the same
   row is refused (pgcode 55P03 lock_not_available).
3. **(c) the quota is per-ACCOUNT** - the per-(account, org) user-key quota
   for account B is unaffected by account A's minted keys; and expired keys
   do not consume A's quota (the R6 predicate: an unusable key is not
   "active").

Requires Docker/testcontainers; marked ``integration`` (the unit runner
excludes the marker, so it only runs where Postgres is available).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import exc as sa_exc
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes.api_keys import _enforce_user_key_quota
from modulo.auth.api_key import create_api_key
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.account import set_hitl_email_preference
from modulo.db.models.api_key import OrgApiKey
from modulo.db.rls import set_rls_org

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group(name="user_scoped_key_isolation"),
]

_ORG_PREFIX = "USKI"


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"USKI {org_id.hex[:8]}", "slug": f"uski-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_account(engine: AsyncEngine, label: str) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(acc_id),
                "email": f"uski-{label}-{acc_id.hex[:12]}@example.com",
                "name": f"USKI {label}",
            },
        )
    return acc_id


async def _mint_user_key(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    expired: bool = False,
) -> OrgApiKey:
    """Mint a user-scope key through the real mint path (auth.api_key)."""
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    expires_at: datetime | None = None
    if expired:
        expires_at = datetime.now(UTC) - timedelta(days=1)
    async with maker() as session, session.begin():
        key, _full_key = await create_api_key(
            session,
            org_id=org_id,
            name=name,
            role="operator",
            account_id=account_id,
            expires_at=expires_at,
            scope="user",
        )
        await session.flush()
        # Detach a plain record: the session closes after this block.
        return OrgApiKey(
            id=key.id,
            organisation_id=key.organisation_id,
            account_id=key.account_id,
            name=key.name,
            role=key.role,
            scope=key.scope,
            lookup_prefix=key.lookup_prefix,
            hashed_secret=key.hashed_secret,
            expires_at=key.expires_at,
        )


def _principal(org_id: uuid.UUID, account_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username=f"uski-{account_id.hex[:8]}",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)


async def _read_preferences_text(engine: AsyncEngine, account_id: uuid.UUID) -> str:
    """Read the stored preferences blob EXACTLY as persisted (JSON ::text)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT preferences::text FROM accounts WHERE id = :id"),
                {"id": str(account_id)},
            )
        ).fetchone()
    return row[0] if row and row[0] is not None else "null"


@pytest_asyncio.fixture
async def two_accounts(db_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One org, two accounts (A the key holder, B the isolation witness)."""
    org_id = await _create_org(db_engine)
    account_a = await _create_account(db_engine, "A")
    account_b = await _create_account(db_engine, "B")
    return org_id, account_a, account_b


async def test_user_key_preference_write_isolates_accounts(
    db_engine: AsyncEngine,
    two_accounts: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """(a) The key's MCP-context preference write touches ONLY its own
    account's row - account B's stored blob is byte-identical."""
    org_id, account_a, account_b = two_accounts

    # Give B a preferences blob with sibling keys to protect.
    maker = _session_factory(db_engine)
    b_block = {"dashboard_level": "error", "hitl_email": {"default": True, "pipeline_overrides": {}}}
    async with maker() as session, session.begin():
        await set_rls_org(session, org_id)
        from modulo.db.crud.account import update_account_preferences

        await update_account_preferences(session, account_b, b_block)
    before_b = await _read_preferences_text(db_engine, account_b)

    # Account A mints a user-scope key through the real mint path.
    minted = await _mint_user_key(db_engine, org_id=org_id, account_id=account_a, name="A user key")
    assert minted.scope == "user"

    # The key's MCP-context preference write: the shared helper, keyed on the
    # KEY's account (exactly what set_hitl_email_alerts does with
    # _ctx_user_id_val()).
    pipeline_override = uuid.uuid4()
    async with maker() as session, session.begin():
        await set_rls_org(session, org_id)
        merged = await set_hitl_email_preference(
            session,
            minted.account_id,
            default=True,
            pipeline_overrides={str(pipeline_override): True},
        )
    assert merged["hitl_email"]["default"] is True
    assert merged["hitl_email"]["pipeline_overrides"] == {str(pipeline_override): True}

    after_a = await _read_preferences_text(db_engine, account_a)
    after_b = await _read_preferences_text(db_engine, account_b)

    # A's row gained the hitl_email block (and only that).
    assert '"hitl_email"' in after_a
    assert f'"{pipeline_override}": true' in after_a
    # B's row is byte-identical.
    assert after_b == before_b
    assert '"hitl_email"' in after_b  # B's own block is intact


async def test_for_update_lock_serialises_the_preferences_write(
    db_engine: AsyncEngine,
    two_accounts: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """(b) While the locked helper holds account A's row inside an open
    transaction, a second connection cannot take the same lock (NOWAIT is
    refused with pgcode 55P03) - the FOR UPDATE lock is real serialisation,
    not a documentation claim."""
    org_id, account_a, _account_b = two_accounts
    maker = _session_factory(db_engine)

    session1 = maker()
    async with session1.begin():
        await set_rls_org(session1, org_id)
        await set_hitl_email_preference(session1, account_a, default=True)
        # Lock held. A second connection must now be refused.
        session2 = maker()
        try:
            async with session2.begin():
                await session2.execute(text("SELECT 1"))
                with pytest.raises(sa_exc.DBAPIError) as exc_info:
                    await session2.execute(
                        text("SELECT preferences FROM accounts WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": str(account_a)},
                    )
            assert getattr(exc_info.value.orig, "pgcode", None) == "55P03", str(exc_info.value)
        finally:
            await session2.close()


async def test_user_key_quota_is_per_account(
    db_engine: AsyncEngine,
    two_accounts: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """(c) Account A at the quota is refused (429); account B - same org,
    zero keys - is unaffected by every one of A's keys."""
    org_id, account_a, account_b = two_accounts

    for i in range(10):
        await _mint_user_key(db_engine, org_id=org_id, account_id=account_a, name=f"A key {i}")
    minted_b = await _mint_user_key(db_engine, org_id=org_id, account_id=account_b, name="B key")
    assert minted_b.scope == "user"

    maker = _session_factory(db_engine)

    # A is at the quota -> 429.
    async with maker() as session, session.begin():
        await set_rls_org(session, org_id)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await _enforce_user_key_quota(session, _principal(org_id, account_a))
    assert exc_info.value.status_code == 429

    # B has one active key -> well under quota; A's 10 keys do not count.
    async with maker() as session, session.begin():
        await set_rls_org(session, org_id)
        await _enforce_user_key_quota(session, _principal(org_id, account_b))


async def test_expired_user_keys_do_not_consume_quota(
    db_engine: AsyncEngine,
    two_accounts: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """R6: the quota counts ACTIVE keys only - an expired key is unusable and
    must not consume quota (revoked_at IS NULL AND expires_at > now)."""
    org_id, account_a, _account_b = two_accounts

    for i in range(10):
        await _mint_user_key(db_engine, org_id=org_id, account_id=account_a, name=f"A expired {i}", expired=True)

    maker = _session_factory(db_engine)
    async with maker() as session, session.begin():
        await set_rls_org(session, org_id)
        # The quota predicate itself: expired keys contribute ZERO to the
        # active count (explicit assertion - the enforcement call below is
        # the end-to-end proof, this pins the counting semantics).
        count = (
            await session.execute(
                select(func.count())
                .select_from(OrgApiKey)
                .where(
                    OrgApiKey.organisation_id == org_id,
                    OrgApiKey.account_id == account_a,
                    OrgApiKey.scope == "user",
                    OrgApiKey.revoked_at.is_(None),
                    or_(OrgApiKey.expires_at.is_(None), OrgApiKey.expires_at > datetime.now(UTC)),
                )
            )
        ).scalar_one()
        assert count == 0
        # Ten expired keys at the numeric quota bound - no HTTPException is
        # raised (an expired key must not consume quota).
        await _enforce_user_key_quota(session, _principal(org_id, account_a))
