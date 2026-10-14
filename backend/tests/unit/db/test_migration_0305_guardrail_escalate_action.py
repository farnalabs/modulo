"""Unit tests for migration 0305_guardrail_escalate_action (FAR-1694 S1).

Structural contract (no Postgres needed):

* **Revision chain** - 0305 revises ``0304_metrics_staging_recorded_at_index``;
  the migrations directory has exactly one head
  (``0311_memberships_active_lookups``, pinned by all sibling chain tests), the
  revision id matches the filename, and neither ``branch_labels`` nor
  ``depends_on`` is set (the pre-commit check-migration-heads hook and every
  ``test_single_head_*`` pin depend on that shape).
* **Upgrade (mocked ``op``)** - Postgres drops + recreates BOTH action CHECKs
  widened with ``'escalate'`` (``NOT VALID`` then ``VALIDATE``); SQLite does the
  same through batch mode (SQLite has no ``ALTER TABLE ... ADD CHECK``).
* **Downgrade** - narrows both CHECKs back to the 0250/0261 vocabularies.
* **Vocabulary parity** - ``GuardrailAction.ESCALATE``, the
  ``LoopInterceptOutcome.action`` literal, and the two ORM ``CheckConstraint``
  predicates all carry ``'escalate'``, so the constant and the widening cannot
  drift (the DB-vocabulary rule).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import get_args, get_type_hints
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from modulo.core.guardrails import GuardrailAction
from modulo.core.guardrails.loop_intercept import LoopInterceptOutcome
from modulo.db.models.policy_gate import PolicyGate
from modulo.db.models.policy_gate_decision import (
    RESOLVED_ACTION_VALUES,
    PolicyGateDecision,
)

_MIGRATION_REVISION = "0305_guardrail_escalate_action"
_MIGRATION_DOWN_REVISION = "0304_metrics_staging_recorded_at_index"
_HEAD_MIGRATION = "0311_memberships_active_lookups"
_GATES_CHECK = "ck_policy_gates_action"
_DECISIONS_CHECK = "ck_policy_gate_decisions_resolved_action"

_GATES_WIDENED = "action IN ('warn', 'block', 'escalate')"
_GATES_ORIGINAL = "action IN ('warn', 'block')"
_DECISIONS_WIDENED = "resolved_action IN ('continue', 'warn', 'block', 'escalate')"
_DECISIONS_ORIGINAL = "resolved_action IN ('continue', 'warn', 'block')"

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_REVISION}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_REVISION}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _run(what: str, dialect: str) -> MagicMock:
    """Run *what* with a mocked ``op`` whose bind reports *dialect*."""
    module = _load_migration()
    bind = MagicMock()
    bind.dialect.name = dialect
    op = MagicMock()
    op.get_bind.return_value = bind
    batch = MagicMock()
    op.batch_alter_table.return_value.__enter__.return_value = batch
    op.batch_alter_table.return_value.__exit__.return_value = False
    with patch.object(module, "op", op):
        getattr(module, what)()
    op.batch = batch  # type: ignore[attr-defined]
    return op


def _executed_sql(op: MagicMock) -> list[str]:
    # The migration passes sa.text(...) to op.execute, so coerce to str.
    return [str(call.args[0]) for call in op.execute.call_args_list]


class TestChain:
    def test_single_head_is_0311_memberships_active_lookups(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0304_metrics_staging_recorded_at_index(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgradePostgres:
    def test_widens_both_checks_with_escalate(self) -> None:
        joined = "\n".join(_executed_sql(_run("upgrade", "postgresql")))
        assert _GATES_WIDENED in joined
        assert _DECISIONS_WIDENED in joined

    def test_drops_readds_not_valid_then_validates_each_check(self) -> None:
        executed = _executed_sql(_run("upgrade", "postgresql"))
        drops = [ddl for ddl in executed if "DROP CONSTRAINT" in ddl]
        adds = [ddl for ddl in executed if "ADD CONSTRAINT" in ddl]
        validates = [ddl for ddl in executed if "VALIDATE CONSTRAINT" in ddl]
        assert len(drops) == 2, f"expected one DROP per CHECK, got {drops}"
        assert len(adds) == 2, f"expected one ADD per CHECK, got {adds}"
        assert len(validates) == 2, f"expected one VALIDATE per CHECK, got {validates}"
        for ddl in drops + validates:
            assert "pg_constraint" in ddl
            assert "IF EXISTS" in ddl
        for ddl in adds:
            assert "NOT VALID;" in ddl


class TestUpgradeSqlite:
    def test_both_checks_recreated_in_batch_mode(self) -> None:
        op = _run("upgrade", "sqlite")
        dropped = [call.args[0] for call in op.batch.drop_constraint.call_args_list]
        assert dropped == [_GATES_CHECK, _DECISIONS_CHECK]
        created = [call.args for call in op.batch.create_check_constraint.call_args_list]
        assert created[0][0] == _GATES_CHECK
        assert created[0][1] == _GATES_WIDENED
        assert created[1][0] == _DECISIONS_CHECK
        assert created[1][1] == _DECISIONS_WIDENED


class TestDowngrade:
    def test_postgres_narrows_both_checks(self) -> None:
        joined = "\n".join(_executed_sql(_run("downgrade", "postgresql")))
        assert _GATES_ORIGINAL in joined
        assert _DECISIONS_ORIGINAL in joined
        assert "escalate" not in joined

    def test_sqlite_narrows_both_checks(self) -> None:
        op = _run("downgrade", "sqlite")
        created = [call.args for call in op.batch.create_check_constraint.call_args_list]
        assert created[0][1] == _GATES_ORIGINAL
        assert created[1][1] == _DECISIONS_ORIGINAL


class TestVocabularyParity:
    def test_guardrail_action_carries_escalate(self) -> None:
        assert GuardrailAction.ESCALATE.value == "escalate"

    def test_loop_intercept_outcome_literal_carries_escalate(self) -> None:
        action_type = get_type_hints(LoopInterceptOutcome)["action"]
        assert "escalate" in get_args(action_type)

    def test_policy_gate_check_widened(self) -> None:
        checks = [c for c in PolicyGate.__table_args__ if isinstance(c, CheckConstraint)]
        match = next((c for c in checks if c.name == _GATES_CHECK), None)
        assert match is not None, f"PolicyGate.__table_args__ missing {_GATES_CHECK}"
        assert str(match.sqltext) == _GATES_WIDENED

    def test_policy_gate_decision_check_widened(self) -> None:
        checks = [c for c in PolicyGateDecision.__table_args__ if isinstance(c, CheckConstraint)]
        match = next((c for c in checks if c.name == _DECISIONS_CHECK), None)
        assert match is not None, f"PolicyGateDecision.__table_args__ missing {_DECISIONS_CHECK}"
        assert str(match.sqltext) == _DECISIONS_WIDENED

    def test_resolved_action_values_carries_escalate(self) -> None:
        assert "escalate" in RESOLVED_ACTION_VALUES

    def test_migration_source_never_edits_shipped_revisions(self) -> None:
        # The widening is a NEW revision on the current head, never an edit of
        # 0250/0261 (the DB-vocabulary rule).
        source = _source()
        assert "0250_eval_policy_gate" not in source
        assert "0261_decision_record_payload" not in source
