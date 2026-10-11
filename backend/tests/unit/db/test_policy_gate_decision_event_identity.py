"""Model-metadata tests for the decision-record event-identity key (FAR-1108 8b).

No database: inspects ``PolicyGateDecision.__table_args__`` only. Proves the
model declares the permanent partial unique index on ``eval_result_id`` and no
longer declares chunk 4's temporary composite bridge index.
"""

from __future__ import annotations

from sqlalchemy import Index, text

from modulo.db.models.policy_gate_decision import PolicyGateDecision

_PERMANENT = "uq_policy_gate_decisions_eval_result_id"
_TEMPORARY = "ix_tmp_policy_gate_decisions_run_gate_result"


def _indexes_by_name() -> dict[str, Index]:
    return {ix.name: ix for ix in PolicyGateDecision.__table_args__ if isinstance(ix, Index)}


def test_permanent_event_identity_index_declared() -> None:
    indexes = _indexes_by_name()
    assert _PERMANENT in indexes
    idx = indexes[_PERMANENT]
    assert idx.unique is True
    assert [c.name for c in idx.columns] == ["eval_result_id"]


def test_permanent_index_is_partial_on_both_dialects() -> None:
    """NULL-result rows stay unbounded, so the predicate excludes them."""
    idx = _indexes_by_name()[_PERMANENT]
    pg_where = idx.dialect_options["postgresql"]["where"]
    sqlite_where = idx.dialect_options["sqlite"]["where"]
    assert pg_where is not None
    assert sqlite_where is not None
    assert str(pg_where) == str(text("eval_result_id IS NOT NULL"))
    assert str(sqlite_where) == str(text("eval_result_id IS NOT NULL"))


def test_temporary_bridge_index_is_not_declared() -> None:
    """The model must reflect HEAD: chunk 4's bridge index was dropped."""
    assert _TEMPORARY not in _indexes_by_name()
