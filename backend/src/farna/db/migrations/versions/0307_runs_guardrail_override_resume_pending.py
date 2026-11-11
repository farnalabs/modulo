"""Dedicated marker column for the guardrail-override resume self-heal (FAR-1728).

Revision ID: 0307_runs_guardrail_override_resume_pending
Revises: 0306_trigger_secret_encryption
Create Date: 2026-10-13

Before this migration the guardrail-override resume flip's self-heal
discriminator was a sentinel STRING stuffed into ``runs.error_detail``:

    GUARDRAIL_OVERRIDE_RESUME_MARKER = "guardrail override applied (FAR-1728 resume self-heal marker)"

written ONLY by ``recovery.guardrail_override``'s flip and read by the
reconcile leg (``cron_helpers._build_re_dispatch_predicate``'s
guardrail-override branch + the row-side recheck
``cron_helpers._is_override_resume_row``). It works, but it puts a
CONTROL-FLOW marker in an OBSERVATION / error column — the exact shape the
Reviewer first-pass lesson warns against ("use a DEDICATED column, never a
failure marker in a contract/observation column") — and ``error_detail`` is
exposed on REST/MCP run responses, so a perfectly healthy overridden run
carried a self-heal marker on the wire.

This migration adds the dedicated column::

    runs.guardrail_override_resume_pending  boolean NOT NULL DEFAULT false

Shape choices:

* **NOT NULL DEFAULT false, not nullable.** Every pre-existing row reads
  "no override resume pending" with no backfill (the flip is the ONLY
  writer), the self-heal predicate is a simple ``IS true`` with no
  three-valued NULL logic, and the shape mirrors the neighbouring
  ``runs`` flags (``ledger_written``,
  ``node_deadline_watchdog_fired_count`` — NOT NULL + server default).
  A nullable ``guardrail_override_flipped_at`` timestamp was rejected: it
  carries no information the flip's own ``heartbeat_at`` stamp does not
  already have, and it would force NULL-comparison predicates.
* **NO index.** The column is near-always ``false`` (only a flipped,
  not-yet-reclaimed override row sets it), and the leg's selectivity is
  driven by the status / ``dispatch_phase`` / stale-heartbeat predicates
  that already bound the reconcile scan — a partial index on a
  near-empty flag would add write cost for no read gain.

Lifecycle (single writer / single clearer):

* SET — by ``recovery.guardrail_override``'s flip, ONLY for a
  node-started (``first_node_dispatched``) eval-blocked run, in the SAME
  atomic optimistic UPDATE that flips the status (so the marker can never
  exist without the flip, and vice versa).
* CLEARED — by the resume claim (``pipeline_execution._RESUME_CLAIM_UPDATE_SQL``
  / ``_RESUME_CLAIM_UPDATE_SQL_WITH_TOKEN``), in the same statement that
  stamps ``dispatch_phase='claimed'``. A successful claim therefore takes
  the row out of the self-heal class AND clears the flag in one write; a
  lost ``resume_run`` job leaves the flag set until the reconcile leg
  re-dispatches it.

The zero-node override flip (status ``pending``, nothing to resume) does
NOT set the column — it is re-dispatched ``execute_run`` and recovered by
the pending-class reconcile legs; stamping a "resume pending" flag there
would linger forever (the execute claim never clears it).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0307_runs_guardrail_override_resume_pending"
down_revision: str | None = "0306_trigger_secret_encryption"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# NOT NULL DEFAULT false mirrors the neighbouring runs flags (ledger_written /
# node_deadline_watchdog_fired_count): PG 11+ adds it without a table rewrite,
# and every existing row correctly reads "no override resume pending".
_COLUMN = sa.Column(
    "guardrail_override_resume_pending",
    sa.Boolean(),
    nullable=False,
    server_default="false",
)


def upgrade() -> None:
    op.add_column("runs", _COLUMN)


def downgrade() -> None:
    # Data column only — the pending markers go with it. The sentinel-in-
    # error_detail design this replaces is NOT restored: a downgrade lands on
    # the pre-FAR-1728 tree, which had no self-heal leg at all.
    op.drop_column("runs", "guardrail_override_resume_pending")
