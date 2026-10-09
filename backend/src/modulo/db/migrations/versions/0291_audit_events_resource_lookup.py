"""Add resource-lookup index for audit_events + chain-head counter guard.

Schema legs:

1. ``ix_audit_events_org_resource`` — composite index on
   ``(organisation_id, resource_type, resource_id)``. Every list/export
   path filters by ``resource_type`` (``_apply_filters`` in
   ``core/audit_logger``), and point lookups by ``(resource_type,
   resource_id)`` serve the run-workspace probe (``api/routes/runs.py``)
   and the MCP audit resource lookups. The existing FAR-748 composite
   covers ``(organisation_id, event_type, account_id, created_at)`` and
   cannot serve a leading-``resource_type`` shape, so without this index
   those queries fall back to a full org scan past the unrelated
   single-column indexes.

2. ``audit_chain_heads.event_count`` server default ``0`` (Postgres
   only; SQLite/ORM-created test schemas get it from the model's
   ``server_default`` via ``create_all``, the 0246 precedent). The column
   is ``NOT NULL`` with only a Python-side ``default=0``, so a raw-SQL
   INSERT omitting ``event_count`` fails with a NOT NULL violation.

3. ``ck_audit_chain_heads_event_count_nonneg`` — ``CHECK (event_count
   >= 0)`` on ``audit_chain_heads``. The counter only ever increments
   (hash-chain head); a negative value indicates corruption or a buggy
   writer. Added ``NOT VALID`` + ``VALIDATE CONSTRAINT`` (the 0186
   precedent) with a pre-flight violation count so a single legacy dirty
   row surfaces a descriptive error instead of dying mid-flight.

Downgrade: drops the index, the CHECK constraint, and the column default.

Revision ID: 0291_audit_events_resource_lookup
Revises: 0290_scheduled_reports_due_scan
Create Date: 2026-10-09
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision: str = "0291_audit_events_resource_lookup"
down_revision: str | None = "0290_scheduled_reports_due_scan"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEX = "ix_audit_events_org_resource"
_CONSTRAINT = "ck_audit_chain_heads_event_count_nonneg"

_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*")


def _validate_identifier(name: str) -> str:
    """Guard an interpolated SQL identifier before it reaches the statement."""
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def _add_constraint_sql() -> str:
    return (
        "ALTER TABLE audit_chain_heads ADD CONSTRAINT "
        f"{_validate_identifier(_CONSTRAINT)} CHECK (event_count >= 0) NOT VALID"
    )


def _validate_constraint_sql() -> str:
    return f"ALTER TABLE audit_chain_heads VALIDATE CONSTRAINT {_validate_identifier(_CONSTRAINT)}"


def _drop_constraint_sql() -> str:
    return f"ALTER TABLE audit_chain_heads DROP CONSTRAINT IF EXISTS {_validate_identifier(_CONSTRAINT)}"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "audit_events",
        ["organisation_id", "resource_type", "resource_id"],
    )
    if not _is_postgres():
        return
    op.get_bind().execute(sa.text("ALTER TABLE audit_chain_heads ALTER COLUMN event_count SET DEFAULT 0"))
    violation_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM audit_chain_heads WHERE NOT (event_count >= 0)"))  # nosec B608
        .scalar_one()
    )
    if violation_count:
        raise RuntimeError(
            f"Cannot add CHECK constraint {_CONSTRAINT}: {violation_count} existing row(s) "
            "in audit_chain_heads violate `event_count >= 0`. Quarantine or fix these "
            "rows before deploying this migration."
        )
    op.execute(_add_constraint_sql())
    op.execute(_validate_constraint_sql())


def downgrade() -> None:
    if _is_postgres():
        op.execute(_drop_constraint_sql())
        op.get_bind().execute(sa.text("ALTER TABLE audit_chain_heads ALTER COLUMN event_count DROP DEFAULT"))
    op.drop_index(_INDEX, table_name="audit_events")
