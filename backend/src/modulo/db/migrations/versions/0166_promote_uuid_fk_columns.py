"""Promote ``String(255)`` FK columns that hold UUIDs to native ``Uuid`` type.

Revision ID: 0166_promote_uuid_fk_columns
Revises: 0165_add_check_constraints
Create Date: 2026-08-29

Four columns store UUID primary keys of other tables as ``String(255)``:

* ``node_observations.node_id``  -> nodes.id
* ``run_evidence.node_id``       -> nodes.id  (part of the (run_id, node_id) PK)
* ``feedback_records.producing_node_id`` -> nodes.id
* ``agents.template_id``         -> composite_templates.id

Storing UUIDs as text loses type safety and index efficiency. The conversion
uses ``USING col::uuid`` so existing well-formed UUID values are preserved. The
two PK/unique-involved columns are dropped and recreated around the type change.

Deploy-safety — pre-flight UUID scan
------------------------------------
A single historical row holding a non-UUID value would otherwise hard-abort the
upgrade at ``ALTER TYPE`` with a cryptic PostgreSQL error and no escape hatch.
Before any type change we scan every affected column for values that are not a
canonical UUID and abort *before* mutating the schema, reporting the exact
offending rows (table, column, value) so operators get a precise quarantine
list instead of a failed deploy. The columns are ``NOT NULL`` so we cannot
silently NULL offenders; surfacing them for triage is the safe, non-destructive
choice. (A real ``FOREIGN KEY`` on these columns is intentionally deferred: it
would reject the synthetic node UUIDs that today's integration tests write, and
is tracked separately once those tests are migrated to maintain referential
integrity.)

Deploy window — lock / rewrite budget
-------------------------------------
``ALTER TYPE ... USING col::uuid`` on ``run_evidence.node_id`` and
``feedback_records.producing_node_id`` takes an ``ACCESS EXCLUSIVE`` lock and
rewrites the table; chained with 0162 (FKs) and 0163 (CHECKs) this is the
longest-held exclusive window in the chain. The pre-flight scan is a full,
unindexed pass over ``run_evidence`` and should be run against a quiet replica
first (or during a low-traffic window). Prefer applying this migration in its
own maintenance window rather than bundled with app traffic; on very large
``run_evidence`` tables consider splitting the type change out into a separate
migration with its own lock-time budget.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0166_promote_uuid_fk_columns"
down_revision: str | None = "0165_add_check_constraints"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Columns promoted from String(255) to Uuid. Scanned for non-UUID values before
# the type change.
_PREFLIGHT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_evidence", "node_id"),
    ("node_observations", "node_id"),
    ("feedback_records", "producing_node_id"),
    ("agents", "template_id"),
)

# Canonical UUID textual form, used by the pre-flight scan to detect rows that
# would fail the ``USING col::uuid`` cast.
_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _scan_offending_uuids() -> None:
    """Abort with a precise report if any promoted column holds a non-UUID value.

    Runs *before* any type change so a bad historical row produces an actionable
    error (which rows, which column) rather than a cryptic ``ALTER TYPE`` failure
    mid-upgrade with no offender list.
    """
    offenders: list[tuple[str, str, str]] = []
    bind = op.get_bind()
    for table, column in _PREFLIGHT_COLUMNS:
        rows = bind.execute(
            text(
                f"SELECT {column}::text FROM {table} "  # noqa: S608  # nosec B608
                f"WHERE {column} IS NOT NULL AND {column}::text !~ :re LIMIT 50"
            ),
            {"re": _UUID_RE},
        )
        for (value,) in rows:
            offenders.append((table, column, str(value)))

    if offenders:
        report = "\n".join(f"  - {t}.{c} = {v!r}" for t, c, v in offenders)
        raise RuntimeError(
            "Migration 0164 aborted: the following rows store a non-UUID value in "
            "a column being promoted to uuid. Quarantine/repair these rows before "
            f"re-running the upgrade:\n{report}"
        )


def upgrade() -> None:
    _scan_offending_uuids()

    op.drop_constraint("pk_run_evidence_run_node", "run_evidence", type_="primary")
    op.alter_column(
        "run_evidence",
        "node_id",
        type_=sa.Uuid(),
        postgresql_using="node_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_primary_key("pk_run_evidence_run_node", "run_evidence", ["run_id", "node_id"])

    op.drop_constraint("uq_node_observations_run_node", "node_observations", type_="unique")
    op.alter_column(
        "node_observations",
        "node_id",
        type_=sa.Uuid(),
        postgresql_using="node_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_unique_constraint("uq_node_observations_run_node", "node_observations", ["run_id", "node_id"])

    op.alter_column(
        "feedback_records",
        "producing_node_id",
        type_=sa.Uuid(),
        postgresql_using="producing_node_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "agents",
        "template_id",
        type_=sa.Uuid(),
        postgresql_using="template_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.drop_constraint("pk_run_evidence_run_node", "run_evidence", type_="primary")
    op.alter_column(
        "run_evidence",
        "node_id",
        type_=sa.String(length=255),
        postgresql_using="node_id::text",
        existing_type=sa.Uuid(),
        existing_nullable=False,
    )
    op.create_primary_key("pk_run_evidence_run_node", "run_evidence", ["run_id", "node_id"])

    op.drop_constraint("uq_node_observations_run_node", "node_observations", type_="unique")
    op.alter_column(
        "node_observations",
        "node_id",
        type_=sa.String(length=255),
        postgresql_using="node_id::text",
        existing_type=sa.Uuid(),
        existing_nullable=False,
    )
    op.create_unique_constraint("uq_node_observations_run_node", "node_observations", ["run_id", "node_id"])

    op.alter_column(
        "feedback_records",
        "producing_node_id",
        type_=sa.String(length=255),
        postgresql_using="producing_node_id::text",
        existing_type=sa.Uuid(),
        existing_nullable=False,
    )
    op.alter_column(
        "agents",
        "template_id",
        type_=sa.String(length=255),
        postgresql_using="template_id::text",
        existing_type=sa.Uuid(),
        existing_nullable=True,
    )
