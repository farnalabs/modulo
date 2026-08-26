"""Add the run-record idempotency-key persistence column (FAR-438).

Revision ID: 0150_run_idempotency_key
Revises: 0149_suite_run_trigger_kind
Create Date: 2026-08-26

FAR-410's ``stable_idempotency_key`` derivation landed WITHOUT a run-record
column (deferred because origin/main had a broken Alembic migration chain, now
repaired). This migration lands the persistence half of the contract:

* ``runs.idempotency_key`` — a nullable ``String(128)`` column holding the run's
  STABLE logical idempotency identity, ``<pipeline_id>:<run_number>``, computed
  at ``create_run`` and written once. A re-run that restores the SAME run reads
  it back and reuses the identical derived per-node keys, so the
  read-before-write dedupe suppresses a duplicate write (no double-submit).

The value is ``<pipeline_id>:<run_number>`` — NOT a per-replay ``run_id``. The
`run_number` is allocated once per org (``create_run``'s ``_allocate_run_number``),
so a restored re-run reuses the same number and thus the same identity. A fresh
per-replay ``run_id`` would mint a new key every re-run and silently defeat
dedupe, which is why the derivation contract (``idempotency._RUN_REF_RE``)
rejects it.

Additive + nullable: an existing run row simply carries ``NULL`` and is never
deduped by this path — no shipped migration is modified.

Reversible: downgrade drops the column. RLS is unchanged (the ``runs`` table
already has the org-scope policy; adding a column does not alter the row-level
USING predicate). No enum / ``ALTER TYPE ... ADD VALUE`` is involved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0150_run_idempotency_key"
down_revision: str | None = "0149_suite_run_trigger_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET search_path TO public")

    op.add_column("runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET search_path TO public")

    op.drop_column("runs", "idempotency_key")
