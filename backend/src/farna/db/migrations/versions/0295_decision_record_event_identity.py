"""FAR-1108 chunk 8b: permanent decision-record event-identity key.

Chunk 4 (``0261_decision_record_payload``) shipped a **TEMPORARY** uniqueness
bound, ``ix_tmp_policy_gate_decisions_run_gate_result`` on ``(run_id,
policy_gate_id, eval_result_id)``, and named this work item (chunk 8b) as its
owner, to decide whether the bound becomes permanent or is replaced.

**Decision (recorded here and in the model docstring):** the bound is
REPLACED. The event-identity key of a decision record is the persisted
evaluation result it decided on — ``eval_result_id`` — enforced by the
partial unique index ``uq_policy_gate_decisions_eval_result_id``
(``WHERE eval_result_id IS NOT NULL``).

Rationale:

* ``(run_id, policy_gate_id)`` cannot be the key: a ``warn``-configured node's
  retry re-runs its evaluations **within the same run id**, each persisting a
  fresh ``EvalResult`` and constituting a distinct governance event.
* ``eval_result_id`` is the correct key because chunk 2
  (persist-before-decide) persists a distinct ``EvalResult`` per evaluation
  attempt **before** the decision is made, so the result id names exactly one
  governance event. One attempt → one result → one decision.
* The chunk-4 composite is redundant (any two rows sharing a result id also
  share its run and gate) and strictly weaker (it would allow the same
  ``eval_result_id`` under a different ``run_id`` — a transposition bug the
  plain non-FK columns carry as a residual gap). The result-only key rejects
  that too.
* NULL-result rows stay unbounded by design (PostgreSQL and SQLite treat NULLs
  as distinct in a unique index): such rows are themselves anomalies and are
  owned by the reconciliation job (``core/eval_engine/decision_reconcile.py``),
  not the index.

This migration drops the temporary bridge index and creates the permanent
partial unique index. It is idempotent in both directions. Downgrade restores
the temporary bridge index (it does NOT re-add the six payload columns — those
belong to ``0261``).

Revision ID: 0295_decision_record_event_identity
Revises: 0294_eval_results_org_fk
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0295_decision_record_event_identity"
down_revision: str | None = "0294_eval_results_org_fk"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "policy_gate_decisions"
_TMP_INDEX = "ix_tmp_policy_gate_decisions_run_gate_result"
_PERMANENT_INDEX = "uq_policy_gate_decisions_eval_result_id"
_PARTIAL_PREDICATE = sa.text("eval_result_id IS NOT NULL")


def _index_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {ix["name"] for ix in inspector.get_indexes(_TABLE)}


def _drop_tmp_index() -> None:
    """Drop the chunk-4 temporary bridge index (idempotent)."""
    if _TMP_INDEX in _index_names():
        op.drop_index(_TMP_INDEX, table_name=_TABLE)


def _create_permanent_index() -> None:
    """Create the permanent partial unique event-identity index (idempotent)."""
    if _PERMANENT_INDEX not in _index_names():
        op.create_index(
            _PERMANENT_INDEX,
            _TABLE,
            ["eval_result_id"],
            unique=True,
            postgresql_where=_PARTIAL_PREDICATE,
            sqlite_where=_PARTIAL_PREDICATE,
        )


def upgrade() -> None:
    _drop_tmp_index()
    _create_permanent_index()


def downgrade() -> None:
    if _PERMANENT_INDEX in _index_names():
        op.drop_index(_PERMANENT_INDEX, table_name=_TABLE)
    # Restore the chunk-4 temporary bridge index if it is absent.
    if _TMP_INDEX not in _index_names():
        op.create_index(
            _TMP_INDEX,
            _TABLE,
            ["run_id", "policy_gate_id", "eval_result_id"],
            unique=True,
        )
