"""Add ``runs.execution_attempt_count`` - a dedicated durable execution counter (FAR-2076).

Revision ID: 0326_runs_execution_attempt_count
Revises: 0325_run_seam_coverage
Create Date: 2026-10-22

FAR-2076 unifies the retry budget (policy, transient-retry and reconcile legs)
onto ONE count. The first attempt DERIVED that count from claims
(``max(node_attempt_count, claim_count - 1)``) and the pre-PR gate rejected it:
``claim_count`` increments on EVERY SAQ claim, including the two classes that
are NOT an execution of the graph --

* a capacity-demoted claim (the run is re-pended because a pool / org /
  pipeline capacity slot was unavailable), and
* a HITL-resume claim (``resume_run`` continues a run that had already paused),

so a healthy run could reach the derived budget having failed zero executions
and be terminal-failed with budget to spare. ``node_attempt_count`` is also not
the right source: it counts per-NODE attempts, so a pre-node death has no node
to record, and it is written at a different point in the lifecycle.

This migration adds the dedicated column::

    runs.execution_attempt_count integer NOT NULL DEFAULT 0

Semantics - incremented EXACTLY ONCE per execution attempt of the graph, i.e.
per ``execute_run``. The counter is stamped by the executor in the transaction
that ADMITS the attempt (``_claim_under_capacity``), NOT by the SAQ claim
UPDATE and NOT by a HITL resume claim: a capacity-demoted claim never begins an
execution. A pre-node death (checkpointer setup, graph compile, connector-hub
init) DOES count: the attempt was made, so a retry there must spend budget.

Shape choices:

* **NOT NULL DEFAULT 0** mirrors the neighbouring run counters (``claim_count``
  / ``node_attempt_count`` / ``node_deadline_watchdog_fired_count``). On
  Postgres 11+ ``ADD COLUMN ... DEFAULT 0 NOT NULL`` is a METADATA-ONLY
  operation - the default is stored in ``pg_attribute`` and the existing heap
  is not rewritten - so this is safe on the hot ``runs`` table. It also makes
  every insert path that omits the column (an older container mid-rollout, a
  fixture, a raw-SQL writer) read 0 rather than NULL.
* **NO index.** The column is read only for a row already located by its
  primary key (the claim UPDATE / the retry consult), never filtered on, so an
  index would add write cost on the hottest table for no read gain.

Backfill: ``execution_attempt_count = COALESCE(node_attempt_count, 0)`` for
NON-TERMINAL rows only. The column is read solely for a row that can still be
claimed or re-dispatched (``pending`` / ``running`` / ``awaiting_human`` /
``claimed`` / ``hitl_parked``), so rewriting the whole historical table during
the release — while the old app is still serving traffic — would lock and bloat
the hottest table for rows no reader will ever consult. The terminal rows keep
the column default ``0``.

Note: PostgreSQL has NO no-op-update elimination. Every ``UPDATE`` writes a new
tuple version, even when the value is unchanged — so a predicate-free statement
rewrites the entire heap. The ``WHERE`` above is what keeps this migration off
the historical rows; there is no Postgres optimisation that would have done so
implicitly.

``node_attempt_count`` is a LOWER BOUND on executions (a pre-node-only history
yields 0), and that asymmetry is deliberate: **under-counting grants a retry,
over-counting kills a healthy run**, so under-counting is the safe direction.
``COALESCE`` is belt-and-braces (the source column is itself NOT NULL DEFAULT
0).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0326_runs_execution_attempt_count"
down_revision: str | None = "0325_run_seam_coverage"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# NOT NULL DEFAULT 0 mirrors the existing run counters: PG 11+ adds it without a
# table rewrite, and every pre-existing row correctly reads "no execution
# attempt recorded".
_EXECUTION_ATTEMPT_COUNT = sa.Column(
    "execution_attempt_count",
    sa.Integer(),
    nullable=False,
    server_default="0",
    comment=(
        "execution attempts of the graph (execute_run only - never resume_run, never a "
        "capacity-demoted claim); a pre-node death counts (FAR-2076)"
    ),
)

# Literal SQL only (no f-string / format-built SQL - migration-fstring-sql
# semgrep rule). Scoped to the NON-TERMINAL statuses the counter is read for: a
# predicate-free UPDATE rewrites every historical row of the hot ``runs`` table
# while the old app still serves (Postgres has no no-op-update elimination).
# Keep every status a reader can still claim / re-dispatch - including
# ``hitl_parked`` and ``claimed`` so a guardrail-override resume flip is
# covered. A lower bound on executions; under-counting is the safe direction
# (see the module docstring).
_BACKFILL_EXECUTION_ATTEMPT_COUNT = (
    "UPDATE runs SET execution_attempt_count = COALESCE(node_attempt_count, 0) "
    "WHERE status IN ('pending','running','awaiting_human','claimed','hitl_parked')"
)


def upgrade() -> None:
    # Metadata-only on Postgres 11+ (ADD COLUMN ... DEFAULT 0 NOT NULL), so the
    # hot ``runs`` heap is not rewritten.
    op.add_column("runs", _EXECUTION_ATTEMPT_COUNT)
    op.execute(sa.text(_BACKFILL_EXECUTION_ATTEMPT_COUNT))


def downgrade() -> None:
    # The count goes with the column; there is no backfill to undo, and
    # op.drop_column is dialect-agnostic.
    op.drop_column("runs", "execution_attempt_count")
