"""Unit tests for migration 0278_trigger_events_trigger_type_check.

Structural: load the migration module and assert its contract without a
database, and pin model/migration parity for the new trigger_events
trigger_type vocabulary CHECK:

* the chain is pinned (0278 -> ``0277_trigger_events_listing_indexes``)
  and it is the single linear head, so the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin cannot
  be ambushed by a renumber;
* the upgrade adds the ``ck_trigger_events_trigger_type`` CHECK
  ``NOT VALID`` then ``VALIDATE``-d, mirroring the 0151/0165/0269
  online-constraint pattern;
* the downgrade drops the same constraint;
* the migration's vocabulary matches the ORM declaration (and the parent
  ``ck_triggers_type`` vocabulary) value-for-value, so a value added to
  one side and not the other breaks here instead of in prod.

They run without a database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from modulo.db.models.trigger_event import TriggerEvent

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0278_trigger_events_trigger_type_check"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0277_trigger_events_listing_indexes"

_CONSTRAINT = "ck_trigger_events_trigger_type"

#: The trigger_type vocabulary. Single source of truth asserted against the
#: migration DDL, the ORM CHECK on this table, and the parent triggers
#: table — a value added to one side and not the others fails here.
_EXPECTED_VALUES = (
    "manual",
    "webhook",
    "cron",
    "polling",
    "agent_signal",
    "ongoing",
    "slack_app_mention",
)


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """Return the migration's executable code, minus the module docstring."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def _orm_trigger_type_check() -> CheckConstraint:
    checks = [c for c in TriggerEvent.__table_args__ if isinstance(c, CheckConstraint)]
    return next(c for c in checks if c.name == _CONSTRAINT)


class TestChain:
    def test_single_head_is_0278(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_MIGRATION_NAME], f"expected a single head, got {heads}"

    def test_down_revision_is_0277_trigger_events_listing_indexes(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_upgrade_adds_the_check_not_valid_then_validates(self) -> None:
        code = _source_code()
        assert f"ADD CONSTRAINT {_CONSTRAINT} CHECK" in code
        assert f"VALIDATE CONSTRAINT {_CONSTRAINT}" in code
        # The ADD must not scan existing rows on a populated table.
        assert "NOT VALID" in code
        assert code.count("ADD CONSTRAINT") == 1
        assert code.count("VALIDATE CONSTRAINT") == 1
        # The upgrade must execute the pair, not merely declare it.
        assert "_CHECKS" in code.split("def upgrade", 1)[1]

    def test_migration_carries_the_full_trigger_vocabulary(self) -> None:
        code = _source_code()
        for value in _EXPECTED_VALUES:
            assert f"'{value}'" in code, f"migration CHECK missing {value!r}"

    def test_no_interpolated_ddl(self) -> None:
        code = _source_code()
        assert "op.execute(f" not in code
        assert "text(f" not in code


class TestDowngrade:
    def test_downgrade_drops_the_check(self) -> None:
        code = _source_code()
        assert f'op.drop_constraint("{_CONSTRAINT}"' in code
        assert code.count("op.drop_constraint(") == 1


class TestModelParity:
    def test_model_declares_the_trigger_type_check(self) -> None:
        check = _orm_trigger_type_check()
        for value in _EXPECTED_VALUES:
            assert f"'{value}'" in check.sqltext.text, f"ORM CHECK missing {value!r}"

    def test_model_vocabulary_matches_parent_triggers_table(self) -> None:
        from modulo.db.models.trigger import Trigger

        parent = next(
            c for c in Trigger.__table_args__ if isinstance(c, CheckConstraint) and c.name == "ck_triggers_type"
        )
        assert _orm_trigger_type_check().sqltext.text == parent.sqltext.text
