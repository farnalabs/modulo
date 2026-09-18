"""Shared Postgres RLS ceremony helpers for migrations (FAR-761 DRY).

These helpers used to be copy-pasted verbatim into every RLS migration version
file (0131_eval_dataset_corpus, 0192_run_node_outputs, 0207_collection_install,
...). SonarCloud flags that copy-paste as new-code duplication on every RLS
migration PR, so they now live here once and are imported by the migrations that
need them. Behaviour is identical to the previous per-file copies.
"""

from __future__ import annotations

import sqlalchemy as sa

_MIGRATE_ROLE = "modulo_migrate"


def is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def role_exists(bind: sa.Connection, role: str) -> bool:
    """Return True when the Postgres role exists (fresh dev/BDD DBs have none)."""
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    """POST-CREATE ownership assertion — the 0066 ceremony (before RLS).

    The app role must NOT own an RLS-FORCED org-scoped table, or it bypasses
    its own RLS.
    """
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl=f"public.{table}"
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"{table} owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own an RLS-FORCED org-scoped table — owner bypasses RLS)"
        )
