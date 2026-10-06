"""Vocabulary/constraint parity tests for the ``rejected`` run-status widening (FAR-1487).

``0283_add_rejected_run_status`` widens EVERY closed copy of the run-status
vocabulary that a terminal ``rejected`` run touches:

* ``ck_runs_status`` (``runs``) - the model's CheckConstraint is the single
  source of truth,
* ``ck_run_daily_facts_status`` (``run_daily_facts``) - every terminal run
  writes a facts row, so a ``rejected`` run would otherwise fail its insert
  (caught by the HITL round-trip integration test),
* the ``ix_runs_workspace_drift_sweep`` partial index predicate - it carries
  the sweep's ``status IN (TERMINAL_STATUSES)`` WHERE clause verbatim, so the
  widened vocabulary would orphan the old index.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from sqlalchemy import CheckConstraint, Index

from modulo.db.models.run import TERMINAL_STATUSES, Run

_NAME = "0283_add_rejected_run_status"
_PATH = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_NAME}.py"


def _load() -> ModuleType:
    assert _PATH.exists(), f"Migration file missing: {_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_NAME}", _PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statuses(expr: str) -> frozenset[str]:
    """The quoted status literals of a CHECK expression (the ARRAY[...] body for DO-block DDL)."""
    body = expr.split("ARRAY[", 1)[1].split("]", 1)[0] if "ARRAY[" in expr else expr
    return frozenset(re.findall(r"'([a-z_]+)'", body))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _model_status_list() -> frozenset[str]:
    check = next(c for c in Run.__table_args__ if isinstance(c, CheckConstraint) and c.name == "ck_runs_status")
    return frozenset(re.findall(r"'([a-z_]+)'", check.sqltext.text))


class TestChain:
    def test_chains_off_the_previous_head(self) -> None:
        module = _load()
        assert module.revision == _NAME
        assert module.down_revision == "0282_env_profiles_kubernetes"


class TestRunsStatusCheck:
    def test_new_runs_constraint_equals_the_model_vocabulary(self) -> None:
        module = _load()
        assert _statuses(module._ADD_NEW) == _model_status_list()
        assert "rejected" in _statuses(module._ADD_NEW)

    def test_drop_guard_expects_the_widened_list(self) -> None:
        module = _load()
        assert "''rejected''::charactervarying" in module._DROP_NEW

    def test_downgrade_restores_the_pre_rejected_list(self) -> None:
        module = _load()
        assert "rejected" not in _statuses(module._ADD_OLD)
        assert _statuses(module._ADD_OLD) | {"rejected"} == _statuses(module._ADD_NEW)

    def test_add_is_staged_not_valid_then_validated(self) -> None:
        module = _load()
        assert "NOT VALID;" in module._ADD_NEW
        assert "VALIDATE CONSTRAINT ck_runs_status" in module._VALIDATE_NEW


class TestRunDailyFactsStatusCheck:
    def test_facts_constraint_admits_rejected(self) -> None:
        module = _load()
        assert "rejected" in _statuses(module._FACTS_STATUS_NEW)
        assert "rejected" not in _statuses(module._FACTS_STATUS_OLD)
        assert _statuses(module._FACTS_STATUS_OLD) | {"rejected"} == _statuses(module._FACTS_STATUS_NEW)

    def test_executed_ddl_literals_embed_the_documented_lists(self) -> None:
        """The DDL is whole-literal (no interpolation - op.execute cannot bind
        parameters); pin that each literal carries its documented list."""
        module = _load()
        assert module._FACTS_STATUS_NEW in module._FACTS_ADD_NEW
        assert module._FACTS_STATUS_OLD in module._FACTS_ADD_OLD

    def test_facts_vocabulary_covers_every_terminal_status(self) -> None:
        """A terminal status the facts constraint rejects would make that
        run's terminalization fail its facts insert."""
        module = _load()
        assert frozenset(TERMINAL_STATUSES) <= _statuses(module._FACTS_STATUS_NEW)


class TestDriftSweepIndex:
    def test_widened_predicate_is_the_terminal_vocabulary(self) -> None:
        module = _load()
        in_list = module._DRIFT_PREDICATE_NEW.split(" AND ")[0]
        assert _statuses(in_list) == frozenset(TERMINAL_STATUSES)

    def test_widened_predicate_matches_the_model_index(self) -> None:
        module = _load()
        index = next(i for i in Run.__table_args__ if isinstance(i, Index) and i.name == module._DRIFT_INDEX)
        pg_where = str(index.dialect_options["postgresql"].get("where"))
        assert _normalise(pg_where) == _normalise(module._DRIFT_PREDICATE_NEW)

    def test_downgrade_restores_the_0278_predicate(self) -> None:
        module = _load()
        assert "rejected" not in module._DRIFT_PREDICATE_OLD
        assert _statuses(module._DRIFT_PREDICATE_OLD.split(" AND ")[0]) | {"rejected"} == frozenset(TERMINAL_STATUSES)


class TestDowngradeOrdering:
    def test_rejected_rows_are_demoted_before_the_constraints_are_restored(self) -> None:
        source = _PATH.read_text(encoding="utf-8")
        downgrade = source.split("def downgrade()")[1]
        demote_runs = downgrade.index("UPDATE runs SET status = 'cancelled' WHERE status = 'rejected'")
        demote_facts = downgrade.index("UPDATE run_daily_facts SET status = 'cancelled' WHERE status = 'rejected'")
        restore = downgrade.index("op.execute(_DROP_OLD)")
        assert demote_runs < restore
        assert demote_facts < restore
