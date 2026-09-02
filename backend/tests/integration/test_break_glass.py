"""Integration tests for break-glass deliverable (A) — last-admin prevention.

Exercises the caller-bound ``deactivate_break_glass`` SECURITY DEFINER
(reconciliation chain 0108_schema_org_identity, redefined per-org by 0173)
against a real Postgres: M2010/M2020/M2040 pgcodes, force gating on the
operator role (real login vs SET ROLE), scoped-vs-global deactivation (the
non-operator branch is per-org since FAR-533/gh-1794 — the membership
tombstone is the signal and accounts.active stays true; the operator branch
keeps the global accounts.active flip), the active IS TRUE membership JOIN
fix, SCIM DELETE parity + re-create reversibility, the accounts UPDATE
allow-list boundary, the break-glass surface posture, and the
lookup_api_key_org regression.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.api.routes.admin import _extract_bg_pgcode
from modulo.db.crud.org_membership import resolve_role_from_membership
from modulo.db.rls import set_rls_org

# ── helpers ──────────────────────────────────────────────────────────


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"BG {org_id.hex[:8]}", "slug": f"bg-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_account(
    engine: AsyncEngine,
    *,
    email: str | None = None,
    active: bool = True,
    is_break_glass: bool = False,
    expires_at: str | None = None,
) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active, is_break_glass, break_glass_expires_at) "
                "VALUES (:id, :email, :name, 'hash', 'local', :active, :bg, :exp)"
            ),
            {
                "id": str(acc_id),
                "email": email or f"bg-{acc_id.hex[:12]}@example.com",
                "name": f"BG User {acc_id.hex[:8]}",
                "active": active,
                "bg": is_break_glass,
                "exp": expires_at,
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


async def _call_deactivate(
    engine: AsyncEngine,
    caller: uuid.UUID,
    target: uuid.UUID,
    *,
    force: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT public.deactivate_break_glass(:caller, :target, :force)"),
            {"caller": str(caller), "target": str(target), "force": force},
        )


def _pgcode_of(exc: BaseException) -> str | None:
    return _extract_bg_pgcode(exc)


@pytest_asyncio.fixture
async def bg_org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _create_org(db_engine)


# ── schema / ownership ───────────────────────────────────────────────


async def test_break_glass_columns_and_check_exist(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        cols = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'accounts' AND table_schema = 'public'"
                    )
                )
            ).fetchall()
        }
        assert {"is_break_glass", "break_glass_expires_at", "break_glass_deactivated_at"} <= cols

        check = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint WHERE conname = 'ck_accounts_break_glass_expiry' "
                    "AND conrelid = 'public.accounts'::regclass"
                )
            )
        ).scalar_one_or_none()
        assert check == 1

        owners = {
            row[0]: row[1]
            for row in (
                await conn.execute(
                    text(
                        "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c "
                        "WHERE c.oid IN ('public.accounts'::regclass, 'public.org_memberships'::regclass, "
                        "'public.token_families'::regclass, 'public.org_api_keys'::regclass)"
                    )
                )
            ).fetchall()
        }
        assert set(owners.values()) == {"modulo_migrate"}, owners

        func_owner = (
            await conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) FROM pg_proc p "
                    "WHERE p.oid = 'public.lookup_api_key_org(text)'::regprocedure"
                )
            )
        ).scalar_one()
        assert func_owner == "modulo_migrate"


async def test_break_glass_columns_not_writable_by_modulo_app(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    """The accounts UPDATE allow-list (active from (A)) blocks modulo_app from
    writing the break-glass columns while allowing the writable ones."""
    account_id = await _create_account(db_engine, is_break_glass=False)

    async with modulo_app_engine.begin() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                text("UPDATE accounts SET is_break_glass = true WHERE id = :id"),
                {"id": str(account_id)},
            )
        assert "permission denied" in str(exc_info.value).lower()

    async with modulo_app_engine.begin() as conn:
        await conn.execute(
            text("UPDATE accounts SET display_name = 'allow-listed-write' WHERE id = :id"),
            {"id": str(account_id)},
        )


# ── caller-bound SECURITY DEFINER ────────────────────────────────────


async def test_caller_with_no_shared_org_is_rejected_m2010(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    caller = await _create_account(db_engine)
    target = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=target, role="admin")

    with pytest.raises(DBAPIError) as exc_info:
        await _call_deactivate(modulo_app_engine, caller, target)
    assert _pgcode_of(exc_info.value) == "M2010"


async def test_last_admin_m2020(modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID) -> None:
    other_org = await _create_org(db_engine)
    caller = await _create_account(db_engine)
    target = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=caller, role="admin")
    # Target shares bg_org with the caller (authorizes the caller) AND is the
    # ONLY admin in other_org (caller has no membership there).
    await _create_membership(db_engine, org_id=bg_org, account_id=target, role="admin")
    await _create_membership(db_engine, org_id=other_org, account_id=target, role="admin")

    # Deactivating the target would orphan other_org (its last admin) -> M2020.
    with pytest.raises(DBAPIError) as exc_info:
        await _call_deactivate(modulo_app_engine, caller, target)
    assert _pgcode_of(exc_info.value) == "M2020"


async def test_deactivation_succeeds_scoped_to_shared_org(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    other_org = await _create_org(db_engine)
    caller = await _create_account(db_engine)
    target = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=caller, role="admin")
    await _create_membership(db_engine, org_id=bg_org, account_id=target, role="admin")
    # A third admin keeps bg_org from being orphaned.
    third = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=third, role="admin")
    # Target also belongs to another org where the caller has NO membership, but
    # that org has its own admin so the target is NOT its last admin.
    await _create_membership(db_engine, org_id=other_org, account_id=target, role="admin")
    other_admin = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=other_org, account_id=other_admin, role="admin")

    await _call_deactivate(modulo_app_engine, caller, target)

    async with db_engine.connect() as conn:
        account_active = (
            await conn.execute(text("SELECT active FROM accounts WHERE id = :id"), {"id": str(target)})
        ).scalar_one()
        # FAR-533 (gh-1794): deactivation is PER-ORG — the membership
        # tombstone below is the signal, accounts.active stays true.
        assert account_active is True

        shared_membership = (
            await conn.execute(
                text(
                    "SELECT deactivated_at IS NOT NULL FROM org_memberships "
                    "WHERE account_id = :id AND organisation_id = :oid"
                ),
                {"id": str(target), "oid": str(bg_org)},
            )
        ).scalar_one()
        assert shared_membership is True

        # The non-shared org membership is untouched (scoped marker rows).
        other_membership = (
            await conn.execute(
                text(
                    "SELECT deactivated_at IS NOT NULL FROM org_memberships "
                    "WHERE account_id = :id AND organisation_id = :oid"
                ),
                {"id": str(target), "oid": str(other_org)},
            )
        ).scalar_one()
        assert other_membership is False


async def test_target_missing_m2040(breakglass_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID) -> None:
    # A non-operator caller is M2010 (not authorized to deactivate a target they
    # share no org with); the operator branch reaches the M2040 target-exists
    # check (GET DIAGNOSTICS / FOUND after the UPDATE).
    operator = await _create_account(db_engine)
    missing = uuid.uuid4()
    with pytest.raises(DBAPIError) as exc_info:
        await _call_deactivate(breakglass_engine, operator, missing)
    assert _pgcode_of(exc_info.value) == "M2040"


async def test_force_requires_operator_and_set_role_does_not_qualify(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    """force_last_admin is gated on session_user='modulo_breakglass'.

    A modulo_app session (SET ROLE modulo_app, session_user = superuser) and a
    SET ROLE modulo_breakglass session BOTH fail with M2010 — only a REAL LOGIN
    as modulo_breakglass satisfies the operator branch.
    """
    caller = await _create_account(db_engine)
    target = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=caller, role="admin")
    await _create_membership(db_engine, org_id=bg_org, account_id=target, role="admin")

    # modulo_app (non-operator) force=true -> M2010
    with pytest.raises(DBAPIError) as exc_info:
        await _call_deactivate(modulo_app_engine, caller, target, force=True)
    assert _pgcode_of(exc_info.value) == "M2010"

    # A SET ROLE modulo_breakglass session (session_user still the superuser) -> M2010
    raw_url = db_engine.url.render_as_string(hide_password=False)
    set_role_engine = create_async_engine(raw_url, poolclass=NullPool)

    @event.listens_for(set_role_engine.sync_engine, "checkout")
    def _set_role(dba_conn: object, _record: object, _proxy: object) -> None:
        cursor = dba_conn.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute('SET ROLE "modulo_breakglass"')
        finally:
            cursor.close()

    try:
        with pytest.raises(DBAPIError) as exc_info:
            await _call_deactivate(set_role_engine, caller, target, force=True)
        assert _pgcode_of(exc_info.value) == "M2010"
    finally:
        await set_role_engine.dispose()


async def test_operator_real_login_force_removes_last_admin(
    breakglass_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    """The operator branch (REAL modulo_breakglass login) can force-remove the
    last non-break-glass admin."""
    target = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=target, role="admin")
    operator = await _create_account(db_engine)

    await _call_deactivate(breakglass_engine, operator, target, force=True)

    async with db_engine.connect() as conn:
        active = (
            await conn.execute(text("SELECT active FROM accounts WHERE id = :id"), {"id": str(target)})
        ).scalar_one()
        assert active is False
        membership_deactivated = (
            await conn.execute(
                text(
                    "SELECT deactivated_at IS NOT NULL FROM org_memberships "
                    "WHERE account_id = :id AND organisation_id = :oid"
                ),
                {"id": str(target), "oid": str(bg_org)},
            )
        ).scalar_one()
        assert membership_deactivated is True


async def test_operator_force_refused_without_force_last_admin(
    breakglass_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    """Even the operator fires M2020 for the last non-bg admin unless force=true."""
    target = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=target, role="admin")
    operator = await _create_account(db_engine)

    with pytest.raises(DBAPIError) as exc_info:
        await _call_deactivate(breakglass_engine, operator, target, force=False)
    assert _pgcode_of(exc_info.value) == "M2020"


# ── active IS TRUE membership JOIN (account-global deactivation fix) ──


async def test_deactivated_account_resolves_none_role(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    active_admin = await _create_account(db_engine, active=True)
    deactivated_admin = await _create_account(db_engine, active=False)
    await _create_membership(db_engine, org_id=bg_org, account_id=active_admin, role="admin")
    await _create_membership(db_engine, org_id=bg_org, account_id=deactivated_admin, role="admin")

    factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, bg_org)
        active_role = await resolve_role_from_membership(session, str(active_admin), str(bg_org))
        deactivated_role = await resolve_role_from_membership(session, str(deactivated_admin), str(bg_org))
        await session.rollback()

    assert active_role == "admin"
    assert deactivated_role is None


# ── SCIM DELETE parity + re-create reversibility ─────────────────────


async def test_scim_deactivate_and_recreate_reversible(db_engine: AsyncEngine, bg_org: uuid.UUID) -> None:
    from modulo.db.crud.scim import scim_create_user, scim_deactivate_user

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    caller = await _create_account(db_engine)
    await _create_membership(db_engine, org_id=bg_org, account_id=caller, role="admin")

    async with factory() as session, session.begin():
        scim_user = await scim_create_user(
            session,
            org_id=bg_org,
            email=f"scim-{uuid.uuid4().hex[:8]}@example.com",
            display_name="SCIM User",
            active=True,
        )
        scim_id = scim_user.id
        await session.commit()

    async with factory() as session, session.begin():
        deactivated = await scim_deactivate_user(session, bg_org, scim_id, caller_account_id=caller)
        assert deactivated is not None
        # FAR-533 (gh-1794): per-org deactivation leaves accounts.active true.
        assert deactivated.active is True
        await session.commit()

    async with db_engine.connect() as conn:
        membership_deactivated = (
            await conn.execute(
                text(
                    "SELECT deactivated_at IS NOT NULL FROM org_memberships "
                    "WHERE account_id = :id AND organisation_id = :oid"
                ),
                {"id": str(scim_id), "oid": str(bg_org)},
            )
        ).scalar_one()
        assert membership_deactivated is True  # tombstone, not hard-delete

    # IdP delete-then-recreate: the tombstoned membership is re-creatable.
    async with factory() as session, session.begin():
        await scim_create_user(
            session,
            org_id=bg_org,
            email=f"scim-{uuid.uuid4().hex[:8]}@example.com".replace("example.com", "recreate.example.com"),
            display_name="SCIM Recreated",
            active=True,
        )
        await session.commit()

    # Re-create of the SAME email (not a new one) clears the tombstone:
    async with factory() as session, session.begin():
        original_email = (
            await session.execute(text("SELECT email FROM accounts WHERE id = :id"), {"id": str(scim_id)})
        ).scalar_one()
        recreated_same = await scim_create_user(
            session,
            org_id=bg_org,
            email=original_email,
            display_name="SCIM Recreated",
            active=True,
        )
        assert recreated_same.id == scim_id
        assert recreated_same.active is True
        await session.commit()

    async with db_engine.connect() as conn:
        membership_deactivated = (
            await conn.execute(
                text(
                    "SELECT deactivated_at IS NOT NULL FROM org_memberships "
                    "WHERE account_id = :id AND organisation_id = :oid"
                ),
                {"id": str(scim_id), "oid": str(bg_org)},
            )
        ).scalar_one()
        assert membership_deactivated is False  # tombstone cleared on re-create
        password_hash = (
            await conn.execute(text("SELECT password_hash FROM accounts WHERE id = :id"), {"id": str(scim_id)})
        ).scalar_one()
        assert password_hash is None  # SCIM-managed re-create resets the hash


# ── lookup_api_key_org regression (re-owned, still callable) ─────────


async def test_lookup_api_key_org_regression(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine, bg_org: uuid.UUID
) -> None:
    key_id = uuid.uuid4()
    prefix = "bgtest1"
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO org_api_keys (id, organisation_id, name, lookup_prefix, "
                "hashed_secret, role, account_id, expires_at) "
                "VALUES (:id, :oid, 'k', :prefix, 'x', 'runner', :acc, now() + interval '1 day')"
            ),
            {
                "id": str(key_id),
                "oid": str(bg_org),
                "prefix": prefix,
                "acc": str(await _create_account(db_engine)),
            },
        )

    async with modulo_app_engine.connect() as conn:
        resolved = (
            await conn.execute(text("SELECT public.lookup_api_key_org(:prefix)"), {"prefix": prefix})
        ).scalar_one()
        assert str(resolved) == str(bg_org)


async def test_modulo_migrate_has_schema_usage(db_engine: AsyncEngine) -> None:
    """The SECURITY DEFINER function owner must resolve schema public objects.

    ``lookup_api_key_org`` is owned by ``modulo_migrate`` (reconciliation chain)
    and executes with that role's privileges (SECURITY DEFINER). The function body
    does ``FROM org_api_keys`` through schema public, so ``modulo_migrate``
    needs USAGE on schema public. ``bootstrap_role`` grants it; without it,
    every API-key-authenticated request fails with
    ``UndefinedTableError: relation "org_api_keys" does not exist`` on DBs that
    revoke the PUBLIC default schema USAGE (the deployed DB does). This test
    revokes the PUBLIC default grant to mirror production, so it fails on the
    regression even where testcontainers otherwise keeps the PUBLIC default.
    """
    async with db_engine.begin() as conn:
        await conn.execute(text("REVOKE USAGE ON SCHEMA public FROM PUBLIC"))
    try:
        async with db_engine.connect() as conn:
            has_usage = (
                await conn.execute(text("SELECT has_schema_privilege('modulo_migrate', 'public', 'USAGE')"))
            ).scalar_one()
            assert has_usage is True

            # Belt-and-braces: the function must actually resolve org_api_keys as
            # modulo_migrate (no UndefinedTableError). A missing-prefix lookup
            # returns NULL; a broken schema-USAGE grant raises.
            resolved = (
                await conn.execute(text("SELECT public.lookup_api_key_org('no_such_prefix_zz')"))
            ).scalar_one_or_none()
            assert resolved is None
    finally:
        # Restore the PUBLIC default so the shared session-scoped DB is untouched
        # for the tests that follow.
        async with db_engine.begin() as conn:
            await conn.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))


# ── break-glass posture (final chain state) ─────────────────────────


async def test_break_glass_surface_present(db_engine: AsyncEngine) -> None:
    """The reconciliation chain (0108_schema_org_identity) keeps the break-glass
    surface: ``accounts.is_break_glass`` and the caller-bound
    ``deactivate_break_glass`` SECURITY DEFINER. The old downgrade round-trip
    (0036 -> heads) no longer exists — reconciliation downgrades are no-ops and
    the chain never drops the surface, so the posture assertions are the
    meaningful contract.
    """
    async with db_engine.connect() as conn:
        cols = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'accounts' AND table_schema = 'public'"
                    )
                )
            ).fetchall()
        }
        assert "is_break_glass" in cols
        func_exists = (
            await conn.execute(
                text("SELECT to_regprocedure('public.deactivate_break_glass(uuid, uuid, boolean)') IS NOT NULL")
            )
        ).scalar_one()
        assert func_exists is True
