"""Migration tests for the ``ongoing`` trigger type (FAR-158, 0094/0095/0096/0097)
and the ``slack_app_mention`` trigger type (FAR-57, 0098).

No live Postgres here (integration territory) — instead:

* ``0094_ongoing_trigger_type`` is loaded and inspected directly: revision
  chain, the Postgres NOT VALID + VALIDATE constraint-recreation pattern, the
  partial CHECK strings, and the downgrade restoring the pre-feature strings.
* The ORM models' CHECK constraints are compared against the migration strings
  (drift guard): a CHECK edited on one side and not the other fails loudly.
* ``0095_ongoing_trigger_flag`` registers the flag inactive (default OFF);
  main's ``0096_hitl_claims_overdue_notified`` sits on top of it;
  ``0097_ongoing_trigger_enabled_by_default`` flips it active (default ON).
* ``0098_slack_app_mention_trigger_type`` (FAR-57) sits on top of
  ``0097_ongoing_trigger_enabled_by_default``.
* ``0099_run_raw_output_markers`` (FAR-188) sits on top of
  ``0098_slack_app_mention_trigger_type``; main's ``0100_run_classification``
  (FAR-189) sits on top of it; this branch's FAR-208 ``0101_guardrails``
  migration (renumbered from ``0100_guardrails`` to clear the numeric-prefix
  collision with FAR-189's ``0100_run_classification``) and FAR-190's
  ``0102_ongoing_streak_epoch`` sit on top of that; main's
  ``0103_lifecycle_map_version_actor`` sits on top of the epoch; this branch's
  FAR-192 ``0104_trigger_event_auto_deactivated`` migration (renumbered from
  ``0103`` to sit on main's new head) sits on top of that, followed by main's
  FAR-219 ``0105_guardrail_pins`` and FAR-214 ``0106_trigger_event_guardrail_blocked``
  (which widens the ``trigger_events`` validation_result vocabulary with
  ``guardrail_blocked``); the chain head is this branch's FAR-223
  ``0107_guardrail_t1_remainder`` migration (renumbered from ``0106`` to clear
  the numeric-prefix collision with FAR-214's ``0106_trigger_event_guardrail_blocked``).
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
_MIGRATION_0096 = "0096_hitl_claims_overdue_notified"
_MIGRATION_0097 = "0097_ongoing_trigger_enabled_by_default"
_MIGRATION_0098 = "0098_slack_app_mention_trigger_type"
_MIGRATION_0099 = "0099_run_raw_output_markers"
_MIGRATION_0101 = "0101_guardrails"
_MIGRATION_0102 = "0102_ongoing_streak_epoch"
_MIGRATION_0104 = "0104_trigger_event_auto_deactivated"
_MIGRATION_0105 = "0105_guardrail_pins"
_MIGRATION_0106 = "0106_trigger_event_guardrail_blocked"
_MIGRATION_HEAD = "0107_guardrail_t1_remainder"

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


@pytest.fixture(scope="module")
def migration_0097() -> ModuleType:
    return _load_migration(_MIGRATION_0097)


@pytest.fixture(scope="module")
def migration_0098() -> ModuleType:
    return _load_migration(_MIGRATION_0098)


@pytest.fixture(scope="module")
def migration_0099() -> ModuleType:
    return _load_migration(_MIGRATION_0099)


@pytest.fixture(scope="module")
def migration_0101() -> ModuleType:
    return _load_migration(_MIGRATION_0101)


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

    def test_single_head_chain(self, migration_0095: ModuleType, migration_0096: ModuleType) -> None:
        script = _script()
        chain = {rev.revision for rev in script.walk_revisions()}
        assert migration_0095.revision in chain
        # which is itself superseded by 0098_slack_app_mention_trigger_type,
        # which is superseded by 0099_run_raw_output_markers, which is
        # superseded by 0100_run_classification, which is superseded by this
        # branch's FAR-208 head 0101_guardrails, then on through the FAR-219
        # head 0105_guardrail_pins, the FAR-214 head
        # 0106_trigger_event_guardrail_blocked, and this branch's FAR-223 head
        # 0107_guardrail_t1_remainder.
        heads = script.get_heads()
        assert migration_0095.revision not in heads
        assert migration_0096.revision not in heads
        assert heads == [_MIGRATION_HEAD], f"expected a single head, got {heads}"
        assert _MIGRATION_0098 not in heads
        assert _MIGRATION_0099 not in heads

    def test_0096_revises_0095(self, migration_0096: ModuleType) -> None:
        assert migration_0096.down_revision == "0095_ongoing_trigger_flag"

    def test_flag_upserts_ongoing_trigger_inactive(self, migration_0095: ModuleType) -> None:
        assert "ongoing_trigger" in migration_0095._FLAGS
        assert migration_0095._FLAGS["ongoing_trigger"][0] == "community"


class TestMigration0097OngoingFlagEnabled:
    def test_revision_chain(self, migration_0097: ModuleType) -> None:
        assert migration_0097.revision == "0097_ongoing_trigger_enabled_by_default"
        assert migration_0097.down_revision == "0096_hitl_claims_overdue_notified"

    def test_single_head_chain(self, migration_0097: ModuleType) -> None:
        script = _script()
        heads = script.get_heads()
        # 0097 is superseded as the head by 0099_run_raw_output_markers,
        # which is superseded by 0100_run_classification, which is superseded
        # by this branch's FAR-208 head 0101_guardrails, on through the FAR-219
        # head 0105_guardrail_pins, the FAR-214 head
        # 0106_trigger_event_guardrail_blocked, and this branch's FAR-223 head
        # 0107_guardrail_t1_remainder.
        assert migration_0097.revision not in heads
        assert heads == [_MIGRATION_HEAD], f"expected a single head, got {heads}"

    def test_flag_flips_ongoing_trigger_active(self, migration_0097: ModuleType) -> None:
        assert "ongoing_trigger" in migration_0097._FLAGS
        assert migration_0097._FLAGS["ongoing_trigger"][0] == "community"
        assert migration_0097._ACTIVE is True

    def test_upgrade_flips_row_to_active(self, migration_0097: ModuleType) -> None:
        is_active_values = _collect_is_active(migration_0097, "upgrade")
        assert is_active_values == [True]

    def test_downgrade_flips_row_back_to_inactive(self, migration_0097: ModuleType) -> None:
        is_active_values = _collect_is_active(migration_0097, "downgrade")
        assert is_active_values == [False]


class TestMigration0098SlackAppMention:
    def test_revision_chain(self, migration_0098: ModuleType) -> None:
        assert migration_0098.revision == "0098_slack_app_mention_trigger_type"
        assert migration_0098.down_revision == "0097_ongoing_trigger_enabled_by_default"
        assert migration_0098.branch_labels is None

    def test_single_head_chain(self, migration_0098: ModuleType, migration_0101: ModuleType) -> None:
        script = _script()
        heads = script.get_heads()
        # 0098 is superseded as the head by 0099_run_raw_output_markers, which
        # is superseded by 0100_run_classification, which is superseded by this
        # branch's FAR-208 migration 0101_guardrails, on through the FAR-219
        # head 0105_guardrail_pins, the FAR-214 head
        # 0106_trigger_event_guardrail_blocked, and this branch's FAR-223 head
        # 0107_guardrail_t1_remainder.
        assert migration_0098.revision not in heads
        assert _MIGRATION_HEAD in heads
        assert heads == [_MIGRATION_HEAD], f"expected a single head, got {heads}"

    def test_upgrade_widens_both_checks_with_slack_app_mention(self, migration_0098: ModuleType) -> None:
        source = _source(migration_0098)
        assert "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'slack_app_mention'" in source
        assert (
            "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'correction', 'slack_app_mention'"
            in source
        )

    def test_upgrade_uses_not_valid_then_validate(self, migration_0098: ModuleType) -> None:
        source = _source(migration_0098)
        assert "NOT VALID" in source
        assert "VALIDATE CONSTRAINT" in source
        assert "ADD CONSTRAINT" in source

    def test_downgrade_restores_pre_strings(self, migration_0098: ModuleType) -> None:
        source = _source(migration_0098)
        assert "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing'" in source
        assert "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'correction'" in source


class TestMigration0099RunRawOutputMarkers:
    def test_revision_chain(self, migration_0099: ModuleType) -> None:
        assert migration_0099.revision == "0099_run_raw_output_markers"
        assert migration_0099.down_revision == "0098_slack_app_mention_trigger_type"
        assert migration_0099.branch_labels is None

    def test_single_head_chain(self, migration_0099: ModuleType) -> None:
        script = _script()
        heads = script.get_heads()
        assert heads == [_MIGRATION_HEAD], f"expected a single head, got {heads}"
        assert migration_0099.revision not in heads
        assert _MIGRATION_HEAD in heads

    def test_upgrade_adds_raw_output_markers_column(self, migration_0099: ModuleType) -> None:
        source = _source(migration_0099)
        assert 'add_column("runs", sa.Column("raw_output_markers"' in source
        assert "JSONB()" in source
        assert "nullable=True" in source

    def test_downgrade_drops_raw_output_markers_column(self, migration_0099: ModuleType) -> None:
        source = _source(migration_0099)
        assert 'drop_column("runs", "raw_output_markers")' in source


class TestMigration0101Guardrails:
    def test_revision_chain(self, migration_0101: ModuleType) -> None:
        assert migration_0101.revision == "0101_guardrails"
        assert migration_0101.down_revision == "0100_run_classification"
        assert migration_0101.branch_labels is None

    def test_single_head_chain(self, migration_0101: ModuleType) -> None:
        script = _script()
        heads = script.get_heads()
        assert heads == [_MIGRATION_HEAD], f"expected a single head, got {heads}"
        assert _MIGRATION_HEAD in heads

    def test_upgrade_widens_eval_type_check_with_guardrail(self, migration_0101: ModuleType) -> None:
        source = _source(migration_0101)
        assert "'llm_judge', 'regex', 'json_schema', 'custom_function', 'guardrail'" in source
        assert "NOT VALID" in source
        assert "VALIDATE CONSTRAINT" in source

    def test_upgrade_adds_observed_column_to_eval_results(self, migration_0101: ModuleType) -> None:
        source = _source(migration_0101)
        assert 'add_column("eval_results", sa.Column("observed"' in source
        assert "nullable=False" in source

    def test_downgrade_converts_guardrail_rows_and_drops_observed(self, migration_0101: ModuleType) -> None:
        source = _source(migration_0101)
        assert "UPDATE eval_definitions SET eval_type='regex' WHERE eval_type='guardrail'" in source
        assert 'drop_column("eval_results", "observed")' in source


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
