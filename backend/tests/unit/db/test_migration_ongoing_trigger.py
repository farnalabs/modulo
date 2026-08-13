"""Migration tests for the ``ongoing`` trigger type (FAR-158, 0092/0093).

No live Postgres here (integration territory) — instead:

* ``0092_ongoing_trigger_type`` is loaded and inspected directly: revision
  chain, the Postgres NOT VALID + VALIDATE constraint-recreation pattern, the
  partial CHECK strings, and the downgrade restoring the pre-feature strings.
* The ORM models' CHECK constraints are compared against the migration strings
  (drift guard): a CHECK edited on one side and not the other fails loudly.
* ``0093_ongoing_trigger_flag`` is confirmed as the single head of the chain.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.script import ScriptDirectory

_MIGRATION_0092 = "0092_ongoing_trigger_type"
_MIGRATION_0093 = "0093_ongoing_trigger_flag"

_VERSIONS_DIR = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

# The pre-feature / post-feature vocabulary strings (hardcoded in 0087).
_TRIGGERS_VALUES_PRE = "'manual', 'webhook', 'cron', 'polling', 'agent_signal'"
_TRIGGERS_VALUES_POST = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing'"
_RUNS_VALUES_PRE = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction'"
_RUNS_VALUES_POST = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction', 'ongoing'"

_SPEND_PARTIAL = "trigger_type <> 'ongoing' OR (daily_spend_limit IS NOT NULL AND daily_spend_limit > 0)"
_TARGET_PARTIAL = "trigger_type <> 'ongoing' OR (max_concurrent_runs BETWEEN 1 AND 20)"


def _load_migration(name: str) -> ModuleType:
    path = _VERSIONS_DIR / f"{name}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration_0092() -> ModuleType:
    return _load_migration(_MIGRATION_0092)


@pytest.fixture(scope="module")
def migration_0093() -> ModuleType:
    return _load_migration(_MIGRATION_0093)


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_VERSIONS_DIR.parent))


class TestMigration0092OngoingTriggerType:
    def test_revision_chain(self, migration_0092: ModuleType) -> None:
        assert migration_0092.revision == "0092_ongoing_trigger_type"
        assert migration_0092.down_revision == "0091_run_evidence"
        assert migration_0092.branch_labels is None

    def test_upgrade_uses_not_valid_then_validate(self, migration_0092: ModuleType) -> None:
        """The wide enum CHECKs are recreated with the Postgres NOT VALID +
        VALIDATE pattern (mirrors 0069) so the DROP/ADD skips the long
        ACCESS EXCLUSIVE re-scan and the explicit VALIDATE catches offenders."""
        source = _source(migration_0092)
        assert "NOT VALID" in source
        assert "VALIDATE CONSTRAINT" in source
        assert "ADD CONSTRAINT" in source

    def test_upgrade_widens_both_checks_with_ongoing(self, migration_0092: ModuleType) -> None:
        source = _source(migration_0092)
        assert _TRIGGERS_VALUES_POST in source
        assert _RUNS_VALUES_POST in source

    def test_partial_ongoing_checks_present(self, migration_0092: ModuleType) -> None:
        source = _source(migration_0092)
        assert _SPEND_PARTIAL in source
        assert _TARGET_PARTIAL in source

    def test_upgrade_creates_trigger_id_indexes(self, migration_0092: ModuleType) -> None:
        source = _source(migration_0092)
        assert "ix_runs_trigger_id_status" in source
        assert "ix_runs_trigger_id_created_at" in source
        assert 'create_index("ix_runs_trigger_id_status"' in source
        assert 'create_index("ix_runs_trigger_id_created_at"' in source

    def test_downgrade_restores_pre_strings_and_drops_indexes(self, migration_0092: ModuleType) -> None:
        source = _source(migration_0092)
        assert _TRIGGERS_VALUES_PRE in source
        assert _RUNS_VALUES_PRE in source
        assert 'drop_index("ix_runs_trigger_id_created_at"' in source
        assert 'drop_index("ix_runs_trigger_id_status"' in source


class TestMigration0093OngoingFlag:
    def test_revision_chain(self, migration_0093: ModuleType) -> None:
        assert migration_0093.revision == "0093_ongoing_trigger_flag"
        assert migration_0093.down_revision == "0092_ongoing_trigger_type"

    def test_single_head_chain(self, migration_0093: ModuleType) -> None:
        script = _script()
        heads = script.get_heads()
        assert heads == ["0093_ongoing_trigger_flag"], f"expected a single head, got {heads}"
        assert migration_0093.revision in heads

    def test_flag_upserts_ongoing_trigger_inactive(self, migration_0093: ModuleType) -> None:
        assert "ongoing_trigger" in migration_0093._FLAGS
        assert migration_0093._FLAGS["ongoing_trigger"][0] == "community"


class TestOrmCheckDriftGuard:
    def test_trigger_orm_check_includes_ongoing(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.trigger import Trigger

        checks = [c for c in Trigger.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_triggers_type" in names
        triggers_check = next(c for c in checks if c.name == "ck_triggers_type")
        assert "ongoing" in triggers_check.sqltext.text

    def test_run_orm_check_includes_ongoing(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.run import Run

        checks = [c for c in Run.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_runs_trigger_type" in names
        runs_check = next(c for c in checks if c.name == "ck_runs_trigger_type")
        assert "ongoing" in runs_check.sqltext.text

    def test_orm_partial_ongoing_checks_present(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.trigger import Trigger

        checks = {c.name: c.sqltext.text for c in Trigger.__table_args__ if isinstance(c, CheckConstraint)}
        assert "ck_triggers_ongoing_spend_limit" in checks
        assert "ck_triggers_ongoing_target_range" in checks
        # Drift guard: the ORM partial CHECK strings must match the migration.
        assert checks["ck_triggers_ongoing_spend_limit"] == _SPEND_PARTIAL
        assert checks["ck_triggers_ongoing_target_range"] == _TARGET_PARTIAL

    def test_ongoing_status_set_matches_run_check(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.run import ONGOING_ACTIVE_STATUSES, Run

        status_check = next(
            c for c in Run.__table_args__ if isinstance(c, CheckConstraint) and c.name == "ck_runs_status"
        )
        assert all(status in status_check.sqltext.text for status in ONGOING_ACTIVE_STATUSES)


def _source(module: ModuleType) -> str:
    path = _VERSIONS_DIR / f"{module.revision}.py"
    return path.read_text(encoding="utf-8")
