"""Unit tests for migration 0309_hitl_claims_decision_sweep.

Structural + SQLite-model contract (no Postgres / Testcontainers needed):

* **Revision chain** — 0309 revises ``0308_runs_dispatch_phase_history``; the
  migrations directory has exactly one head (``0313_policy_gate_evaluation_mode``,
  pinned by all sibling chain tests), the revision id matches the filename, and
  neither ``branch_labels`` nor ``depends_on`` is set (the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin depend on
  that shape).
* **Index shape (mocked ``op``)** — the upgrade emits exactly one idempotent
  ``CREATE INDEX IF NOT EXISTS`` for ``ix_hitl_claims_sweep_actor_window`` on
  ``hitl_claims(organisation_id, decided_by, decision_at)`` carrying the
  partial predicate ``decided_by IS NOT NULL AND decision IS NOT NULL``; the
  downgrade drops it.
* **CHECK emission** — on Postgres the upgrade adds ``ck_hitl_claims_decision``
  via ``op.create_check_constraint`` (NULL-tolerant three-value vocabulary) and
  the downgrade drops it; on non-Postgres dialects the CHECK leg is skipped (a
  bare ``ALTER TABLE ... ADD CONSTRAINT`` is not SQLite-executable, the 0246 /
  0291 precedent) and ``create_all`` supplies it from the model.
* **Model parity** — ``HitlClaim`` declares the same partial index (name,
  ordered columns, ``postgresql_where`` / ``sqlite_where`` predicate) and the
  same CHECK name + expression, so ``create_all``'d schemas and migrated ones
  agree (the database.md rule: a migration must update the model in the SAME
  change).
* **CHECK effectiveness (SQLite)** — the model's CHECK actually rejects a
  decision outside the vocabulary while accepting a valid row.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.exc import IntegrityError

from modulo.db.models.hitl_claim import HitlClaim

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0309_hitl_claims_decision_sweep"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0308_runs_dispatch_phase_history"
_HEAD_MIGRATION = "0313_policy_gate_evaluation_mode"

_TABLE = "hitl_claims"
_INDEX_NAME = "ix_hitl_claims_sweep_actor_window"
_INDEX_COLUMNS = ("organisation_id", "decided_by", "decision_at")
_CHECK_NAME = "ck_hitl_claims_decision"
_CHECK_EXPRESSION = "decision IS NULL OR decision IN ('approved', 'rejected', 'deliver_manual')"
_DECIDED_WHERE = "decided_by IS NOT NULL AND decision IS NOT NULL"


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
        self.check_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.drop_constraint_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.drop_index_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def run(self, entry_point: str) -> _OpRecorder:
        module = _load_migration()
        with patch.object(module, "op") as op:
            op.get_bind.return_value.dialect.name = self.dialect
            op.create_index.side_effect = lambda *a, **k: self.index_calls.append((a, k))
            op.create_check_constraint.side_effect = lambda *a, **k: self.check_calls.append((a, k))
            op.drop_constraint.side_effect = lambda *a, **k: self.drop_constraint_calls.append((a, k))
            op.drop_index.side_effect = lambda *a, **k: self.drop_index_calls.append((a, k))
            getattr(module, entry_point)()
        return self


class TestChain:
    def test_single_head_is_0313_policy_gate_evaluation_mode(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0308_runs_dispatch_phase_history(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgradePostgres:
    def test_emits_exactly_one_create_index(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")
        assert len(rec.index_calls) == 1, rec.index_calls
        (name, table, columns), kwargs = rec.index_calls[0]
        assert name == _INDEX_NAME
        assert table == _TABLE
        assert tuple(columns) == _INDEX_COLUMNS
        assert kwargs.get("if_not_exists") is True

    def test_index_carries_the_partial_predicate(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")
        _args, kwargs = rec.index_calls[0]
        assert str(kwargs["postgresql_where"]) == _DECIDED_WHERE

    def test_adds_the_decision_check(self) -> None:
        rec = _OpRecorder("postgresql").run("upgrade")
        assert len(rec.check_calls) == 1, rec.check_calls
        (name, table, condition), _kwargs = rec.check_calls[0]
        assert name == _CHECK_NAME
        assert table == _TABLE
        assert str(condition) == _CHECK_EXPRESSION


class TestUpgradeNonPostgres:
    def test_skips_the_check_leg(self) -> None:
        """SQLite cannot execute a bare ``ALTER TABLE ... ADD CONSTRAINT``, so
        the CHECK is Postgres-only; the model's ``create_all`` path supplies it
        on SQLite (the 0246 / 0291 precedent)."""
        rec = _OpRecorder("sqlite").run("upgrade")
        assert not rec.check_calls
        assert len(rec.index_calls) == 1


class TestDowngrade:
    def test_postgres_drops_the_check_and_the_index(self) -> None:
        rec = _OpRecorder("postgresql").run("downgrade")
        assert len(rec.drop_constraint_calls) == 1, rec.drop_constraint_calls
        (name, table), kwargs = rec.drop_constraint_calls[0]
        assert name == _CHECK_NAME
        assert table == _TABLE
        assert kwargs.get("type_") == "check"
        assert len(rec.drop_index_calls) == 1, rec.drop_index_calls
        (index_name,), index_kwargs = rec.drop_index_calls[0]
        assert index_name == _INDEX_NAME
        assert index_kwargs.get("table_name") == _TABLE
        assert index_kwargs.get("if_exists") is True

    def test_non_postgres_drops_only_the_index(self) -> None:
        rec = _OpRecorder("sqlite").run("downgrade")
        assert not rec.drop_constraint_calls
        assert len(rec.drop_index_calls) == 1


class TestModelParity:
    def test_model_declares_the_sweep_actor_partial_index(self) -> None:
        """Model and migration ship the same index in the SAME change."""
        match = next(
            (idx for idx in HitlClaim.__table__.indexes if idx.name == _INDEX_NAME),
            None,
        )
        assert match is not None, f"HitlClaim must declare {_INDEX_NAME} (migration 0309 parity)"
        assert tuple(col.name for col in match.columns) == _INDEX_COLUMNS
        assert str(match.dialect_options["postgresql"].get("where")) == _DECIDED_WHERE
        assert str(match.dialect_options["sqlite"].get("where")) == _DECIDED_WHERE

    def test_model_declares_the_decision_check(self) -> None:
        declared = {c.name: str(c.sqltext) for c in HitlClaim.__table__.constraints if isinstance(c, CheckConstraint)}
        assert _CHECK_NAME in declared, f"HitlClaim must declare {_CHECK_NAME} (migration 0309 parity)"
        assert declared[_CHECK_NAME] == _CHECK_EXPRESSION


def _insert_claim(engine: object, decision: str | None) -> None:
    with engine.connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text(
                "INSERT INTO hitl_claims (id, organisation_id, run_id, review_id, pipeline_id, "
                "expires_at, decision) VALUES (:id, :org, :run, :review, :pipeline, :expires, :decision)"
            ),
            {
                "id": uuid.uuid4().hex,
                "org": uuid.uuid4().hex,
                "run": uuid.uuid4().hex,
                "review": "review-1",
                "pipeline": uuid.uuid4().hex,
                "expires": "2030-01-01 00:00:00",
                "decision": decision,
            },
        )
        conn.commit()


class TestCheckEffectiveness:
    def test_model_check_rejects_a_decision_outside_the_vocabulary(self) -> None:
        engine = create_engine("sqlite://")
        try:
            HitlClaim.__table__.create(engine)
            _insert_claim(engine, "approved")
            _insert_claim(engine, None)
            with pytest.raises(IntegrityError, match=_CHECK_NAME):
                _insert_claim(engine, "maybe")
        finally:
            engine.dispose()
