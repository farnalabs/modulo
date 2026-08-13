"""Migration tests for the ``ongoing`` trigger type (FAR-158, 0094/0095/0096).

No live Postgres here (integration territory) — instead:

* ``0094_ongoing_trigger_type`` is loaded and inspected directly: revision
  chain, the Postgres NOT VALID + VALIDATE constraint-recreation pattern, the
  partial CHECK strings, and the downgrade restoring the pre-feature strings.
* The ORM models' CHECK constraints are compared against the migration strings
  (drift guard): a CHECK edited on one side and not the other fails loudly.
* ``0095_ongoing_trigger_flag`` registers the flag inactive (default OFF);
  ``0096_ongoing_trigger_enabled_by_default`` flips it active (default ON) and
  is confirmed as the single head of the chain.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.sql.elements import TextClause

_MIGRATION_0094 = "0094_ongoing_trigger_type"
_MIGRATION_0095 = "0095_ongoing_trigger_flag"
_MIGRATION_0096 = "0096_ongoing_trigger_enabled_by_default"

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
def migration_0094() -> ModuleType:
    return _load_migration(_MIGRATION_0094)


@pytest.fixture(scope="module")
def migration_0095() -> ModuleType:
    return _load_migration(_MIGRATION_0095)


@pytest.fixture(scope="module")
def migration_0096() -> ModuleType:
    return _load_migration(_MIGRATION_0096)


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_VERSIONS_DIR.parent))


class TestMigration0094OngoingTriggerType:
    def test_revision_chain(self, migration_0094: ModuleType) -> None:
        assert migration_0094.revision == "0094_ongoing_trigger_type"
        assert migration_0094.down_revision == "0093_run_number_sequence"
        assert migration_0094.branch_labels is None

    def test_upgrade_uses_not_valid_then_validate(self, migration_0094: ModuleType) -> None:
        """The wide enum CHECKs are recreated with the Postgres NOT VALID +
        VALIDATE pattern (mirrors 0069) so the DROP/ADD skips the long
        ACCESS EXCLUSIVE re-scan and the explicit VALIDATE catches offenders."""
        source = _source(migration_0094)
        assert "NOT VALID" in source
        assert "VALIDATE CONSTRAINT" in source
        assert "ADD CONSTRAINT" in source

    def test_upgrade_widens_both_checks_with_ongoing(self, migration_0094: ModuleType) -> None:
        source = _source(migration_0094)
        assert _TRIGGERS_VALUES_POST in source
        assert _RUNS_VALUES_POST in source

    def test_partial_ongoing_checks_present(self, migration_0094: ModuleType) -> None:
        source = _source(migration_0094)
        assert _SPEND_PARTIAL in source
        assert _TARGET_PARTIAL in source

    def test_upgrade_creates_trigger_id_indexes(self, migration_0094: ModuleType) -> None:
        source = _source(migration_0094)
        assert "ix_runs_trigger_id_status" in source
        assert "ix_runs_trigger_id_created_at" in source
        assert 'create_index("ix_runs_trigger_id_status"' in source
        assert 'create_index("ix_runs_trigger_id_created_at"' in source

    def test_downgrade_restores_pre_strings_and_drops_indexes(self, migration_0094: ModuleType) -> None:
        source = _source(migration_0094)
        assert _TRIGGERS_VALUES_PRE in source
        assert _RUNS_VALUES_PRE in source
        assert 'drop_index("ix_runs_trigger_id_created_at"' in source
        assert 'drop_index("ix_runs_trigger_id_status"' in source


class TestMigration0095OngoingFlag:
    def test_revision_chain(self, migration_0095: ModuleType) -> None:
        assert migration_0095.revision == "0095_ongoing_trigger_flag"
        assert migration_0095.down_revision == "0094_ongoing_trigger_type"

    def test_in_chain_after_0096(self, migration_0095: ModuleType) -> None:
        script = _script()
        chain = {rev.revision for rev in script.walk_revisions()}
        assert migration_0095.revision in chain
        # 0095 is superseded as the head by 0096_ongoing_trigger_enabled_by_default.
        heads = script.get_heads()
        assert migration_0095.revision not in heads
        assert heads == ["0096_ongoing_trigger_enabled_by_default"]

    def test_flag_upserts_ongoing_trigger_inactive(self, migration_0095: ModuleType) -> None:
        assert "ongoing_trigger" in migration_0095._FLAGS
        assert migration_0095._FLAGS["ongoing_trigger"][0] == "community"


class TestMigration0096OngoingFlagEnabled:
    def test_revision_chain(self, migration_0096: ModuleType) -> None:
        assert migration_0096.revision == "0096_ongoing_trigger_enabled_by_default"
        assert migration_0096.down_revision == "0095_ongoing_trigger_flag"

    def test_single_head_chain(self, migration_0096: ModuleType) -> None:
        script = _script()
        heads = script.get_heads()
        assert heads == ["0096_ongoing_trigger_enabled_by_default"], f"expected a single head, got {heads}"
        assert migration_0096.revision in heads

    def test_flag_flips_ongoing_trigger_active(self, migration_0096: ModuleType) -> None:
        assert "ongoing_trigger" in migration_0096._FLAGS
        assert migration_0096._FLAGS["ongoing_trigger"][0] == "community"
        assert migration_0096._ACTIVE is True

    def test_upgrade_flips_row_to_active(self, migration_0096: ModuleType) -> None:
        is_active_values = _collect_is_active(migration_0096, "upgrade")
        assert is_active_values == [True]

    def test_downgrade_flips_row_back_to_inactive(self, migration_0096: ModuleType) -> None:
        is_active_values = _collect_is_active(migration_0096, "downgrade")
        assert is_active_values == [False]


def _collect_is_active(migration: ModuleType, func_name: str) -> list[bool]:
    """Run ``upgrade()``/``downgrade()`` against a mocked Postgres op and return
    the ``is_active`` bind values sent to the feature_flag_catalog upsert."""
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_op = MagicMock()
    mock_op.get_bind.return_value = mock_bind

    with patch.object(migration, "op", mock_op):
        getattr(migration, func_name)()

    values: list[bool] = []
    for call in mock_op.execute.call_args_list:
        stmt = call.args[0]
        if not isinstance(stmt, TextClause):
            continue
        if "feature_flag_catalog" not in stmt.text:
            continue
        params = stmt._bindparams
        if "is_active" in params:
            values.append(params["is_active"].value)
    return values


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
