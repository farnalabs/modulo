"""Rename the physical ``remy_*`` DB objects to ``assistant_*`` (FAR-1196 Tier 3).

Revision ID: 0257_rename_remy_to_assistant
Revises: 0256_pipeline_max_autonomy_level
Create Date: 2026-09-24

The SQLAlchemy models were renamed to ``AssistantSkill`` /
``AssistantContextSource`` (tables ``assistant_skills`` /
``assistant_context_sources``) in the same delivery; this migration renames the
physical objects the earlier migrations created under the ``remy_*`` names so
``compare_metadata`` in ``test_initial_migration`` sees parity:

- tables ``remy_skills`` -> ``assistant_skills``,
  ``remy_context_sources`` -> ``assistant_context_sources``
- PK / CHECK / UNIQUE / FK constraints (``remy_*`` -> ``assistant_*``)
- lookup indexes ``ix_remy_*`` -> ``ix_assistant_*``
- tenant triggers ``trg_remy_*_account_id_tenant`` ->
  ``trg_assistant_*_account_id_tenant``

RLS: ``ALTER TABLE ... RENAME`` carries ``ENABLE ROW LEVEL SECURITY`` and the
``rls_org_isolation`` policy across (the policy name is generic and
table-scoped, so it survives the rename attached to the same table OID).
This migration re-asserts both idempotently anyway — ENABLE is a no-op when
already on, and the CREATE POLICY is guarded by a pg_policies existence
check. ``public.enforce_same_organisation(...)`` is parameterised and needs
no change; the renamed tenant triggers keep executing it.

DATA (not covered by any table rename): ``system_config`` keys written under
the old ``remy_config:`` prefix are rewritten to ``assistant_config:`` — the
code's ``_CONFIG_KEY_PREFIX`` changed to ``assistant_config:`` in the same
delivery, so any persisted per-org Remy config row would otherwise become
unreachable. The UPDATE is idempotent (after the first run no row matches
``LIKE 'remy_config:%'``) and guarded for table/column existence.

Ticket note: the ticket also lists ``ck_remy_skills_mode`` — no such
constraint exists in any shipped migration or in the pre-rename model (the
skills table has never had a mode CHECK; only ``remy_context_sources`` has
one), so there is nothing to rename and no target name to invent.

Every rename is guarded by catalogue existence checks (``information_schema``
for tables/columns, ``pg_constraint``/``pg_class``/``pg_trigger`` for the
rest) so a partially applied run is safe to re-execute: each statement fires
only when the OLD name exists and the NEW name does not. ``downgrade``
mirrors every rename and the data rewrite.

Non-Postgres (SQLite) parity branch: table renames and the data rewrite are
portable; constraint/index/trigger names are Postgres-catalogue concepts and
SQLite's test schema is built from the ORM model anyway (same stance as
migration 0136).
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision: str = "0257_rename_remy_to_assistant"
down_revision: str | None = "0256_pipeline_max_autonomy_level"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# FAR-915: every interpolated DDL identifier passes through this guard before
# it reaches an f-string SQL statement (``op.execute`` cannot bind params).
_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _validate_identifier(name: str) -> str:
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


# (old table, new table)
_TABLES: tuple[tuple[str, str], ...] = (
    ("remy_skills", "assistant_skills"),
    ("remy_context_sources", "assistant_context_sources"),
)

# (table the constraint lives on AFTER the upgrade's table rename, old, new)
_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("assistant_skills", "remy_skills_pkey", "assistant_skills_pkey"),
    ("assistant_skills", "ck_remy_skills_owner", "ck_assistant_skills_owner"),
    ("assistant_skills", "remy_skills_organisation_id_fkey", "assistant_skills_organisation_id_fkey"),
    ("assistant_skills", "remy_skills_account_id_fkey", "assistant_skills_account_id_fkey"),
    ("assistant_context_sources", "remy_context_sources_pkey", "assistant_context_sources_pkey"),
    ("assistant_context_sources", "ck_remy_context_sources_owner", "ck_assistant_context_sources_owner"),
    ("assistant_context_sources", "ck_remy_context_sources_mode", "ck_assistant_context_sources_mode"),
    ("assistant_context_sources", "uq_remy_context_sources_key", "uq_assistant_context_sources_key"),
    (
        "assistant_context_sources",
        "remy_context_sources_organisation_id_fkey",
        "assistant_context_sources_organisation_id_fkey",
    ),
    (
        "assistant_context_sources",
        "remy_context_sources_account_id_fkey",
        "assistant_context_sources_account_id_fkey",
    ),
)

# (old index, new index). The last three are the backing indexes of the PK /
# UNIQUE constraints above — PostgreSQL keeps them in sync on RENAME
# CONSTRAINT in some paths and not others; these guarded renames are a no-op
# when RENAME CONSTRAINT already moved them and repair the name when it did
# not.
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_remy_skills_organisation_id", "ix_assistant_skills_organisation_id"),
    ("ix_remy_skills_account_id", "ix_assistant_skills_account_id"),
    ("ix_remy_context_sources_organisation_id", "ix_assistant_context_sources_organisation_id"),
    ("remy_skills_pkey", "assistant_skills_pkey"),
    ("remy_context_sources_pkey", "assistant_context_sources_pkey"),
    ("uq_remy_context_sources_key", "uq_assistant_context_sources_key"),
)

# (table the trigger lives on AFTER the upgrade's table rename, old, new).
# The pre-0136 ``*_user_id_tenant`` variants were already renamed by 0136;
# at the 0255 head only the ``*_account_id_tenant`` names exist.
_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("assistant_skills", "trg_remy_skills_account_id_tenant", "trg_assistant_skills_account_id_tenant"),
    (
        "assistant_context_sources",
        "trg_remy_context_sources_account_id_tenant",
        "trg_assistant_context_sources_account_id_tenant",
    ),
)

_RLS_POLICY = "rls_org_isolation"
_RLS_TABLES: tuple[str, ...] = ("assistant_skills", "assistant_context_sources")

# Data rewrite (system_config key prefix). The bare statements are portable
# (Postgres AND SQLite both implement replace()); the *_PG wrappers add the
# information_schema table/column guard inside a DO block.
_CONFIG_UPDATE_FORWARD = (
    "UPDATE system_config SET key = replace(key, 'remy_config:', 'assistant_config:') WHERE key LIKE 'remy_config:%'"
)
_CONFIG_UPDATE_REVERSE = "UPDATE system_config SET key = replace(key, 'assistant_config:', 'remy_config:') WHERE key LIKE 'assistant_config:%'"
_CONFIG_FORWARD_PG = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='system_config') "
    "AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='key') "
    "THEN UPDATE system_config SET key = replace(key, 'remy_config:', 'assistant_config:') WHERE key LIKE 'remy_config:%'; END IF; END $$;"
)
_CONFIG_REVERSE_PG = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='system_config') "
    "AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='key') "
    "THEN UPDATE system_config SET key = replace(key, 'assistant_config:', 'remy_config:') WHERE key LIKE 'assistant_config:%'; END IF; END $$;"
)


def _rename_table_sql(old: str, new: str) -> str:
    old, new = _validate_identifier(old), _validate_identifier(new)
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='{old}') "  # noqa: S608  # nosec B608 - interpolates module-constant identifiers only, never caller data
        f"AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='{new}') "
        f'THEN ALTER TABLE public."{old}" RENAME TO "{new}"; END IF; END $$;'
    )


def _rename_constraint_sql(table: str, old: str, new: str) -> str:
    table, old, new = _validate_identifier(table), _validate_identifier(old), _validate_identifier(new)
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "  # noqa: S608  # nosec B608 - interpolates module-constant identifiers only, never caller data
        f"JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='{table}' AND con.conname='{old}') "
        f"AND NOT EXISTS (SELECT 1 FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
        f"JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='{table}' AND con.conname='{new}') "
        f'THEN ALTER TABLE public."{table}" RENAME CONSTRAINT "{old}" TO "{new}"; END IF; END $$;'
    )


def _rename_index_sql(old: str, new: str) -> str:
    old, new = _validate_identifier(old), _validate_identifier(new)
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "  # noqa: S608  # nosec B608 - interpolates module-constant identifiers only, never caller data
        f"WHERE n.nspname='public' AND c.relname='{old}') "
        f"AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE n.nspname='public' AND c.relname='{new}') "
        f'THEN ALTER INDEX public."{old}" RENAME TO "{new}"; END IF; END $$;'
    )


def _rename_trigger_sql(table: str, old: str, new: str) -> str:
    # to_regclass returns NULL (no error) when the table is absent, so the
    # tgrelid comparisons are NULL-safe and the guarded ALTER never fires.
    table, old, new = _validate_identifier(table), _validate_identifier(old), _validate_identifier(new)
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_trigger tg WHERE tg.tgname='{old}' "  # noqa: S608  # nosec B608 - interpolates module-constant identifiers only, never caller data
        f"AND tg.tgrelid=to_regclass('public.{table}')) "
        f"AND NOT EXISTS (SELECT 1 FROM pg_trigger tg WHERE tg.tgname='{new}' "
        f"AND tg.tgrelid=to_regclass('public.{table}')) "
        f'THEN ALTER TRIGGER "{old}" ON public."{table}" RENAME TO "{new}"; END IF; END $$;'
    )


def _ensure_rls_sql(table: str) -> str:
    # Policy body verbatim from 0109_schema_teams_library (the creator).
    table = _validate_identifier(table)
    return (
        f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL THEN "  # noqa: S608  # nosec B608 - interpolates module-constant identifiers only, never caller data
        f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY; '
        f"IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='{table}' "
        f"AND policyname='{_RLS_POLICY}') THEN "
        f'CREATE POLICY {_RLS_POLICY} ON public."{table}" '
        f"USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) "
        f"OR (organisation_id IS NULL))); "
        f"END IF; END IF; END $$;"
    )


def _upgrade_postgres() -> None:
    for old, new in _TABLES:
        op.execute(_rename_table_sql(old, new))
    # Constraints first: the table rename above keeps the old constraint
    # names attached to the renamed table, so they are renamed in place.
    for table, old, new in _CONSTRAINTS:
        op.execute(_rename_constraint_sql(table, old, new))
    for old, new in _INDEXES:
        op.execute(_rename_index_sql(old, new))
    for table, old, new in _TRIGGERS:
        op.execute(_rename_trigger_sql(table, old, new))
    for table in _RLS_TABLES:
        op.execute(_ensure_rls_sql(table))


def _rename_table_portable_sql(old: str, new: str) -> str:
    """Un-guarded (portable) table rename for the non-Postgres branch.

    Built through a helper so no direct ``op.execute(f"...")`` appears in this
    migration (the FAR-915 semgrep rule) and with the identifier guard applied.
    """
    return f'ALTER TABLE "{_validate_identifier(old)}" RENAME TO "{_validate_identifier(new)}"'


def _upgrade_other() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    for old, new in _TABLES:
        if old in existing and new not in existing:
            op.execute(_rename_table_portable_sql(old, new))


def _migrate_config_keys(*, forward: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(_CONFIG_FORWARD_PG if forward else _CONFIG_REVERSE_PG)
        return
    inspector = sa.inspect(bind)
    config_columns = (
        {column["name"] for column in inspector.get_columns("system_config")}
        if inspector.has_table("system_config")
        else set()
    )
    if "key" in config_columns:
        op.execute(_CONFIG_UPDATE_FORWARD if forward else _CONFIG_UPDATE_REVERSE)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgres()
    else:
        _upgrade_other()
    _migrate_config_keys(forward=True)


def downgrade() -> None:
    _migrate_config_keys(forward=False)
    if op.get_bind().dialect.name == "postgresql":
        # Reverse order: triggers/indexes/constraints while the tables still
        # carry their assistant_* names, then the tables themselves.
        for table, old, new in _TRIGGERS:
            op.execute(_rename_trigger_sql(table, new, old))
        for old, new in _INDEXES:
            op.execute(_rename_index_sql(new, old))
        for table, old, new in _CONSTRAINTS:
            op.execute(_rename_constraint_sql(table, new, old))
        for old, new in _TABLES:
            op.execute(_rename_table_sql(new, old))
        for old_table, _new_table in _TABLES:
            op.execute(_ensure_rls_sql(old_table))
    else:
        inspector = sa.inspect(op.get_bind())
        existing = set(inspector.get_table_names())
        for old, new in _TABLES:
            if new in existing and old not in existing:
                op.execute(_rename_table_portable_sql(new, old))
