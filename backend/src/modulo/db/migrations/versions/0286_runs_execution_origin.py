"""FAR-1141: run-level execution origin provenance for dispatched runs.

Revision ID: 0286_runs_execution_origin
Revises: 0285_system_audit_events
Create Date: 2026-10-05

Originally numbered ``0283_runs_execution_origin``; ``main`` landed
``0283_runs_drop_unused_indexes``, ``0284_add_rejected_run_status`` and then
``0285_system_audit_events`` in the intervening slots, so this was renumbered
onto the next free sequential number and now chains onto that head (single
linear head preserved for ``check-migration-heads``).

ADR-042 requires that no claim-ready surface lets a run containing
externally-dispatched work read indistinguishably from a run Modulo executed
itself. Slice 2 stamped the NODE-level provenance (``witnessed_via`` /
``execution_identity`` / ``declared_external_cost``) on dispatch node output;
this slice adds the RUN-level origin and its read surfaces::

    runs.execution_origin       varchar(20) NULL
    run_daily_facts.execution_origin varchar(20) NULL

Semantics (single-sourced in ``modulo.db.models.run``):

* ``'dispatched'`` — the run's frozen snapshot graph contains at least one
  ``dispatch`` node, stamped by ``crud.run.create_run`` at run creation;
* ``NULL`` — executed by Modulo / legacy. Existing rows are deliberately NOT
  backfilled: per ADR-042 existing runs keep their current provenance
  unchanged, and a NULL on a pre-existing row is the accurate "recorded
  before this shipped" value. The value is a marker, not a counter, so there
  is nothing to increment into old rows.

Both columns are plain nullable ``ADD COLUMN``s with NO server default —
metadata-only on Postgres, so adding one to the hot ``runs`` table does not
rewrite it. No CHECK constraint is added on purpose: validating a new
constraint against a populated ``runs`` costs a full-table scan (or a NOT
VALID + later VALIDATE), which buys nothing while the vocabulary has exactly
one member and every write site reads the shared constant.

The fact column is the read side (ADR 020): ``record_run_facts`` copies it at
finalize, so the analytics read path never joins ``runs`` and the marker
outlives the 90-day run purge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0286_runs_execution_origin"
down_revision: str | None = "0285_system_audit_events"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Nullable, no server default: PG 11+ adds it as a metadata-only change, and
# every pre-existing row legitimately reads NULL ("provenance not recorded
# before this shipped" / executed-by-Modulo legacy).
_RUNS_ORIGIN = sa.Column(
    "execution_origin",
    sa.String(20),
    nullable=True,
    comment=(
        "run-level execution provenance (FAR-1141): 'dispatched' = the run's frozen "
        "snapshot graph contains at least one dispatch node; NULL = executed by Modulo "
        "/ recorded before this shipped"
    ),
)
_FACTS_ORIGIN = sa.Column(
    "execution_origin",
    sa.String(20),
    nullable=True,
    comment=(
        "run-level execution provenance — from runs.execution_origin at finalize "
        "(FAR-1141); NULL for facts finalized before this shipped"
    ),
)


def upgrade() -> None:
    op.add_column("runs", _RUNS_ORIGIN)
    op.add_column("run_daily_facts", _FACTS_ORIGIN)


def downgrade() -> None:
    # Data columns only — the origin markers go with them. op.drop_column is
    # dialect-agnostic (no schema-qualified raw SQL needed: there is no
    # backfill to undo), so a SQLite/MariaDB downgrade works too.
    op.drop_column("run_daily_facts", "execution_origin")
    op.drop_column("runs", "execution_origin")
