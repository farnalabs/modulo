"""HITL claim decision vocabulary CHECK + sweep-actor composite index.

Schema legs:

1. ``ck_hitl_claims_decision`` — ``decision`` is written only as
   ``approved`` / ``rejected`` / ``deliver_manual`` (or NULL while the
   gate is undecided) by the single stamp authority
   (``HITLManager._decide``: ``_DECISION_APPROVED`` /
   ``_DECISION_REJECTED`` / ``_DECISION_DELIVER_MANUAL``), but nothing
   enforces that vocabulary at the DB level — a typo'd writer or a
   raw-SQL path would persist an unchecked value that the sweep alarm
   (``decision.in_(approved, deliver_manual)``) silently ignores.
   Now enforced at the DB level, NULL-tolerant for undecided gates.

2. ``ix_hitl_claims_sweep_actor_window`` — partial composite index on
   ``(organisation_id, decided_by, decision_at) WHERE decided_by IS NOT
   NULL AND decision IS NOT NULL``. The FAR-611 sweep alarm
   (``hitl_manager/sweep_alarm.py``) counts per-actor decisions with
   ``WHERE organisation_id = ? AND decided_by = ? AND decision IN (...)
   AND decision_at >= ?`` on every decided gate; the only pre-existing
   legs are the single-column ``ix_hitl_claims_organisation_id`` and
   ``ix_hitl_claims_decided_by`` (0217), so Postgres must bitmap-and
   two single-column scans plus a heap filter on the time range. The
   composite serves the lookup in one btree descent, and the partial
   predicate keeps it to decided rows only (undecided gates — the
   growing majority — never enter it).

Both are additive plain ``CREATE INDEX`` / ``ADD CONSTRAINT`` (Alembic
runs inside a transaction so ``CREATE INDEX CONCURRENTLY`` cannot be
used — the 0171/0182 precedent) and idempotent (``IF NOT EXISTS`` /
``IF EXISTS``). The CHECK is Postgres-only via
``op.create_check_constraint`` — SQLite cannot execute a bare
``ALTER TABLE ... ADD CONSTRAINT``. SQLite unit-test schemas get the
same index predicate and CHECK rule from the ``HitlClaim`` model's
``__table_args__`` (``sqlite_where`` + ``CheckConstraint``) via
``create_all`` (the 0291 precedent), so the SQLite-ported claim holds
at the model layer, not by running this migration's DDL there.

Deploy-safety: brief table-size-proportional write lock on
``hitl_claims`` during upgrade (one row per human gate — small
relative to ``runs``); the CHECK validates existing rows on creation.
All writers already use the three-value vocabulary, so validation
cannot fail on current data.

Downgrade: drops the index and the constraint.

Revision ID: 0307_hitl_claims_decision_sweep
Revises: 0306_trigger_secret_encryption
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0307_hitl_claims_decision_sweep"
down_revision: str | None = "0306_trigger_secret_encryption"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "hitl_claims"
_CHECK_NAME = "ck_hitl_claims_decision"
_INDEX_NAME = "ix_hitl_claims_sweep_actor_window"

# A claim is "decided with an actor" once the stamp authority sets both
# the verdict and its author (FAR-748); only those rows can ever match
# the sweep alarm's per-actor window query.
_DECIDED_WHERE = sa.text("decided_by IS NOT NULL AND decision IS NOT NULL")


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["organisation_id", "decided_by", "decision_at"],
        postgresql_where=_DECIDED_WHERE,
        if_not_exists=True,
    )
    if not _is_postgres():
        return
    op.create_check_constraint(
        _CHECK_NAME,
        _TABLE,
        sa.text("decision IS NULL OR decision IN ('approved', 'rejected', 'deliver_manual')"),
    )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint(_CHECK_NAME, _TABLE, type_="check")
    op.drop_index(_INDEX_NAME, table_name=_TABLE, if_exists=True)
