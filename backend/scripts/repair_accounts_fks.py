"""Repair tool for a corrupt ``accounts`` table catalog (2026-08-04 incident).

Symptom
-------
Every FOREIGN KEY referencing ``accounts`` failed with
``permission denied for schema public`` for EVERY role -- including the
``postgres`` superuser -- while FKs to other tables (e.g. ``organisations``)
kept working. Login and most inserts broke.

What does NOT fix it
--------------------
REINDEX TABLE accounts, VACUUM (FULL/ANALYZE) accounts, dropping/re-adding the
FK constraint, and disabling only the tenant trigger all left the corruption
in place.

Root cause
----------
The table's catalog state was corrupt after the DB was recreated. The
pg_class/pg_namespace entry for ``accounts`` pointed at an unusable namespace,
so every FK check that had to resolve the referenced table failed with a
schema-permission error regardless of the connecting role.

Definitive fix
--------------
Rebuild the table so it gets a fresh catalog entry:
  1. CREATE TABLE accounts_new (LIKE accounts INCLUDING ALL)
  2. INSERT INTO accounts_new SELECT * FROM accounts
  3. DROP TABLE accounts
  4. ALTER TABLE accounts_new RENAME TO accounts
  5. re-apply grants + ownership, then re-add all 46 FK constraints.

This script is the version-controlled, repeatable form of that manual repair.
It is a DOCUMENTED repair tool -- it is never run automatically.

Usage
-----
    DATABASE_URL=postgresql+asyncpg://... python repair_accounts_fks.py check
    DATABASE_URL=... python repair_accounts_fks.py rebuild-accounts
    DATABASE_URL=... python repair_accounts_fks.py add-fks
    DATABASE_URL=... python repair_accounts_fks.py repair
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg  # type: ignore[import-untyped]  # asyncpg does not publish a py.typed marker

# (child_table, constraint_name, fk_column, on_delete_action) -- snapshot of
# the 46 FKs referencing public.accounts (prod, 2026-08-04). Keep in sync with
# the migrations when the schema changes.
ACCOUNTS_FKS: tuple[tuple[str, str, str, str], ...] = (
    ("agents", "agents_account_id_fkey", "account_id", "RESTRICT"),
    ("audit_events", "audit_events_account_id_fkey", "account_id", "SET NULL"),
    ("chat_sessions", "chat_sessions_user_id_fkey", "user_id", "CASCADE"),
    ("composite_templates", "composite_templates_account_id_fkey", "account_id", "RESTRICT"),
    ("connector_instances", "connector_instances_account_id_fkey", "account_id", "RESTRICT"),
    ("dismissals", "dismissals_dismissed_by_user_id_fkey", "dismissed_by_user_id", "CASCADE"),
    ("environment_profiles", "environment_profiles_account_id_fkey", "account_id", "RESTRICT"),
    ("error_groups", "error_groups_assigned_to_fkey", "assigned_to", "SET NULL"),
    ("eval_definitions", "eval_definitions_account_id_fkey", "account_id", "RESTRICT"),
    ("feedback_records", "feedback_records_account_id_fkey", "account_id", "RESTRICT"),
    ("hitl_claims", "hitl_claims_account_id_fkey", "account_id", "SET NULL"),
    ("library_primitives", "library_primitives_account_id_fkey", "account_id", "SET NULL"),
    ("lifecycle_maps", "lifecycle_maps_account_id_fkey", "account_id", "RESTRICT"),
    ("mcp_setup_tokens", "fk_mcp_setup_tokens_created_by", "created_by", "RESTRICT"),
    ("model_backends", "model_backends_account_id_fkey", "account_id", "RESTRICT"),
    ("node_categories", "node_categories_account_id_fkey", "account_id", "RESTRICT"),
    ("node_observations", "node_observations_account_id_fkey", "account_id", "SET NULL"),
    ("nodes", "nodes_account_id_fkey", "account_id", "RESTRICT"),
    ("notification_endpoints", "notification_endpoints_account_id_fkey", "account_id", "SET NULL"),
    ("notifications", "notifications_target_user_id_fkey", "target_user_id", "SET NULL"),
    ("oauth_authorization_codes", "fk_oauth_authorization_codes_account_id", "account_id", "CASCADE"),
    ("oauth_clients", "oauth_clients_account_id_fkey", "account_id", "SET NULL"),
    ("oauth_consent_states", "oauth_consent_states_account_id_fkey", "account_id", "SET NULL"),
    ("org_api_keys", "org_api_keys_account_id_fkey", "account_id", "RESTRICT"),
    ("org_memberships", "org_memberships_account_id_fkey", "account_id", "CASCADE"),
    ("parameter_schemas", "parameter_schemas_account_id_fkey", "account_id", "RESTRICT"),
    ("parameter_sets", "parameter_sets_account_id_fkey", "account_id", "RESTRICT"),
    ("pipeline_folders", "pipeline_folders_account_id_fkey", "account_id", "RESTRICT"),
    ("pipelines", "pipelines_account_id_fkey", "account_id", "RESTRICT"),
    ("pipeline_snapshots", "pipeline_snapshots_account_id_fkey", "account_id", "SET NULL"),
    ("primitive_abuse_reports", "primitive_abuse_reports_reviewer_account_id_fkey", "reviewer_account_id", "SET NULL"),
    ("primitive_abuse_reports", "primitive_abuse_reports_reporter_account_id_fkey", "reporter_account_id", "SET NULL"),
    ("primitive_ratings", "primitive_ratings_account_id_fkey", "account_id", "SET NULL"),
    ("remy_context_sources", "remy_context_sources_user_id_fkey", "user_id", "CASCADE"),
    ("remy_skills", "remy_skills_user_id_fkey", "user_id", "CASCADE"),
    ("runs", "runs_account_id_fkey", "account_id", "SET NULL"),
    ("saved_views", "saved_views_account_id_fkey", "account_id", "RESTRICT"),
    ("scheduled_reports", "scheduled_reports_created_by_fkey", "created_by", "SET NULL"),
    ("schemas", "schemas_account_id_fkey", "account_id", "RESTRICT"),
    ("schema_versions", "schema_versions_account_id_fkey", "account_id", "RESTRICT"),
    ("stages", "stages_account_id_fkey", "account_id", "RESTRICT"),
    ("system_config", "fk_system_config_updated_by", "updated_by", "SET NULL"),
    ("team_memberships", "team_memberships_account_id_fkey", "account_id", "CASCADE"),
    ("teams", "teams_account_id_fkey", "account_id", "RESTRICT"),
    ("token_families", "token_families_account_id_fkey", "account_id", "CASCADE"),
    ("triggers", "triggers_account_id_fkey", "account_id", "RESTRICT"),
)

# Grants re-applied after a rebuild, mirroring the app's bootstrap posture for
# accounts (see db/bootstrap_role.py -- modulo_app may only UPDATE the
# allow-listed columns; modulo_breakglass is SELECT+INSERT only).
_ACCOUNTS_GRANTS: tuple[str, ...] = (
    'GRANT SELECT, INSERT, DELETE ON public.accounts TO "modulo_app"',
    'REVOKE UPDATE ON public.accounts FROM "modulo_app"',
    "REVOKE UPDATE ON public.accounts FROM PUBLIC",
    "GRANT UPDATE (email, display_name, password_hash, active, auth_provider, "
    "sso_subject, preferences, last_login, is_system_admin, updated_at) "
    'ON public.accounts TO "modulo_app"',
    'GRANT SELECT, INSERT ON public.accounts TO "modulo_breakglass"',
    'ALTER TABLE public.accounts OWNER TO "modulo_migrate"',
)

_ON_DELETE_ACTIONS = frozenset({"RESTRICT", "CASCADE", "SET NULL", "NO ACTION", "SET DEFAULT"})


def _resolve_db_url(raw: str) -> str:
    """Convert a SQLAlchemy/asyncpg-style URL into an asyncpg connection string."""
    url = raw
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix) :]
            break
    # asyncpg does not understand sslmode in the query string -- strip it
    # (mirrors settings.py). SSL mode is negotiated by asyncpg by default.
    if "?" in url:
        base, _, query = url.partition("?")
        kept = [kv for kv in query.split("&") if kv and not kv.startswith("sslmode=")]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url


def _q(ident: str) -> str:
    """Double-quote a SQL identifier, escaping any embedded quotes."""
    return '"' + ident.replace('"', '""') + '"'


async def _fk_exists(conn: asyncpg.Connection, child: str, constraint: str) -> bool:
    """Return True when the FK constraint exists on the given child table."""
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class rel ON rel.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = rel.relnamespace "
            "WHERE c.conname = $1 AND c.contype = 'f' "
            "AND n.nspname = 'public' AND rel.relname = $2",
            constraint,
            child,
        )
    )


async def _accounts_fk_names(conn: asyncpg.Connection) -> set[str]:
    """Return the names of every FK in the DB that references public.accounts."""
    oid = await conn.fetchval("SELECT to_regclass('public.accounts')")
    if oid is None:
        return set()
    rows = await conn.fetch(
        "SELECT c.conname FROM pg_constraint c WHERE c.contype = 'f' AND c.confrelid = $1",
        oid,
    )
    return {row["conname"] for row in rows}


async def _run_fk_probe(conn: asyncpg.Connection) -> tuple[bool, str]:
    """Scratch-table FK probe against public.accounts.

    Creates a TEMP table with an FK to accounts, then exercises the FK check:
    with rows present a valid account id must be accepted; with an empty
    accounts table a missing id must be rejected with an FK violation. On the
    corrupt catalog every FK operation instead fails with ``permission denied
    for schema public`` regardless of the role -- that is the corruption
    signature this probe detects.
    """
    try:
        await conn.execute(
            "CREATE TEMP TABLE _fk_probe (id uuid PRIMARY KEY, account_id uuid REFERENCES public.accounts (id))"
        )
    except Exception as exc:
        return False, f"FK probe FAILED at CREATE (corrupt catalog signature): {exc}"
    try:
        account_id = await conn.fetchval("SELECT id FROM public.accounts LIMIT 1")
        if account_id is not None:
            await conn.execute(
                "INSERT INTO _fk_probe (id, account_id) VALUES (gen_random_uuid(), $1)",
                account_id,
            )
            return True, "FK probe passed (valid account id accepted by the FK check)"
        try:
            await conn.execute("INSERT INTO _fk_probe (id, account_id) VALUES (gen_random_uuid(), gen_random_uuid())")
        except asyncpg.exceptions.ForeignKeyViolationError:
            return True, "FK probe passed (empty accounts: FK violation correctly raised)"
        return False, "FK probe FAILED: missing account id did not raise an FK violation"
    except Exception as exc:
        return False, f"FK probe FAILED at INSERT (corrupt catalog signature): {exc}"
    finally:
        await conn.execute("DROP TABLE IF EXISTS _fk_probe")


async def _cmd_check(conn: asyncpg.Connection) -> int:
    """Report FK presence + run the corruption-signature probe. 0 = healthy."""
    existing: list[str] = []
    missing: list[str] = []
    for child, constraint, _fk_column, _on_delete in ACCOUNTS_FKS:
        if await _fk_exists(conn, child, constraint):
            existing.append(f"{child}.{constraint}")
        else:
            missing.append(f"{child}.{constraint}")
    print(f"FKs referencing accounts: {len(existing)}/{len(ACCOUNTS_FKS)} present")
    if missing:
        print("Missing:")
        for name in missing:
            print(f"  - {name}")
    known = {constraint for _child, constraint, _fk_column, _on_delete in ACCOUNTS_FKS}
    unexpected = sorted(await _accounts_fk_names(conn) - known)
    if unexpected:
        print("FKs referencing accounts NOT in ACCOUNTS_FKS (drift):")
        for name in unexpected:
            print(f"  - {name}")
    probe_ok, probe_msg = await _run_fk_probe(conn)
    print(probe_msg)
    if not missing and probe_ok:
        print("RESULT: accounts catalog looks healthy (all 46 FKs present, probe passed).")
        return 0
    print("RESULT: repair needed.")
    return 1


async def _apply_grants(conn: asyncpg.Connection) -> None:
    """Re-apply the accounts grants + ownership block after a rebuild."""
    for statement in _ACCOUNTS_GRANTS:
        await conn.execute(statement)


async def _cmd_rebuild(conn: asyncpg.Connection) -> int:
    """Transactional rebuild of accounts with a fresh catalog entry."""
    print("WARNING: rebuild-accounts DROPs and recreates the accounts table.")
    print("It is destructive. Run 'check' first, ensure all 46 FKs are absent,")
    print("and take a fresh backup before proceeding.")
    if await conn.fetchval("SELECT to_regclass('public.accounts')") is None:
        print("ERROR: public.accounts does not exist - nothing to rebuild.")
        return 1
    existing = [
        f"{child}.{constraint}"
        for child, constraint, _fk_column, _on_delete in ACCOUNTS_FKS
        if await _fk_exists(conn, child, constraint)
    ]
    if existing:
        print("ERROR: FKs referencing accounts still exist - drop them before rebuilding:")
        for name in existing:
            print(f"  - {name}")
        return 1
    unexpected = sorted(await _accounts_fk_names(conn))
    if unexpected:
        print("ERROR: FKs referencing accounts not covered by ACCOUNTS_FKS exist:")
        for name in unexpected:
            print(f"  - {name}")
        return 1
    if await conn.fetchval("SELECT to_regclass('public.accounts_new')") is not None:
        print("ERROR: a leftover public.accounts_new exists (a previous rebuild may have failed).")
        print("Drop it first, then re-run.")
        return 1
    before = await conn.fetchval("SELECT count(*) FROM public.accounts")
    async with conn.transaction():
        await conn.execute("CREATE TABLE public.accounts_new (LIKE public.accounts INCLUDING ALL)")
        await conn.execute("INSERT INTO public.accounts_new SELECT * FROM public.accounts")
        await conn.execute("DROP TABLE public.accounts")
        await conn.execute("ALTER TABLE public.accounts_new RENAME TO public.accounts")
        await _apply_grants(conn)
    after = await conn.fetchval("SELECT count(*) FROM public.accounts")
    print(f"Rebuild complete: {before} rows copied, {after} rows in the rebuilt table.")
    print("Next step: run 'add-fks' to restore the 46 FK constraints.")
    return 0


async def _cmd_add_fks(conn: asyncpg.Connection) -> int:
    """Add any of the 46 FKs that are missing, skipping orphaned children."""
    added: list[str] = []
    already: list[str] = []
    skipped_orphans: list[str] = []
    skipped_missing: list[str] = []
    for child, constraint, fk_column, on_delete in ACCOUNTS_FKS:
        if on_delete not in _ON_DELETE_ACTIONS:
            skipped_missing.append(f"{child}.{constraint}: unknown ON DELETE action {on_delete!r}")
            continue
        if await _fk_exists(conn, child, constraint):
            already.append(f"{child}.{constraint}")
            continue
        if await conn.fetchval("SELECT to_regclass($1)", f"public.{child}") is None:
            skipped_missing.append(f"{child}.{constraint}: child table missing")
            continue
        if not await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2",
            child,
            fk_column,
        ):
            skipped_missing.append(f"{child}.{constraint}: column {fk_column} missing")
            continue
        # Orphan check - never auto-delete data; skip and report instead.
        orphan_sql = (
            f"SELECT count(*) FROM {_q(child)} c "  # noqa: S608 - identifiers come from the hardcoded ACCOUNTS_FKS allowlist
            f"LEFT JOIN public.accounts a ON a.id = c.{_q(fk_column)} "
            f"WHERE c.{_q(fk_column)} IS NOT NULL AND a.id IS NULL"
        )
        orphans = await conn.fetchval(orphan_sql)
        if orphans:
            skipped_orphans.append(
                f"{child}.{constraint}: {orphans} orphaned row(s) in {fk_column} - resolve before adding"
            )
            continue
        await conn.execute(
            f"ALTER TABLE {_q(child)} ADD CONSTRAINT {_q(constraint)} "
            f"FOREIGN KEY ({_q(fk_column)}) REFERENCES public.accounts (id) ON DELETE {on_delete}"
        )
        added.append(f"{child}.{constraint}")
    print(f"Added {len(added)} FK(s), {len(already)} already present.")
    for name in added:
        print(f"  + {name}")
    for name in skipped_orphans:
        print(f"  ! skipped (orphans): {name}")
    for name in skipped_missing:
        print(f"  ! skipped (missing/invalid): {name}")
    if skipped_orphans or skipped_missing:
        print("RESULT: complete with skips - resolve the orphaned rows / missing objects manually.")
        return 1
    print("RESULT: all FKs present.")
    return 0


async def _cmd_repair(conn: asyncpg.Connection) -> int:
    """rebuild-accounts then add-fks, with the same guards."""
    rc = await _cmd_rebuild(conn)
    if rc != 0:
        return rc
    return await _cmd_add_fks(conn)


async def _dispatch(command: str, url: str) -> int:
    conn = await asyncpg.connect(url)
    try:
        if command == "check":
            return await _cmd_check(conn)
        if command == "rebuild-accounts":
            return await _cmd_rebuild(conn)
        if command == "add-fks":
            return await _cmd_add_fks(conn)
        if command == "repair":
            return await _cmd_repair(conn)
        print(f"ERROR: unknown command {command!r}", file=sys.stderr)
        return 1
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="repair_accounts_fks.py",
        description="Repair tool for the corrupt accounts table catalog (2026-08-04 incident).",
    )
    parser.add_argument(
        "command",
        choices=("check", "rebuild-accounts", "add-fks", "repair"),
        help="Which repair action to run (see module docstring for details).",
    )
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    try:
        rc = asyncio.run(_dispatch(args.command, _resolve_db_url(raw)))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
