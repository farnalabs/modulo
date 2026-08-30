"""Add the run-record idempotency-key persistence column (FAR-438).

Revision ID: 0160_run_idempotency_key
Revises: 0159_pipeline_retry_compensation
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
deduped by this path — no shipped migration is modified. This migration was
originally ``0151_run_idempotency_key`` (then ``0156_run_idempotency_key``) but
was renumbered to ``0160_run_idempotency_key`` and re-parented onto
``0159_pipeline_retry_compensation`` to avoid a two-head Alembic collision with
main's ``0158_sso_provider_id`` migration that landed ahead of this PR on
main. The single head is now ``0160_run_idempotency_key`` (chain: 0157 ->
0158_sso_provider_id -> 0159_pipeline_retry_compensation ->
0160_run_idempotency_key).

Reversible: downgrade drops the column. RLS is unchanged (the ``runs`` table
already has the org-scope policy; adding a column does not alter the row-level
USING predicate). No enum / ``ALTER TYPE ... ADD VALUE`` is involved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0160_run_idempotency_key"
down_revision: str | None = "0159_pipeline_retry_compensation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET search_path TO public")

    # Idempotent: the ``runs.idempotency_key`` column is also added by the
    # parallel ``0159_pipeline_retry_compensation`` migration (FAR-402 P5) that the
    # merge queue lands ahead of this PR. Only add it if it is not already present,
    # so the two migrations can be applied in either order without a "column already
    # exists" failure. The model field is defined once (in run.py).
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    if "idempotency_key" not in existing:
        op.add_column("runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET search_path TO public")

    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    if "idempotency_key" in existing:
        op.drop_column("runs", "idempotency_key")
