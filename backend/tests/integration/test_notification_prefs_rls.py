"""FAR-247: notification_preferences RLS — FORCE, org isolation, per-user writes.

Real Postgres (testcontainer) with the full migration chain applied, so the
policies are the ones migration 0115 actually created. Enforcement runs as a
NON-superuser role (``app_engine`` SET ROLEs to ``modulo_integration_app``) —
testcontainers connect as the DB superuser, which bypasses RLS even under
``FORCE ROW LEVEL SECURITY``, so a superuser session proves nothing about
enforcement (only about the ``relforcerowsecurity`` flag, asserted directly).

Covers the ticket's three RLS requirements:

1. FORCE RLS is ON (the migration owns it — the integration conftest's FORCE
   list does not include this table) and the policy split is present:
   org-scope SELECT, per-user INSERT/UPDATE/DELETE on ``app.user_id``.
2. Two-org isolation — a prefs row in org A is invisible to (and unwritable
   from) org B's context.
3. Same-org two-user isolation — user B cannot INSERT / UPDATE / DELETE user
   A's rows (WITH CHECK / USING keyed on ``app.user_id``); user A can still
   manage their own (positive control).
"""

import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.db.crud.notifications import get_opted_out_categories
from modulo.db.models.notification import NotificationPreference
from modulo.db.rls import set_rls_org, set_rls_user_context

pytestmark = pytest.mark.integration


async def _seed_tenant(
    db_engine: AsyncEngine,
    org_label: str,
    account_ids: list[uuid.UUID],
) -> uuid.UUID:
    """Commit an organisation + accounts as superuser (bypasses RLS)."""
    org_id = uuid.uuid4()
    slug = f"notif-prefs-{org_label}-{org_id.hex[:8]}"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"NotifPrefs {org_label}", "slug": slug},
        )
        for idx, account_id in enumerate(account_ids):
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                    "VALUES (:id, :email, :name, 'local', true, 'hash')"
                ),
                {
                    "id": str(account_id),
                    "email": f"{slug}-{idx}@example.com",
                    "name": f"{slug}-{idx}",
                },
            )
    return org_id


async def _seed_pref_row(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID, category: str) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO notification_preferences (id, organisation_id, account_id, category) "
                "VALUES (:id, :oid, :aid, :cat)"
            ),
            {"id": str(uuid.uuid4()), "oid": str(org_id), "aid": str(account_id), "cat": category},
        )


async def _cleanup_tenant(db_engine: AsyncEngine, org_id: uuid.UUID, account_ids: list[uuid.UUID]) -> None:
    async with db_engine.connect() as conn, conn.begin():
        for account_id in account_ids:
            await conn.execute(
                text("DELETE FROM notification_preferences WHERE account_id = :aid"),
                {"aid": str(account_id)},
            )
            await conn.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(account_id)})
        await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(org_id)})


def _app_session_factory(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, expire_on_commit=False)


async def test_notification_preferences_forces_rls_and_splits_policies(db_engine: AsyncEngine) -> None:
    """FORCE RLS is ON and the 0066-style policy split exists (0115 migration).

    ``relforcerowsecurity`` matters because the table's OWNER is
    ``modulo_migrate`` — the owner bypasses ENABLE-only RLS, and the notifier's
    org-only context must still be confined by the SELECT policy.
    """
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = to_regclass('public.notification_preferences')"
                )
            )
        ).one()
        assert bool(row.relrowsecurity), "ENABLE ROW LEVEL SECURITY missing"
        assert bool(row.relforcerowsecurity), "FORCE ROW LEVEL SECURITY missing — owner would bypass RLS"

        policies = {
            (name, cmd): (qual or "", with_check or "")
            for name, cmd, qual, with_check in (
                await conn.execute(
                    text(
                        "SELECT policyname, cmd, qual, with_check FROM pg_policies "
                        "WHERE tablename = 'notification_preferences'"
                    )
                )
            ).fetchall()
        }
        assert ("rls_org_isolation", "SELECT") in policies, f"org-scope SELECT policy missing: {sorted(policies)}"
        assert ("rls_user_isolation_insert", "INSERT") in policies
        assert ("rls_user_isolation_update", "UPDATE") in policies
        assert ("rls_user_isolation_delete", "DELETE") in policies

        for (name, _cmd), (qual, with_check) in policies.items():
            body = qual + with_check
            assert "app.organisation_id" in body, f"{name} lost the org scope"
            # app.account_id is never set — only app.user_id may key the user scope.
            assert "app.account_id" not in body, f"{name} references app.account_id (never set)"
        # The WRITE policies are per-user (app.user_id); the SELECT policy is
        # deliberately org-scope only so the notifier's org-only context can read.
        for name in ("rls_user_isolation_insert", "rls_user_isolation_update", "rls_user_isolation_delete"):
            _qual, with_check = policies[(name, name.rsplit("_", 1)[-1].upper())]
            body = _qual + with_check
            assert "app.user_id" in body, f"{name} lost the app.user_id user scope"

        grants = (
            await conn.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = 'modulo_app' AND table_name = 'notification_preferences'"
                )
            )
        ).fetchall()
        assert {g[0] for g in grants} >= {"SELECT", "INSERT", "UPDATE", "DELETE"}


async def test_two_org_isolation_read_and_write(db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """A prefs row in org A is invisible to org B's context and unwritable from it."""
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    org_a = await _seed_tenant(db_engine, "org-a", [user_a])
    org_b = await _seed_tenant(db_engine, "org-b", [user_b])
    try:
        await _seed_pref_row(db_engine, org_a, user_a, "run.failed")
        await _seed_pref_row(db_engine, org_b, user_b, "eval.blocked")

        factory = _app_session_factory(app_engine)

        # Org A context (user A): only org A's row.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            await set_rls_user_context(session, user_a, "admin")
            cats = await get_opted_out_categories(session, org_id=org_a, account_id=user_a)
        assert cats == {"run.failed"}, f"org A context leaked foreign rows: {cats}"

        # Org B context (user B): only org B's row.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            await set_rls_user_context(session, user_b, "admin")
            cats = await get_opted_out_categories(session, org_id=org_b, account_id=user_b)
        assert cats == {"eval.blocked"}, f"org B context leaked foreign rows: {cats}"

        # Cross-org WRITE: org A context attempting an org B row violates the
        # policy's organisation check (42501), never a silent cross-tenant insert.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            await set_rls_user_context(session, user_a, "admin")
            session.add(NotificationPreference(organisation_id=org_b, account_id=user_a, category="run.stalled"))
            with pytest.raises(ProgrammingError) as excinfo:
                await session.flush()
        assert excinfo.value.orig.sqlstate == "42501", (
            f"expected RLS 42501 on cross-org insert, got {excinfo.value.orig!r}"
        )
    finally:
        await _cleanup_tenant(db_engine, org_a, [user_a])
        await _cleanup_tenant(db_engine, org_b, [user_b])


async def test_same_org_two_user_isolation(db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """User B cannot INSERT / UPDATE / DELETE user A's prefs rows in one org.

    The per-user WITH CHECK / USING clauses key on ``app.user_id`` (the value
    ``set_rls_user_context`` writes). Reads stay org-scoped BY DESIGN (the
    notifier's org-only context must read opt-outs) — the per-user read
    scoping is the CRUD's ``account_id`` predicate, asserted via
    ``get_opted_out_categories`` below.
    """
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    org_id = await _seed_tenant(db_engine, "two-user", [user_a, user_b])
    try:
        await _seed_pref_row(db_engine, org_id, user_a, "run.failed")
        factory = _app_session_factory(app_engine)

        # B cannot INSERT a row FOR A's account (WITH CHECK fails).
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, user_b, "admin")
            session.add(NotificationPreference(organisation_id=org_id, account_id=user_a, category="run.stalled"))
            with pytest.raises(ProgrammingError) as excinfo:
                await session.flush()
        assert excinfo.value.orig.sqlstate == "42501"

        # B cannot DELETE A's row — the USING clause filters it out: 0 rows.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, user_b, "admin")
            result = await session.execute(
                delete(NotificationPreference).where(
                    NotificationPreference.organisation_id == org_id,
                    NotificationPreference.account_id == user_a,
                    NotificationPreference.category == "run.failed",
                )
            )
            assert result.rowcount == 0, f"user B deleted {result.rowcount} of user A's rows"

        # B cannot UPDATE A's row either.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, user_b, "admin")
            result = await session.execute(
                update(NotificationPreference)
                .where(
                    NotificationPreference.organisation_id == org_id,
                    NotificationPreference.account_id == user_a,
                )
                .values(category="hijacked")
            )
            assert result.rowcount == 0

        # Row survived both attempts (superuser sees reality).
        async with db_engine.connect() as conn:
            surviving = (
                await conn.execute(
                    text("SELECT category FROM notification_preferences WHERE account_id = :aid"),
                    {"aid": str(user_a)},
                )
            ).fetchall()
        assert [s[0] for s in surviving] == ["run.failed"]

        # Read scoping: B's opt-out query never reports A's rows.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, user_b, "admin")
            cats_b = await get_opted_out_categories(session, org_id=org_id, account_id=user_b)
        assert cats_b == set()

        # Positive control: A can update and delete their OWN row.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, user_a, "admin")
            result = await session.execute(
                delete(NotificationPreference).where(
                    NotificationPreference.organisation_id == org_id,
                    NotificationPreference.account_id == user_a,
                    NotificationPreference.category == "run.failed",
                )
            )
            assert result.rowcount == 1, "user A could not delete their own row — policy too strict"
    finally:
        await _cleanup_tenant(db_engine, org_id, [user_a, user_b])


async def test_empty_org_context_reads_nothing(db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """No org GUC set -> the strict org-scope policy hides every row (fail-closed)."""
    user_a = uuid.uuid4()
    org_id = await _seed_tenant(db_engine, "empty-ctx", [user_a])
    try:
        await _seed_pref_row(db_engine, org_id, user_a, "run.failed")
        factory = _app_session_factory(app_engine)
        async with factory() as session, session.begin():
            # Deliberately NO set_rls_org: app.organisation_id is empty.
            result = await session.execute(select(NotificationPreference.category))
            assert result.scalars().all() == [], "empty org context leaked prefs rows"
    finally:
        await _cleanup_tenant(db_engine, org_id, [user_a])
