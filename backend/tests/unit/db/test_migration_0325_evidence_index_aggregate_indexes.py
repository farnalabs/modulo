"""Unit tests for migration 0325_evidence_index_aggregate_indexes (FAR-2055).

Structural + SQLite-model contract (no Postgres / Testcontainers needed):

* **Revision chain** - 0325 revises ``0324_purge_eval_definitions``; the
  graph's single, linear head is asserted by the shared
  ``_migration_graph.assert_single_linear_head`` helper (never a hard-coded head
  value), the revision id matches the filename, and neither ``branch_labels``
  nor ``depends_on`` is set.
* **Index shape (mocked ``op``)** - the upgrade emits exactly three idempotent
  ``create_index`` calls: the PARTIAL HITL index with the decided-rows
  predicate, and the two org-scoped DISTINCT composites. The downgrade drops
  all three.
* **Model parity** - the three models (``HitlClaim``, ``Run``, ``EvalResult``)
  declare the same indexes, so ``create_all``-derived SQLite schemas and
  migrated ones agree (the model/migration index-parity rule).
* **Non-Postgres is a no-op** - the migration returns early on SQLite, which
  takes its indexes from the model instead of Alembic's DDL.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

from modulo.db.models.eval_result import EvalResult
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.run import Run
from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0325_evidence_index_aggregate_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0324_purge_eval_definitions"

_HITL_INDEX = "ix_hitl_claims_org_decision_at"
_RUNS_INDEX = "ix_runs_org_snapshot_id"
_EVAL_RESULTS_INDEX = "ix_eval_results_org_eval_id"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    """Capture every ``op.*`` call an entry point makes, in order."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.index_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.drop_index_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def run(self, entry_point: str) -> _OpRecorder:
        module = _load_migration()
        with patch.object(module, "op") as op:
            op.get_bind.return_value.dialect.name = self.dialect
            op.create_index.side_effect = lambda *a, **k: self.index_calls.append((a, k))
            op.drop_index.side_effect = lambda *a, **k: self.drop_index_calls.append((a, k))
            getattr(module, entry_point)()
        return self


def _model_indexes(model: type, name: str) -> list[Any]:
    return [index for index in model.__table__.indexes if index.name == name]


class TestChain:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_down_revision_is_0324_purge_eval_definitions(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgradePostgres:
    def test_emits_exactly_three_create_index_calls(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")

        names = [args[0] for args, _kwargs in rec.index_calls]
        assert names == [_HITL_INDEX, _RUNS_INDEX, _EVAL_RESULTS_INDEX]

    def test_hitl_index_is_partial_on_decided_rows(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")
        (name, table, columns), kwargs = rec.index_calls[0]

        assert name == _HITL_INDEX
        assert table == "hitl_claims"
        assert tuple(columns) == ("organisation_id", "decision_at")
        assert str(kwargs["postgresql_where"]) == "decision IS NOT NULL"
        # The SQLite half is what ``create_all`` builds the unit-test schema
        # from, so the predicate must be declared for BOTH dialects - the
        # model-side parity test below asserts the same pair.
        assert str(kwargs["sqlite_where"]) == "decision IS NOT NULL"
        assert kwargs.get("if_not_exists") is True

    def test_runs_index_is_the_org_snapshot_composite(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")
        (name, table, columns), kwargs = rec.index_calls[1]

        assert name == _RUNS_INDEX
        assert table == "runs"
        assert tuple(columns) == ("organisation_id", "snapshot_id")
        assert kwargs.get("if_not_exists") is True

    def test_eval_results_index_is_the_org_eval_composite(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")
        (name, table, columns), kwargs = rec.index_calls[2]

        assert name == _EVAL_RESULTS_INDEX
        assert table == "eval_results"
        assert tuple(columns) == ("organisation_id", "eval_id")
        assert kwargs.get("if_not_exists") is True


class TestUpgradeNonPostgres:
    def test_creates_nothing_on_sqlite(self) -> None:
        rec = _OpRecorder("sqlite").run("upgrade")

        assert not rec.index_calls


class TestDowngrade:
    def test_drops_all_three_indexes(self) -> None:
        rec = _OpRecorder("postgresql").run("downgrade")

        dropped = [args[0] for args, _kwargs in rec.drop_index_calls]
        assert dropped == [_EVAL_RESULTS_INDEX, _RUNS_INDEX, _HITL_INDEX]
        for _args, kwargs in rec.drop_index_calls:
            assert kwargs.get("if_exists") is True


class TestModelParity:
    def test_hitl_model_declares_the_partial_org_decision_index(self) -> None:
        matches = _model_indexes(HitlClaim, _HITL_INDEX)

        assert matches, f"HitlClaim must declare {_HITL_INDEX}"
        index = matches[0]
        assert tuple(column.name for column in index.columns) == ("organisation_id", "decision_at")
        assert str(index.dialect_options["postgresql"]["where"]) == "decision IS NOT NULL"
        assert str(index.dialect_options["sqlite"]["where"]) == "decision IS NOT NULL"

    def test_runs_model_declares_the_org_snapshot_composite(self) -> None:
        matches = _model_indexes(Run, _RUNS_INDEX)

        assert matches, f"Run must declare {_RUNS_INDEX}"
        assert tuple(column.name for column in matches[0].columns) == ("organisation_id", "snapshot_id")

    def test_eval_results_model_declares_the_org_eval_composite(self) -> None:
        matches = _model_indexes(EvalResult, _EVAL_RESULTS_INDEX)

        assert matches, f"EvalResult must declare {_EVAL_RESULTS_INDEX}"
        assert tuple(column.name for column in matches[0].columns) == ("organisation_id", "eval_id")
