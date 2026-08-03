"""Integration tests for break-glass deliverable (B) chunk 1 — enforcement.

0037_break_glass_enforcement + the SQL-predicate deny, against a real
Postgres (testcontainers): the accounts UPDATE allow-list holds for
``modulo_app`` AND PUBLIC, ``modulo_breakglass`` has the three break-glass
column grants only (no DELETE, no other UPDATE), and the deny predicate in
``resolve_role_from_membership`` folds denied break-glass accounts to None
(expired / NULL-expiry / deactivated / inactive) while live ones resolve
their role. A positive control proves a rogue PUBLIC/table-level UPDATE grant
is detected by the bootstrap allow-list assertion.
"""

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.bootstrap_role import ACCOUNTS_WRITABLE_COLUMNS, _find_allow_list_violations
from modulo.db.crud.break_glass_deny import BREAK_GLASS_COLUMNS
from modulo.db.crud.org_membership import resolve_role_from_membership

_BG_COLS = tuple(BREAK_GLASS_COLUMNS)


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"ENF {org_id.hex[:8]}", "slug": f"enf-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_account(
    engine: AsyncEngine,
    *,
    is_break_glass: bool = True,
    expires_at: datetime | None = None,
    deactivated_at: datetime | None = None,
    active: bool = True,
) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active, "
                "is_break_glass, break_glass_expires_at, break_glass_deactivated_at) "
                "VALUES (:id, :email, :name, 'hash', 'local', :active, :bg, :exp, :deactivated)"
            ),
            {
                "id": str(acc_id),
                "email": f"enf-{acc_id.hex[:12]}@example.com",
                "name": f"ENF {acc_id.hex[:8]}",
                "active": active,
                "bg": is_break_glass,
                "exp": expires_at,
                "deactivated": deactivated_at,
            },
        )
    return acc_id


async def _create_membership(
    engine: AsyncEngine, *, org_id: uuid.UUID, account_id: uuid.UUID, role: str = "admin"
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, :role)"),
            {"id": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id), "role": role},
        )


async def _pg_connect(migrated_db_url: str) -> asyncpg.Connection:
    url = migrated_db_url.replace("postgresql+asyncpg://", "postgres://").split("?")[0]
    return await asyncpg.connect(url, ssl=False)


# ── modulo_breakglass column grants ──────────────────────────────────


async def test_modulo_breakglass_has_only_break_glass_column_updates(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        for col in _BG_COLS:
            granted = (
                await conn.execute(
                    text("SELECT has_column_privilege('modulo_breakglass', 'public.accounts', :col, 'UPDATE')"),
                    {"col": col},
                )
            ).scalar_one()
            assert granted is True, f"modulo_breakglass should UPDATE {col}"

        for col in ("email", "password_hash", "active", "display_name"):
            granted = (
                await conn.execute(
                    text("SELECT has_column_privilege('modulo_breakglass', 'public.accounts', :col, 'UPDATE')"),
                    {"col": col},
                )
            ).scalar_one()
            assert granted is False, f"modulo_breakglass must NOT UPDATE {col}"

        has_delete = (
            await conn.execute(text("SELECT has_table_privilege('modulo_breakglass', 'public.accounts', 'DELETE')"))
        ).scalar_one()
        assert has_delete is False, "modulo_breakglass must not DELETE from accounts"


# ── allow-list boundary (modulo_app + PUBLIC) ────────────────────────


async def test_no_table_level_update_on_accounts_for_app_or_public(db_engine: AsyncEngine) -> None:
    """role_table_grants is table-level only by construction — no column_name
    predicate. Runs on the superuser engine (an unrelated role would see zero
    rows and false-pass)."""
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT grantee FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' AND table_name = 'accounts' "
                    "AND privilege_type = 'UPDATE' AND grantee IN ('modulo_app', 'PUBLIC')"
                )
            )
        ).fetchall()
        assert rows == [], f"accounts has a table-level UPDATE grant: {rows}"


async def test_allow_list_set_equality(db_engine: AsyncEngine) -> None:
    """Inverted schema-evolution test: the set of accounts columns modulo_app
    can UPDATE equals the single-sourced allow-listed constant, and the three
    break-glass columns are NOT writable by it."""
    async with db_engine.connect() as conn:
        cols = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'accounts'"
                    )
                )
            ).fetchall()
        }
        granted: set[str] = set()
        for col in sorted(cols):
            ok = (
                await conn.execute(
                    text("SELECT has_column_privilege('modulo_app', 'public.accounts', :col, 'UPDATE')"),
                    {"col": col},
                )
            ).scalar_one()
            if ok:
                granted.add(col)

        assert granted == set(ACCOUNTS_WRITABLE_COLUMNS), (
            f"modulo_app UPDATE-grant drift: granted={sorted(granted)} allow_list={sorted(ACCOUNTS_WRITABLE_COLUMNS)}"
        )
        for col in _BG_COLS:
            assert col not in granted, f"modulo_app must not UPDATE {col}"


async def test_allow_list_positive_control(migrated_db_url: str) -> None:
    """A rogue PUBLIC/table-level UPDATE grant must make the bootstrap
    allow-list assertion detect a violation (iteration-17 positive control)."""
    conn = await _pg_connect(migrated_db_url)
    try:
        assert await _find_allow_list_violations(conn, "modulo_app") == []

        await conn.execute("GRANT UPDATE ON public.accounts TO PUBLIC")
        try:
            violations = await _find_allow_list_violations(conn, "modulo_app")
            assert any("PUBLIC" in v for v in violations), violations
        finally:
            await conn.execute("REVOKE UPDATE ON public.accounts FROM PUBLIC")

        assert await _find_allow_list_violations(conn, "modulo_app") == []
    finally:
        await conn.close()


# ── SQL-predicate deny at the canonical role-resolution site ─────────


@pytest_asyncio.fixture
async def enf_org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _create_org(db_engine)


async def test_denied_break_glass_resolves_none_live_resolves_role(
    db_engine: AsyncEngine, db_session: AsyncSession, enf_org: uuid.UUID
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    past = datetime.now(UTC) - timedelta(seconds=1)
    live = await _create_account(db_engine, is_break_glass=True, expires_at=future)
    expired = await _create_account(db_engine, is_break_glass=True, expires_at=past)
    # NULL-expiry is CHECK-representable only as the deactivated tombstone
    # (live NULL-expiry rows violate ck_accounts_break_glass_expiry).
    null_expiry = await _create_account(db_engine, is_break_glass=True, expires_at=None, deactivated_at=past)
    deactivated = await _create_account(db_engine, is_break_glass=True, expires_at=future, deactivated_at=past)
    inactive = await _create_account(db_engine, is_break_glass=True, expires_at=future, active=False)

    for acc in (live, expired, null_expiry, deactivated, inactive):
        await _create_membership(db_engine, org_id=enf_org, account_id=acc, role="admin")

    live_role = await resolve_role_from_membership(db_session, str(live), str(enf_org))
    assert live_role == "admin"

    for denied in (expired, null_expiry, deactivated, inactive):
        resolved = await resolve_role_from_membership(db_session, str(denied), str(enf_org))
        assert resolved is None, f"denied break-glass account {denied} must resolve None, got {resolved!r}"


async def test_recovery_cycle_deny_at_canonical_site(
    db_engine: AsyncEngine, db_session: AsyncSession, enf_org: uuid.UUID
) -> None:
    """The full recovery-cycle deny: expired / NULL-expiry / deactivated
    break-glass memberships all return None at the canonical site
    (resolve_role_from_membership), and a live one still resolves."""
    future = datetime.now(UTC) + timedelta(hours=1)
    past = datetime.now(UTC) - timedelta(seconds=1)
    expired = await _create_account(db_engine, is_break_glass=True, expires_at=past)
    null_expiry = await _create_account(db_engine, is_break_glass=True, expires_at=None, deactivated_at=past)
    deactivated = await _create_account(db_engine, is_break_glass=True, expires_at=future, deactivated_at=past)
    live = await _create_account(db_engine, is_break_glass=True, expires_at=future)

    for acc in (expired, null_expiry, deactivated, live):
        await _create_membership(db_engine, org_id=enf_org, account_id=acc, role="admin")

    assert await resolve_role_from_membership(db_session, str(expired), str(enf_org)) is None
    assert await resolve_role_from_membership(db_session, str(null_expiry), str(enf_org)) is None
    assert await resolve_role_from_membership(db_session, str(deactivated), str(enf_org)) is None
    assert await resolve_role_from_membership(db_session, str(live), str(enf_org)) == "admin"
