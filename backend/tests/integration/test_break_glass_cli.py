"""Integration tests for the break-glass CLI deliverable (B).

Runs the CLI core operations (activate / deactivate / force-last-admin /
smoke / status) against a REAL modulo_breakglass LOGIN engine on testcontainers
Postgres. Exercises the caller-bound ``deactivate_break_glass`` SECURITY
DEFINER operator branch (session_user='modulo_breakglass' only), the accounts
UPDATE allow-list boundary (the CLI INSERTs accounts and writes the three
break-glass columns), and the audit writes via the Python
``append_audit_event`` on the modulo_breakglass session (plan §5, iteration-16
CRITICAL — the chained audit_chain_heads write needs the SELECT+INSERT/UPDATE
grants).

If Docker / testcontainers is unavailable these tests cannot run — the unit
suite (tests/unit/cli/test_break_glass_cli.py) covers the wrapper exit codes
and pgcode mapping without a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.passwords import verify_password
from modulo.cli import break_glass as bg
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditEvent

pytestmark = pytest.mark.integration


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"BGCLI {org_id.hex[:8]}", "slug": f"bgcli-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_admin(engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, "
                "active, is_break_glass) VALUES (:id, :email, :name, 'hash', 'local', true, false)"
            ),
            {"id": str(acc_id), "email": f"admin-{acc_id.hex[:12]}@example.com", "name": "Admin"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'admin')"
            ),
            {"id": str(uuid.uuid4()), "aid": str(acc_id), "oid": str(org_id)},
        )
    return acc_id


@pytest_asyncio.fixture
async def bg_session(breakglass_engine: AsyncEngine) -> AsyncSession:
    factory = async_sessionmaker(breakglass_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session:
        yield session


async def _audit_types(session: AsyncSession, org_id: uuid.UUID) -> set[str]:
    async with session.begin():
        result = await session.execute(select(AuditEvent.event_type).where(AuditEvent.organisation_id == org_id))
        return {row[0] for row in result.all()}


async def test_activate_creates_row_and_audit(db_engine: AsyncEngine, bg_session: AsyncSession) -> None:
    org_id = await _create_org(db_engine)
    now = datetime.now(UTC)

    credential = await bg.activate(bg_session, now=now, org_id=org_id, ttl_minutes=30, actor="operator", reason="TKT-1")
    assert credential and len(credential) >= 20

    rows: list[dict] = []
    async with bg_session.begin():
        rows = await bg.status_rows(bg_session, org_id=org_id, all_rows=True, now=now)
    assert len(rows) == 1
    assert rows[0]["state"] == "live"
    assert rows[0]["reason"] == "TKT-1"
    assert rows[0]["actor"] == "operator"

    async with bg_session.begin():
        result = await bg_session.execute(select(Account).where(Account.email == rows[0]["email"]))
    account = result.scalar_one()
    assert account.is_break_glass is True
    assert account.active is True
    assert account.break_glass_expires_at is not None
    # The delivered credential is the single-use bcrypt credential.
    assert verify_password(credential, account.password_hash or "") is True

    types = await _audit_types(bg_session, org_id)
    assert "break_glass_activated" in types


async def test_deactivate_refused_then_forced(db_engine: AsyncEngine, bg_session: AsyncSession) -> None:
    org_id = await _create_org(db_engine)
    now = datetime.now(UTC)
    credential = await bg.activate(bg_session, now=now, org_id=org_id, ttl_minutes=30, actor="operator", reason="TKT-2")
    assert credential

    # Plain deactivate refuses while a live activation exists.
    with pytest.raises(bg.DeactivateRefusedError):
        await bg.deactivate(
            bg_session,
            org_id=org_id,
            account_id=None,
            actor="operator",
            reason="TKT-2",
            force=False,
            now=now,
        )

    result = await bg.deactivate(
        bg_session,
        org_id=org_id,
        account_id=None,
        actor="operator",
        reason="TKT-2",
        force=True,
        now=now,
    )
    assert result["deactivated"] == 1

    async with bg_session.begin():
        rows = await bg.status_rows(bg_session, org_id=org_id, all_rows=True, now=now)
    assert rows[0]["state"] == "deactivated"

    # expire_all() forces a DB refresh -- the SECURITY DEFINER deactivate updated
    # accounts via raw SQL, and expire_on_commit=False keeps the stale identity map.
    bg_session.expire_all()
    async with bg_session.begin():
        result = await bg_session.execute(select(Account).where(Account.email == rows[0]["email"]))
    account = result.scalar_one()
    assert account.active is False
    assert account.break_glass_expires_at is None
    assert account.break_glass_deactivated_at is not None

    types = await _audit_types(bg_session, org_id)
    assert "break_glass_deactivated" in types


async def test_force_last_admin_removes_last_non_bg_admin(db_engine: AsyncEngine, bg_session: AsyncSession) -> None:
    org_id = await _create_org(db_engine)
    admin_id = await _create_admin(db_engine, org_id)

    result = await bg.force_last_admin(
        bg_session, org_id=org_id, actor="operator", reason="TKT-3", now=datetime.now(UTC)
    )
    assert result["removed_account_id"] == str(admin_id)

    async with bg_session.begin():
        result = await bg_session.execute(select(Account).where(Account.id == admin_id))
    account = result.scalar_one()
    assert account.active is False

    types = await _audit_types(bg_session, org_id)
    assert "last_admin_forcibly_removed" in types


async def test_force_last_admin_refuses_bg_only_org(db_engine: AsyncEngine, bg_session: AsyncSession) -> None:
    org_id = await _create_org(db_engine)
    now = datetime.now(UTC)
    await bg.activate(bg_session, now=now, org_id=org_id, ttl_minutes=30, actor="operator", reason="TKT-4")

    with pytest.raises(bg.PreconditionError):
        await bg.force_last_admin(bg_session, org_id=org_id, actor="operator", reason="TKT-4", now=now)


async def test_status_all_lists_all_orgs(db_engine: AsyncEngine, bg_session: AsyncSession) -> None:
    org_a = await _create_org(db_engine)
    org_b = await _create_org(db_engine)
    now = datetime.now(UTC)
    await bg.activate(bg_session, now=now, org_id=org_a, ttl_minutes=30, actor="operator", reason="TKT-A")
    await bg.activate(bg_session, now=now, org_id=org_b, ttl_minutes=30, actor="operator", reason="TKT-B")

    async with bg_session.begin():
        rows = await bg.status_rows(bg_session, org_id=None, all_rows=True, now=now)
    our_slugs = {f"bgcli-{org_a.hex[:8]}", f"bgcli-{org_b.hex[:8]}"}
    our_rows = [row for row in rows if row["org_slug"] in our_slugs]
    assert len(our_rows) == 2
    assert all(row["state"] == "live" for row in our_rows)


async def test_smoke_probe(bg_session: AsyncSession) -> None:
    async with bg_session.begin():
        result = await bg.smoke(bg_session)
    assert result["connectivity"] == "ok"
    assert result["session_user"] == "modulo_breakglass"
    assert result["deactivate_function"] is not None
