"""Unit tests for migration 0288_runs_execution_origin (FAR-1141 / ADR-042).

Structural — load the migration module and pin its contract without a
database, plus ORM parity for both new columns:

1. the chain: 0288 revises 0287_team_rls_lifecycle_evals,
   0289_pipelines_environment_profile chains onto 0288, and
   0290_scheduled_reports_due_scan chains onto 0289, and
   0291_invitations_lookup_constraints chains onto 0290, and
   0292_audit_events_resource_lookup chains onto 0291_invitations_lookup_constraints, and
   0293_oauth_clients_team_id chains onto 0292_audit_events_resource_lookup, and
   0295_oauth_consent_state_preauth_rls chains onto 0294_eval_results_org_fk as the single
   linear head;
2. the upgrade adds EXACTLY two nullable ``varchar(20)`` columns with NO
   server default and NO backfill (metadata-only on the hot ``runs`` table);
3. the downgrade drops exactly those two columns;
4. model/migration parity: ``Run.execution_origin`` and
   ``RunDailyFact.execution_origin`` declare the same name / type / nullability
   the migration creates, so ``test_initial_migration``'s ORM-vs-migrated-DB
   parity check cannot diverge;
5. the vocabulary constant the write site and the readers share.

They run without a database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

from alembic.script import ScriptDirectory
from sqlalchemy import String

from modulo.db.models.run import EXECUTION_ORIGIN_DISPATCHED, EXECUTION_ORIGIN_VALUES, Run
from modulo.db.models.run_daily_facts import RunDailyFact

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0288_runs_execution_origin"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0287_team_rls_lifecycle_evals"
_CHAIN_HEAD_MIGRATION = "0295_oauth_consent_state_preauth_rls"

#: (table, column) pairs the upgrade must add — the two read surfaces ADR-042
#: needs: the run row itself and the self-contained analytics fact.
_EXPECTED_COLUMNS = (("runs", "execution_origin"), ("run_daily_facts", "execution_origin"))


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_calls(entry_point: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Every DDL call the given entry point makes, as (args, kwargs)."""
    module = _load_migration()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    with patch.object(module, "op") as mock_op:
        mock_op.add_column.side_effect = _record
        mock_op.drop_column.side_effect = _record
        getattr(module, entry_point)()
    return calls


class TestChain:
    def test_single_head_is_0288_runs_execution_origin(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0287_team_rls_lifecycle_evals(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_adds_exactly_the_two_expected_columns(self) -> None:
        calls = _migration_calls("upgrade")
        assert len(calls) == len(_EXPECTED_COLUMNS), f"expected exactly two add_column calls, got {calls}"
        actual = tuple((args[0], args[1].name) for args, _kwargs in calls)
        assert actual == _EXPECTED_COLUMNS

    def test_columns_are_nullable_varchar20_without_server_default(self) -> None:
        """Metadata-only ADD COLUMN: nullable, no server default (so Postgres
        does not rewrite the hot ``runs`` table), and String(20) to match the
        ORM."""
        for args, _kwargs in _migration_calls("upgrade"):
            column = args[1]
            assert column.name == "execution_origin"
            assert column.nullable is True, f"{args[0]}.execution_origin must be nullable"
            assert column.server_default is None, f"{args[0]}.execution_origin must have no server default"
            assert isinstance(column.type, String)
            assert column.type.length == 20


class TestDowngrade:
    def test_drops_exactly_the_two_expected_columns(self) -> None:
        calls = _migration_calls("downgrade")
        assert len(calls) == len(_EXPECTED_COLUMNS), f"expected exactly two drop_column calls, got {calls}"
        actual = {(args[0], args[1]) for args, _kwargs in calls}
        assert actual == set(_EXPECTED_COLUMNS)


class TestModelParity:
    def _assert_parity(self, model: type[Any]) -> None:
        column = model.__table__.columns["execution_origin"]
        assert column.nullable is True, f"{model.__tablename__}.execution_origin must be nullable"
        assert column.server_default is None, f"{model.__tablename__}.execution_origin must have no default"
        assert isinstance(column.type, String)
        assert column.type.length == 20

    def test_run_model_matches_the_migration(self) -> None:
        self._assert_parity(Run)

    def test_run_daily_fact_model_matches_the_migration(self) -> None:
        self._assert_parity(RunDailyFact)


class TestVocabulary:
    def test_dispatched_is_the_single_origin_value(self) -> None:
        assert EXECUTION_ORIGIN_DISPATCHED == "dispatched"
        assert EXECUTION_ORIGIN_DISPATCHED in EXECUTION_ORIGIN_VALUES
        assert len(EXECUTION_ORIGIN_VALUES) == 1

    def test_origin_is_wider_than_the_vocabulary_deliberately(self) -> None:
        """The COLUMN is nullable (NULL = executed / legacy) while the
        vocabulary covers only the non-NULL members — the pair is the whole
        contract, so a future member must be added HERE, not only at the
        write site."""
        assert Run.__table__.columns["execution_origin"].nullable is True
