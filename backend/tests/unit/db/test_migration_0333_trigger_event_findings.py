"""Structural and model-parity tests for migration 0333."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import sqlalchemy as sa

from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.trigger_event_finding import TriggerEventFinding
from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0333_trigger_event_findings"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    def __init__(self) -> None:
        self.tables: dict[str, sa.Table] = {}
        self.sql: list[str] = []

    def get_bind(self) -> MagicMock:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    def execute(self, statement: object, *_args: object, **_kwargs: object) -> None:
        self.sql.append(str(statement))

    def create_unique_constraint(self, *_args: object, **_kwargs: object) -> None:
        pass

    def create_table(self, name: str, *args: object, **_kwargs: object) -> None:
        metadata = sa.MetaData()
        sa.Table("organisations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
        sa.Table("trigger_events", metadata, sa.Column("id", sa.Uuid()), sa.Column("organisation_id", sa.Uuid()))
        sa.Table("evals", metadata, sa.Column("id", sa.Uuid()), sa.Column("organisation_id", sa.Uuid()))
        sa.Table("accounts", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
        self.tables[name] = sa.Table(name, metadata, *args)  # type: ignore[arg-type]

    def drop_table(self, *_args: object, **_kwargs: object) -> None:
        pass

    def drop_constraint(self, *_args: object, **_kwargs: object) -> None:
        pass


def _run_upgrade() -> _Recorder:
    migration = _load_migration()
    recorder = _Recorder()
    with patch.object(migration, "op", recorder):
        migration.upgrade()
    return recorder


def test_migration_chain_is_linear_and_revision_is_current() -> None:
    migration = _load_migration()
    assert_single_linear_head(script_directory())
    assert migration.revision == _MIGRATION_NAME
    assert migration.down_revision == "0332_eval_mute_policy"


def test_finding_id_server_default_matches_migration() -> None:
    column = TriggerEventFinding.__table__.c.id
    assert column.server_default is not None
    assert str(column.server_default.arg) == "gen_random_uuid()"
    migration_text = _MIGRATION_PATH.read_text()
    assert 'sa.text("gen_random_uuid()")' in migration_text


def test_trigger_event_composite_unique_key_is_model_owned_and_migration_managed() -> None:
    constraint = next(
        constraint
        for constraint in TriggerEvent.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint) and constraint.name == "uq_trigger_events_id_organisation_id"
    )
    assert tuple(column.name for column in constraint.columns) == ("id", "organisation_id")
    migration_text = _MIGRATION_PATH.read_text()
    assert "op.create_unique_constraint(" in migration_text
    assert (
        'op.drop_constraint("uq_trigger_events_id_organisation_id", "trigger_events", type_="unique")' in migration_text
    )
    assert "DROP CONSTRAINT uq_trigger_events_id_organisation_id" not in migration_text


def test_finding_table_model_has_migration_columns_and_constraints() -> None:
    table = _run_upgrade().tables["trigger_event_findings"]
    assert {column.name for column in table.columns} == {
        "id",
        "organisation_id",
        "trigger_event_id",
        "eval_id",
        "passed",
        "outcome",
        "score",
        "detail",
        "resolution",
        "resolution_reason",
        "resolved_at",
        "resolved_by",
        "created_at",
        "updated_at",
    }
    assert table.c.id.server_default is not None
    assert str(table.c.id.server_default.arg) == "gen_random_uuid()"
    unique_names = {constraint.name for constraint in table.constraints if isinstance(constraint, sa.UniqueConstraint)}
    assert unique_names == {
        "uq_trigger_event_findings_id_organisation_id",
        "uq_trigger_event_findings_org_event_eval",
    }
    check_names = {constraint.name for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)}
    assert check_names == {"ck_trigger_event_findings_resolution"}


def test_downgrade_only_drops_constraint_owned_by_0333() -> None:
    migration = _load_migration()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    recorder = MagicMock()
    recorder.get_bind.return_value = bind
    with patch.object(migration, "op", recorder):
        migration.downgrade()
    recorder.drop_constraint.assert_called_once_with(
        "uq_trigger_events_id_organisation_id", "trigger_events", type_="unique"
    )
    assert all("DROP CONSTRAINT" not in str(call) for call in recorder.execute.call_args_list)
