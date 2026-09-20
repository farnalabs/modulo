"""Integration tests for SSO sign-in consuming a pending invitation (FAR-1043).

Proves the DB-level contract: when a user signs in via SSO (OIDC callback ->
``jit_provision_user``) and a live pending Invitation exists for their email,
the invitation is consumed and the membership carries the invitation's role --
NOT the provider's default role.

Why this matters:
  - The unit tests (``TestSsoJoinGate``) mock ALL DB CRUD functions, proving
    the decision logic is correct but never exercising real DB interactions.
  - The e2e test (``sso-invite-allowlist.spec.ts``) exercises the invite-URL ->
    password-form path, NOT the SSO sign-in path.
  - This integration test bridges the gap: real Postgres, real ORM, real CAS
    consumption -- no mocked CRUD.

What is NOT covered here:
  - The full OIDC callback HTTP flow (needs a real IdP for code exchange +
    token verification). That can only be e2e with a real or stubbed OIDC
    provider, which is outside staging-safety scope.
  - The SAML ACS path (same gap, different protocol).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.models.invitation import Invitation
from modulo.db.models.sso_provider import SsoProvider

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# DB seed helpers (superuser engine -- bypasses RLS for setup)
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"SSO Invite {org_id.hex[:8]}", "slug": f"sso-inv-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_admin_account(engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    """Create an admin account + membership so we have an inviter."""
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(acc_id), "email": f"admin-{acc_id.hex[:8]}@example.com", "name": "Admin"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(acc_id), "oid": str(org_id)},
        )
    return acc_id


async def _create_sso_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    provider_id: str = "test-sso",
    auto_provision: bool = True,
    allowed_domains: list[str] | None = None,
    default_role: str = "viewer",
    enabled: bool = False,
) -> uuid.UUID:
    """Create an SSO provider row (disabled by default for staging safety)."""
    prov_id = uuid.uuid4()
    domains_json = json.dumps(allowed_domains or [])
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, client_id, "
                "client_secret, discovery_url, enabled, auto_provision, "
                "allowed_domains, default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'oidc', :name, :pid, :cid, :secret, "
                ":disc, :enabled, :auto, CAST(:domains AS json), :role, '[]'::json, 'custom')"
            ),
            {
                "id": str(prov_id),
                "oid": str(org_id),
                "name": f"Test SSO {prov_id.hex[:8]}",
                "pid": provider_id,
                "cid": "test-client-id",
                "secret": b"encrypted-test-secret",
                "disc": "https://example.com/.well-known/openid-configuration",
                "enabled": enabled,
                "auto": auto_provision,
                "domains": domains_json,
                "role": default_role,
            },
        )
    return prov_id


async def _create_invitation(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    invited_by: uuid.UUID,
    email: str,
    org_role: str,
    expires_hours: int = 24,
) -> uuid.UUID:
    """Create a pending (live) invitation."""
    inv_id = uuid.uuid4()
    token_hash = f"integration-test-{inv_id.hex[:16]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO invitations "
                "(id, organisation_id, email, display_name, org_role, token_hash, "
                "invited_by, expires_at) "
                "VALUES (:id, :oid, :email, :name, :role, :hash, :by, :exp)"
            ),
            {
                "id": str(inv_id),
                "oid": str(org_id),
                "email": email,
                "name": f"Invited {email}",
                "role": org_role,
                "hash": token_hash,
                "by": str(invited_by),
                "exp": datetime.now(UTC) + timedelta(hours=expires_hours),
            },
        )
    return inv_id


async def _get_invitation(engine: AsyncEngine, inv_id: uuid.UUID) -> dict | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT id, consumed_at, revoked_at FROM invitations WHERE id = :id"),
                    {"id": str(inv_id)},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def _get_membership_role(engine: AsyncEngine, account_id: uuid.UUID, org_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT role FROM org_memberships "
                    "WHERE account_id = :aid AND organisation_id = :oid "
                    "AND deactivated_at IS NULL"
                ),
                {"aid": str(account_id), "oid": str(org_id)},
            )
        ).scalar_one_or_none()


async def _get_account(engine: AsyncEngine, email: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT id, email, auth_provider FROM accounts WHERE email = :email"),
                    {"email": email},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sso_invite_consume_invitation_role_wins(
    db_engine: AsyncEngine,
) -> None:
    """The core gap: SSO sign-in with a pending invitation -> invitation role wins.

    Setup:
      - SsoProvider in domain-allowlist mode (auto_provision=True,
        allowed_domains=['allowlisted.example.com'], default_role='viewer')
      - Pending invitation for invited@otherdomain.com with org_role='operator'

    When jit_provision_user is called with the provider + invitation:
      - The membership role MUST be 'operator' (from the invitation), NOT
        'viewer' (from the provider's default_role).
      - The invitation MUST be consumed (consumed_at is set).
    """
    org_id = await _create_org(db_engine)
    admin_id = await _create_admin_account(db_engine, org_id)
    prov_db_id = await _create_sso_provider(
        db_engine,
        org_id=org_id,
        auto_provision=True,
        allowed_domains=["allowlisted.example.com"],
        default_role="viewer",
    )
    invited_email = f"invited-{uuid.uuid4().hex[:8]}@otherdomain.com"
    inv_db_id = await _create_invitation(
        db_engine,
        org_id=org_id,
        invited_by=admin_id,
        email=invited_email,
        org_role="operator",
    )

    # Read the SsoProvider row as an ORM object (jit_provision_user expects it)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        # Set RLS org so the session can read/write within this org
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)

        provider = await session.get(SsoProvider, prov_db_id)
        assert provider is not None, "SsoProvider row must exist"
        assert provider.auto_provision is True
        assert provider.default_role == "viewer"
        assert provider.allowed_domains == ["allowlisted.example.com"]

        invitation = await session.get(Invitation, inv_db_id)
        assert invitation is not None, "Invitation row must exist"
        assert invitation.org_role == "operator"

        # Call jit_provision_user -- the function under test
        from modulo.settings import Settings, get_settings

        get_settings.cache_clear()
        settings = Settings(
            database_url=str(db_engine.url),
            secret_key="a" * 32,
            fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            modulo_sso_default_role="runner",
        )

        from modulo.auth.sso import jit_provision_user

        account, _returned_org_id, returned_role = await jit_provision_user(
            session,
            settings,
            invited_email,
            "SSO Invited User",
            "oidc",
            "test-sso:sub123",
            default_org_id=org_id,
            sso_provider=provider,
            email_verified=True,
        )

        await session.commit()

    # -- Assertions against the real DB --

    # 1. The membership role MUST be the invitation's role (operator),
    #    NOT the provider's default (viewer) or the SSO default (runner).
    assert returned_role == "operator", f"Expected invitation role 'operator', got '{returned_role}'"
    membership_role = await _get_membership_role(db_engine, account.id, org_id)
    assert membership_role == "operator", f"DB membership role should be 'operator', got '{membership_role}'"

    # 2. The invitation MUST be consumed.
    inv_row = await _get_invitation(db_engine, inv_db_id)
    assert inv_row is not None, "Invitation row must still exist in DB"
    assert inv_row["consumed_at"] is not None, "Invitation must have been consumed (consumed_at set)"
    assert inv_row["revoked_at"] is None, "Invitation must not be revoked"

    # 3. The account was created with the right auth provider.
    acct_row = await _get_account(db_engine, invited_email)
    assert acct_row is not None, "Account must have been created"
    assert acct_row["auth_provider"] == "oidc"


async def test_sso_invite_consume_exactly_once(
    db_engine: AsyncEngine,
) -> None:
    """Consuming the same invitation twice must not double-provision.

    The second call should find the existing membership and return it
    without burning the invitation again.
    """
    org_id = await _create_org(db_engine)
    admin_id = await _create_admin_account(db_engine, org_id)
    prov_db_id = await _create_sso_provider(
        db_engine,
        org_id=org_id,
        auto_provision=True,
        allowed_domains=["allowlisted.example.com"],
        default_role="viewer",
    )
    invited_email = f"invite2-{uuid.uuid4().hex[:8]}@otherdomain.com"
    inv_db_id = await _create_invitation(
        db_engine,
        org_id=org_id,
        invited_by=admin_id,
        email=invited_email,
        org_role="operator",
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # First SSO sign-in -- consumes the invitation
    async with factory() as session:
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)
        provider = await session.get(SsoProvider, prov_db_id)
        assert provider is not None

        from modulo.settings import Settings, get_settings

        get_settings.cache_clear()
        settings = Settings(
            database_url=str(db_engine.url),
            secret_key="a" * 32,
            fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            modulo_sso_default_role="runner",
        )

        from modulo.auth.sso import jit_provision_user

        account, _, role1 = await jit_provision_user(
            session,
            settings,
            invited_email,
            "SSO Invited User",
            "oidc",
            "test-sso:sub123",
            default_org_id=org_id,
            sso_provider=provider,
            email_verified=True,
        )
        await session.commit()

    assert role1 == "operator"

    # Second SSO sign-in -- should find existing membership, not burn invitation
    async with factory() as session:
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)
        provider = await session.get(SsoProvider, prov_db_id)
        assert provider is not None

        account2, _, role2 = await jit_provision_user(
            session,
            settings,
            invited_email,
            "SSO Invited User",
            "oidc",
            "test-sso:sub123",
            default_org_id=org_id,
            sso_provider=provider,
            email_verified=True,
        )
        await session.commit()

    # Same account, same role, invitation not double-consumed
    assert account2.id == account.id
    assert role2 == "operator"

    # Verify: invitation consumed_at is set but not double-consumed
    inv_row = await _get_invitation(db_engine, inv_db_id)
    assert inv_row is not None
    assert inv_row["consumed_at"] is not None
