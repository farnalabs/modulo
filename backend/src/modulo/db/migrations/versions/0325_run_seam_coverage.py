"""FAR-2054: runs.seam_coverage_json (exhaustive per-seam guardrail coverage).

Revision ID: 0325_run_seam_coverage
Revises: 0324_purge_eval_definitions
Create Date: 2026-10-23

Adds one nullable JSON column to ``runs`` storing the exhaustive, provenance-
typed per-seam guardrail coverage list written at run creation::

    [{seam, state, provenance, reason}, ...]

over the three known seams:

* ``ingestion_edge``  - the run-creation guardrail pass;
* ``node_output_edge`` - the executor's chain-end output interception;
* ``runtime_loop``    - always ``not_applicable`` (Alpha does not consume a
  runtime tool-event stream it can verify).

``enforced`` is only ever stored with ADR-042 ``attested`` provenance, i.e.
where Modulo itself acted; a runtime-sourced value can never produce it (the
typed contract in ``core/guardrails/coverage.py`` enforces this on the read
path too).

Physical type: ``jsonb`` on Postgres, generic ``JSON`` elsewhere - the
repo-wide multi-backend parity convention (``JSONB_COL`` in the ORM). This is
a METADATA-ONLY add (nullable, no server default, no backfill): safe on the
hot ``runs`` table per the FAR-403 ALTER-TYPE lesson, which applies to type
rewrites, not adds. NULL for runs created before this shipped; the run-detail
reader exposes no coverage for those rather than fabricating one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0325_run_seam_coverage"
down_revision: str | None = "0324_purge_eval_definitions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "seam_coverage_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "seam_coverage_json")
