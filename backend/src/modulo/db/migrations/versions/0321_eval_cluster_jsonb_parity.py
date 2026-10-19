"""Promote remaining eval-cluster JSON columns to JSONB.

Revision ID: 0321_eval_cluster_jsonb_parity
Revises: 0320_evidence_redaction_rules
Create Date: 2026-10-19

0147 standardized the codebase on ``jsonb`` but predates the ``suite_runs``
table (0137) and missed the version-snapshot columns added by 0138/0250/0254.
Those columns remain plain ``json`` while every sibling payload column in the
eval cluster (``evals.config_json``, ``eval_cases.input_payload``,
``eval_definitions.config_json``) is ``jsonb``. This migration closes the gap so
containment/path operators work consistently and the ORM declaration matches the
physical Postgres type.

Promoted columns (all nullable, no defaults):

* ``evals.pre_version_raw``
* ``eval_definitions.pre_version_raw``
* ``eval_suites.pre_version_raw``
* ``suite_runs.baseline_tuple``
* ``suite_runs.comparison_json``
* ``suite_runs.extra``

The conversion is additively performed in place, following 0147's pattern: add
a temporary ``jsonb`` column (metadata-only), batch-backfill it, then swap the
names. No ``ALTER COLUMN ... TYPE`` table rewrite is taken on ``suite_runs``,
which is written on every eval-suite execution. Every phase is idempotent so a
partially-applied run can be re-run safely.

Postgres-only: SQLite/MariaDB keep the ORM's generic ``JSON`` variant.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0321_eval_cluster_jsonb_parity"
down_revision: str | None = "0320_evidence_redaction_rules"
branch_labels: str | None = None
depends_on: str | None = None

_SCHEMA = "public"
_BATCH_SIZE = 1000
_TEMP_SUFFIX = "__jsonb"
_OLD_SUFFIX = "__jsonb_old"
_DOWNGRADE_TEMP_SUFFIX = "__json"
_DOWNGRADE_OLD_SUFFIX = "__json_old"

_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("evals", "pre_version_raw"),
    ("eval_definitions", "pre_version_raw"),
    ("eval_suites", "pre_version_raw"),
    ("suite_runs", "baseline_tuple"),
    ("suite_runs", "comparison_json"),
    ("suite_runs", "extra"),
)


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _quote_identifier(name: str) -> str:
    """Validate and quote an internal SQL identifier before interpolation."""
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return '"' + name + '"'


def _rename_column_named(table_name: str, column_name: str, new_name: str) -> str:
    return (
        "ALTER TABLE "
        + _SCHEMA
        + "."
        + _quote_identifier(table_name)
        + " RENAME COLUMN "
        + _quote_identifier(column_name)
        + " TO "
        + _quote_identifier(new_name)
    )


def _drop_column_named(table_name: str, column_name: str) -> str:
    return (
        "ALTER TABLE "
        + _SCHEMA
        + "."
        + _quote_identifier(table_name)
        + " DROP COLUMN "
        + _quote_identifier(column_name)
    )


def _column_exists(bind: sa.Connection, table_name: str, column_name: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
        ),
        {"schema": _SCHEMA, "table": table_name, "column": column_name},
    ).scalar_one_or_none()
    return row is not None


def _backfill(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    temp_name: str,
    target_type: sa.types.TypeEngine[object],
) -> None:
    """Backfill *temp_name* from *column_name* in bounded batches."""
    tbl = sa.table(
        table_name,
        sa.column("ctid"),
        sa.column(column_name),
        sa.column(temp_name),
        schema=_SCHEMA,
    )
    pending = tbl.c[temp_name].is_(None) & tbl.c[column_name].isnot(None)
    ctid_subq = sa.select(tbl.c.ctid).where(pending).limit(_BATCH_SIZE)
    stmt = (
        sa.update(tbl)
        .where(pending)
        .where(tbl.c.ctid.in_(ctid_subq))
        .values({temp_name: sa.cast(tbl.c[column_name], target_type)})
    )
    while True:
        result = bind.execute(stmt)
        if result.rowcount == 0:
            break


def _swap(bind: sa.Connection, table_name: str, column_name: str, temp_name: str, old_name: str) -> None:
    """Rename the original aside, promote the temp column, then drop the original."""
    if _column_exists(bind, table_name, old_name):
        return
    bind.execute(sa.text(_rename_column_named(table_name, column_name, old_name)))
    bind.execute(sa.text(_rename_column_named(table_name, temp_name, column_name)))
    bind.execute(sa.text(_drop_column_named(table_name, old_name)))


def _add_temp_columns(bind: sa.Connection) -> None:
    """Add the temp ``jsonb`` columns with literal pairs (metadata-only)."""
    if not _column_exists(bind, "evals", "pre_version_raw__jsonb"):
        op.add_column("evals", sa.Column("pre_version_raw__jsonb", postgresql.JSONB(), nullable=True))
    if not _column_exists(bind, "eval_definitions", "pre_version_raw__jsonb"):
        op.add_column("eval_definitions", sa.Column("pre_version_raw__jsonb", postgresql.JSONB(), nullable=True))
    if not _column_exists(bind, "eval_suites", "pre_version_raw__jsonb"):
        op.add_column("eval_suites", sa.Column("pre_version_raw__jsonb", postgresql.JSONB(), nullable=True))
    if not _column_exists(bind, "suite_runs", "baseline_tuple__jsonb"):
        op.add_column("suite_runs", sa.Column("baseline_tuple__jsonb", postgresql.JSONB(), nullable=True))
    if not _column_exists(bind, "suite_runs", "comparison_json__jsonb"):
        op.add_column("suite_runs", sa.Column("comparison_json__jsonb", postgresql.JSONB(), nullable=True))
    if not _column_exists(bind, "suite_runs", "extra__jsonb"):
        op.add_column("suite_runs", sa.Column("extra__jsonb", postgresql.JSONB(), nullable=True))


def _add_downgrade_temp_columns(bind: sa.Connection) -> None:
    """Add the temp ``json`` columns with literal pairs for downgrade."""
    if not _column_exists(bind, "evals", "pre_version_raw__json"):
        op.add_column("evals", sa.Column("pre_version_raw__json", sa.JSON(), nullable=True))
    if not _column_exists(bind, "eval_definitions", "pre_version_raw__json"):
        op.add_column("eval_definitions", sa.Column("pre_version_raw__json", sa.JSON(), nullable=True))
    if not _column_exists(bind, "eval_suites", "pre_version_raw__json"):
        op.add_column("eval_suites", sa.Column("pre_version_raw__json", sa.JSON(), nullable=True))
    if not _column_exists(bind, "suite_runs", "baseline_tuple__json"):
        op.add_column("suite_runs", sa.Column("baseline_tuple__json", sa.JSON(), nullable=True))
    if not _column_exists(bind, "suite_runs", "comparison_json__json"):
        op.add_column("suite_runs", sa.Column("comparison_json__json", sa.JSON(), nullable=True))
    if not _column_exists(bind, "suite_runs", "extra__json"):
        op.add_column("suite_runs", sa.Column("extra__json", sa.JSON(), nullable=True))


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    op.execute("SET search_path TO public")
    _add_temp_columns(bind)

    for table_name, column_name in _COLUMNS:
        temp_name = f"{column_name}{_TEMP_SUFFIX}"
        _backfill(bind, table_name, column_name, temp_name, postgresql.JSONB())
        _swap(bind, table_name, column_name, temp_name, f"{column_name}{_OLD_SUFFIX}")


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    op.execute("SET search_path TO public")
    _add_downgrade_temp_columns(bind)

    for table_name, column_name in _COLUMNS:
        temp_name = f"{column_name}{_DOWNGRADE_TEMP_SUFFIX}"
        _backfill(bind, table_name, column_name, temp_name, sa.JSON())
        _swap(bind, table_name, column_name, temp_name, f"{column_name}{_DOWNGRADE_OLD_SUFFIX}")
