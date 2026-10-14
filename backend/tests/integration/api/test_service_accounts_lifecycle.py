"""Integration tests: service-account lifecycle on REAL Postgres (FAR-1478).

Drives the real endpoint functions against a NOBYPASSRLS role (the production
``modulo_app`` posture — RLS applies) and asserts the rows that land:

* provisioning creates a ``kind='service'`` account with NO human-shaped
  identity (email / password_hash / sso_subject / auth_provider all NULL) and
  an org membership at the granted role;
* a key minted FOR the service principal is owned by it
  (``org_api_keys.account_id`` = the service account, never the human) and
  honours the 365-day service TTL ceiling;
* decommission tombstones the membership, flips ``active`` off, stamps
  ``decommissioned_at`` and bulk-revokes every live key;
* the 0311 role-ceiling TRIGGER fires for the real runtime role: an ``admin``
  org role on a service membership is refused with SQLSTATE 23514 even via
  raw SQL, and the human user-admin surface refuses a service target (422);
* a service account can hold a TEAM membership (the existing team surface),
  while a team ``admin`` role remains structurally impossible (CHECK).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes.admin import UpdateUserRequest, admin_update_user
from modulo.api.routes.service_accounts import (
    ServiceAccountCreate,
    ServiceAccountKeyCreate,
    ServiceAccountUpdate,
    create_service_account_endpoint,
    create_service_account_key_endpoint,
    decommission_service_account_endpoint,
    update_service_account_endpoint,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.team import create_team
from modulo.db.crud.team_membership import add_team_member
from modulo.db.rls import set_rls_org, set_rls_user_context

pytestmark = pytest.mark.integration


def _admin_principal(org_id: uuid.UUID, account_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="integration-admin",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )


@pytest_asyncio.fixture
async def app_session(app_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """NOBYPASSRLS session (RLS applies), rolled back per test via dispose."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


class TestServiceAccountLifecycle:
    async def test_provision_mint_decommission_end_to_end(
        self,
        app_session: AsyncSession,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        principal = _admin_principal(test_org, test_user)

        # --- provision -----------------------------------------------------
        created = await create_service_account_endpoint(
            req=ServiceAccountCreate(name="  ci-bot  ", role="operator"),
            session=app_session,
            principal=principal,
        )
        sa_id = uuid.UUID(created.id)
        assert created.kind == "service"
        assert created.name == "ci-bot"
        assert created.role == "operator"

        async with db_engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT kind, email, password_hash, auth_provider, sso_subject, active "
                            "FROM accounts WHERE id = :id"
                        ),
                        {"id": str(sa_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["kind"] == "service"
        assert row["email"] is None
        assert row["password_hash"] is None
        assert row["auth_provider"] is None
        assert row["sso_subject"] is None
        assert row["active"] is True

        # --- mint a key OWNED by the service principal ----------------------
        minted = await create_service_account_key_endpoint(
            service_account_id=sa_id,
            req=ServiceAccountKeyCreate(name="ci key", role="runner"),
            session=app_session,
            principal=principal,
        )
        assert minted.owner_account_id == sa_id
        assert minted.key_value.startswith("mk_")
        async with db_engine.connect() as conn:
            key_row = (
                (
                    await conn.execute(
                        text(
                            "SELECT account_id::text, role, scope, revoked_at, expires_at "
                            "FROM org_api_keys WHERE id = :id"
                        ),
                        {"id": str(minted.id)},
                    )
                )
                .mappings()
                .one()
            )
        assert key_row["account_id"] == str(sa_id)
        assert key_row["role"] == "runner"
        assert key_row["scope"] == "org"
        assert key_row["revoked_at"] is None
        ttl_days = (key_row["expires_at"] - datetime.now(UTC)).days
        assert 364 <= ttl_days <= 365

        # --- the 365-day service TTL ceiling is enforced at the choke point -
        too_long = datetime.now(UTC) + timedelta(days=400)
        with pytest.raises(HTTPException) as exc:
            await create_service_account_key_endpoint(
                service_account_id=sa_id,
                req=ServiceAccountKeyCreate(name="long key", expires_at=too_long.isoformat()),
                session=app_session,
                principal=principal,
            )
        assert exc.value.status_code == 422
        assert "365" in str(exc.value.detail)

        # --- rename ---------------------------------------------------------
        updated = await update_service_account_endpoint(
            service_account_id=sa_id,
            req=ServiceAccountUpdate(name="renamed-bot"),
            session=app_session,
            principal=principal,
        )
        assert updated.name == "renamed-bot"

        # --- decommission: keys die with the principal ----------------------
        decommissioned = await decommission_service_account_endpoint(
            service_account_id=sa_id,
            session=app_session,
            principal=principal,
        )
        assert decommissioned.revoked_keys == 1
        async with db_engine.connect() as conn:
            account_row = (
                (
                    await conn.execute(
                        text("SELECT active, decommissioned_at IS NOT NULL AS retired FROM accounts WHERE id = :id"),
                        {"id": str(sa_id)},
                    )
                )
                .mappings()
                .one()
            )
            key_state = (
                (
                    await conn.execute(
                        text("SELECT revoked_at IS NOT NULL AS revoked FROM org_api_keys WHERE id = :id"),
                        {"id": str(minted.id)},
                    )
                )
                .mappings()
                .one()
            )
        assert account_row["active"] is False
        assert account_row["retired"] is True
        assert key_state["revoked"] is True

        # A decommissioned principal cannot be re-commissioned or mutated.
        with pytest.raises(HTTPException) as repeat:
            await decommission_service_account_endpoint(
                service_account_id=sa_id,
                session=app_session,
                principal=principal,
            )
        assert repeat.value.status_code == 409

    async def test_admin_role_grant_is_structurally_refused(
        self,
        app_session: AsyncSession,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        principal = _admin_principal(test_org, test_user)
        created = await create_service_account_endpoint(
            req=ServiceAccountCreate(name="ceiling-bot", role="operator"),
            session=app_session,
            principal=principal,
        )
        sa_id = uuid.UUID(created.id)

        # Application surface: 422, never a silent clamp.
        with pytest.raises(HTTPException) as exc:
            await update_service_account_endpoint(
                service_account_id=sa_id,
                req=ServiceAccountUpdate(role="admin"),
                session=app_session,
                principal=principal,
            )
        assert exc.value.status_code == 422

        # Structural backstop: the 0311 TRIGGER refuses raw SQL too (23514).
        with pytest.raises(DBAPIError) as raw:
            async with app_session.begin():
                await app_session.execute(
                    text("UPDATE org_memberships SET role = 'admin' WHERE account_id = :aid"),
                    {"aid": str(sa_id)},
                )
        orig = getattr(raw.value, "orig", None)
        pgcode = str(getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", "") or "")
        assert pgcode == "23514"

    async def test_human_user_admin_surface_rejects_service_target(
        self,
        app_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        principal = _admin_principal(test_org, test_user)
        created = await create_service_account_endpoint(
            req=ServiceAccountCreate(name="guarded-bot"),
            session=app_session,
            principal=principal,
        )
        sa_id = uuid.UUID(created.id)

        with pytest.raises(HTTPException) as exc:
            await admin_update_user(
                user_id=sa_id,
                req=UpdateUserRequest(org_role="admin"),
                current_user=principal,
                session=app_session,
            )
        assert exc.value.status_code == 422
        assert "service-accounts" in str(exc.value.detail)

    async def test_service_account_holds_a_team_membership(
        self,
        app_session: AsyncSession,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        principal = _admin_principal(test_org, test_user)
        created = await create_service_account_endpoint(
            req=ServiceAccountCreate(name="team-bot", role="operator"),
            session=app_session,
            principal=principal,
        )
        sa_id = uuid.UUID(created.id)

        async with app_session.begin():
            await set_rls_org(app_session, test_org)
            await set_rls_user_context(app_session, test_user, "admin")
            team = await create_team(
                app_session,
                org_id=test_org,
                name="service-account-team",
                account_id=test_user,
            )
            membership = await add_team_member(
                app_session,
                org_id=test_org,
                team_id=team.id,
                account_id=sa_id,
                role="operator",
            )
        assert membership.role == "operator"

        # Team roles never include admin (ck_team_memberships_role) — the
        # CHECK is the structural ceiling on the team axis.
        with pytest.raises(DBAPIError):
            async with app_session.begin():
                await app_session.execute(
                    text(
                        "INSERT INTO team_memberships (id, organisation_id, team_id, account_id, role) "
                        "VALUES (:id, :oid, :tid, :aid, 'admin')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "oid": str(test_org),
                        "tid": str(team.id),
                        "aid": str(sa_id),
                    },
                )

        # Decommission removes the team membership in the same act.
        decommissioned = await decommission_service_account_endpoint(
            service_account_id=sa_id,
            session=app_session,
            principal=principal,
        )
        assert decommissioned.team_memberships_removed == 1
        async with db_engine.connect() as conn:
            remaining = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM team_memberships WHERE account_id = :aid"),
                    {"aid": str(sa_id)},
                )
            ).scalar_one()
        assert remaining == 0

    async def test_admin_bootstrap_can_be_replaced_by_self_mint_ownership(
        self,
        app_session: AsyncSession,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """The ownership stamp is the TARGET, whatever the minting path is.

        A service principal's own ``mk_`` credential resolves to the same
        principal id (the key's ``account_id``), so the self-mint path stamps
        the same owner as the admin bootstrap — pinned here at the DB level
        for the admin path, and by the live-role clamp reading the SA's own
        membership.
        """
        principal = _admin_principal(test_org, test_user)
        created = await create_service_account_endpoint(
            req=ServiceAccountCreate(name="owner-bot", role="operator"),
            session=app_session,
            principal=principal,
        )
        sa_id = uuid.UUID(created.id)

        # A runner-role key for the SA: cap reads the SA's LIVE role.
        minted = await create_service_account_key_endpoint(
            service_account_id=sa_id,
            req=ServiceAccountKeyCreate(name="owner key", role="runner"),
            session=app_session,
            principal=principal,
        )
        assert minted.owner_account_id == sa_id

        # A viewer-role SA cannot mint a runner key above its live role.
        await update_service_account_endpoint(
            service_account_id=sa_id,
            req=ServiceAccountUpdate(role="viewer"),
            session=app_session,
            principal=principal,
        )
        with pytest.raises(HTTPException) as cap:
            await create_service_account_key_endpoint(
                service_account_id=sa_id,
                req=ServiceAccountKeyCreate(name="capped key", role="runner"),
                session=app_session,
                principal=principal,
            )
        assert cap.value.status_code == 403

        # The membership the live-role clamp reads is the one we assigned.
        async with db_engine.connect() as conn:
            role = (
                await conn.execute(
                    text("SELECT role FROM org_memberships WHERE account_id = :aid"),
                    {"aid": str(sa_id)},
                )
            ).scalar_one()
        assert role == "viewer"
