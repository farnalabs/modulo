"""FAR-309 PR C: down-migration rollback-safety for the guardrail trust model.

The guardrail trust-model migrations — ``0113_guardrail_summary``
(``runs.guardrail_summary_json``) and ``0116_guardrail_trust_pr_b``
(``pipeline_snapshots.guardrail_pins_fingerprint``,
``eval_definitions.deleted_at`` / ``deleted_by`` + the
``ix_eval_definitions_deleted_at`` index) — are plain additive migrations
chained onto the linear revision graph. Unlike the schema-reconciliation
migrations (0108/0109/0110), they ARE fully reversible: their DOWNGRADE drops
exactly the columns the upgrade added, so an org rolling the schema back can do
so cleanly with no data loss beyond the feature's own columns.

These tests execute both migrations against an in-memory SQLite engine
(SQLite >= 3.35 supports ``ALTER TABLE ... DROP COLUMN``, and both migrations
use only portable column/index DDL), giving real up/down/up round-trips rather
than source-level string assertions. They prove:

* **Round-trip** — the upgrade adds the trust surface, the downgrade removes
  it, and a second upgrade re-adds it (schema state asserted at every step,
  index included).
* **Data safety** — the downgrade drops ONLY its own columns, never the owning
  tables or their rows: ordinary eval definitions and soft-deleted guardrails
  survive a 0116 downgrade, and the run row survives a 0113 downgrade.
* **Model/migration consistency** — the ORM models (``EvalDefinition``,
  ``PipelineSnapshot``, ``Run``) match the post-upgrade schema, and a drift
  guard fails if a trust column exists on one side but not the other. The
  add/drop column sets must be symmetric — the downgrade must drop exactly
  what the upgrade added — so the models would also match the post-downgrade
  schema were they reverted to their pre-0116/0113 state.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_REVISION_0113 = "0113_guardrail_summary"
_REVISION_0116 = "0116_guardrail_trust_pr_b"

# The guardrail-trust columns owned by these two migrations: ``(table, column)``
# -> the migration whose upgrade MUST create it. The drift guard walks this
# surface in both directions (model <-> migration).
_TRUST_COLUMNS = {
    ("runs", "guardrail_summary_json"): _REVISION_0113,
    ("eval_definitions", "deleted_at"): _REVISION_0116,
    ("eval_definitions", "deleted_by"): _REVISION_0116,
    ("pipeline_snapshots", "guardrail_pins_fingerprint"): _REVISION_0116,
}

_ADD_COLUMN_RE = re.compile(r'op\.add_column\(\s*"(\w+)"\s*,\s*sa\.Column\(\s*"(\w+)"')
_DROP_COLUMN_RE = re.compile(r'op\.drop_column\(\s*"(\w+)"\s*,\s*"(\w+)"')
_CREATE_INDEX_RE = re.compile(r'op\.create_index\(\s*"(\w+)"\s*,\s*"(\w+)"')
_DROP_INDEX_RE = re.compile(r'op\.drop_index\(\s*"(\w+)"\s*,\s*table_name="(\w+)"')


def _load_migration(revision: str) -> ModuleType:
    path = _VERSIONS / f"{revision}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(revision: str) -> str:
    return (_VERSIONS / f"{revision}.py").read_text(encoding="utf-8")


def _migration_columns(source: str, op_name: str) -> set[tuple[str, str]]:
    pattern = _ADD_COLUMN_RE if op_name == "add_column" else _DROP_COLUMN_RE
    return {(match.group(1), match.group(2)) for match in pattern.finditer(source)}


def _index_ops(source: str, op_name: str) -> set[tuple[str, str]]:
    pattern = _CREATE_INDEX_RE if op_name == "create_index" else _DROP_INDEX_RE
    return {(match.group(1), match.group(2)) for match in pattern.finditer(source)}


def _assert_drift_free(
    model_columns: dict[str, set[str]],
    migration_columns: dict[str, set[tuple[str, str]]],
    trust_columns: dict[tuple[str, str], str] | None = None,
) -> None:
    """Raise on any trust-surface drift between the ORM models and the migrations.

    Checks both directions: every ``_TRUST_COLUMNS`` column must exist on its
    ORM model AND be created by the migration that owns it; every column a
    migration creates must exist on its ORM model. Any drift on either side —
    a trust column removed from a migration, a migration column absent from
    its model, or a model column whose owning migration stopped creating it —
    raises AssertionError. The mismatched-input cases make this the
    prove-the-fix harness: feed a synthetic mismatch and it must fail.
    """
    trust_columns = trust_columns or _TRUST_COLUMNS
    all_migration_columns = {
        (table, column) for revision_columns in migration_columns.values() for table, column in revision_columns
    }
    for (table, column), revision in trust_columns.items():
        assert column in model_columns[table], f"model {table} is missing trust column {column}"
        assert (table, column) in migration_columns[revision], f"migration {revision} must create {table}.{column}"
    for table, column in all_migration_columns:
        assert column in model_columns[table], f"{table}.{column} created by a migration but absent from the ORM model"


def _scaffold(conn: sa.Connection) -> None:
    conn.execute(sa.text("CREATE TABLE eval_definitions (id INTEGER PRIMARY KEY, name TEXT)"))
    conn.execute(sa.text("CREATE TABLE pipeline_snapshots (id INTEGER PRIMARY KEY)"))
    conn.execute(sa.text("CREATE TABLE runs (id INTEGER PRIMARY KEY, input_payload TEXT)"))


def _run(engine: sa.Engine, module: ModuleType, fn: str) -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        # Operations.context installs the alembic.op proxy, so the migration
        # module's ``alembic.op`` calls route to THIS engine/connection.
        with Operations.context(context):
            getattr(module, fn)()


def _table_columns(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {column["name"] for column in sa.inspect(conn).get_columns(table) if column["name"] is not None}


def _index_names(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {index["name"] for index in sa.inspect(conn).get_indexes(table) if index["name"] is not None}


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestRoundTrip0116:
    """Up/down/up round-trip of 0116 on a real (in-memory) engine."""

    def test_upgrade_adds_trust_surface(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(_REVISION_0116), "upgrade")
        assert {"deleted_at", "deleted_by"} <= _table_columns(sqlite_engine, "eval_definitions")
        assert "guardrail_pins_fingerprint" in _table_columns(sqlite_engine, "pipeline_snapshots")
        assert "ix_eval_definitions_deleted_at" in _index_names(sqlite_engine, "eval_definitions")

    def test_downgrade_removes_trust_surface(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(_REVISION_0116), "upgrade")
        _run(sqlite_engine, _load_migration(_REVISION_0116), "downgrade")
        # Only the pre-upgrade columns remain — nothing else was dropped.
        assert {"id", "name"} == _table_columns(sqlite_engine, "eval_definitions")
        assert {"id"} == _table_columns(sqlite_engine, "pipeline_snapshots")
        assert "ix_eval_definitions_deleted_at" not in _index_names(sqlite_engine, "eval_definitions")

    def test_second_upgrade_restores_trust_surface(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        migration = _load_migration(_REVISION_0116)
        _run(sqlite_engine, migration, "upgrade")
        _run(sqlite_engine, migration, "downgrade")
        _run(sqlite_engine, migration, "upgrade")
        assert {"deleted_at", "deleted_by"} <= _table_columns(sqlite_engine, "eval_definitions")
        assert "guardrail_pins_fingerprint" in _table_columns(sqlite_engine, "pipeline_snapshots")
        assert "ix_eval_definitions_deleted_at" in _index_names(sqlite_engine, "eval_definitions")


class TestRoundTrip0113:
    """Up/down/up round-trip of 0113 on a real (in-memory) engine."""

    def test_0113_upgrade_down_up_round_trip(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        migration = _load_migration(_REVISION_0113)
        _run(sqlite_engine, migration, "upgrade")
        assert "guardrail_summary_json" in _table_columns(sqlite_engine, "runs")
        _run(sqlite_engine, migration, "downgrade")
        assert "guardrail_summary_json" not in _table_columns(sqlite_engine, "runs")
        _run(sqlite_engine, migration, "upgrade")
        assert "guardrail_summary_json" in _table_columns(sqlite_engine, "runs")


class TestDowngradeDataSafety:
    """Dropping the trust columns must never lose non-guardrail data."""

    def test_0116_downgrade_preserves_eval_definition_rows(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            conn.execute(sa.text("INSERT INTO eval_definitions (id, name) VALUES (1, 'keeper')"))
        _run(sqlite_engine, _load_migration(_REVISION_0116), "upgrade")
        # A soft-deleted guardrail row with trust data stamped.
        with sqlite_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO eval_definitions (id, name, deleted_at, deleted_by) "
                    "VALUES (2, 'soft_deleted_guardrail', '2026-08-18 00:00:00', "
                    "'0123456789abcdef0123456789abcdef')"
                )
            )
        _run(sqlite_engine, _load_migration(_REVISION_0116), "downgrade")
        with sqlite_engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT id, name FROM eval_definitions ORDER BY id")).all()
        assert [(row[0], row[1]) for row in rows] == [(1, "keeper"), (2, "soft_deleted_guardrail")]

    def test_0113_downgrade_preserves_run_rows(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            conn.execute(sa.text("INSERT INTO runs (id, input_payload) VALUES (1, '{}')"))
        migration = _load_migration(_REVISION_0113)
        _run(sqlite_engine, migration, "upgrade")
        # A run carrying the feature's own data: the downgrade must drop the
        # column without losing the run row itself.
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text('UPDATE runs SET guardrail_summary_json = \'{"bound": 1, "passed": 1}\' WHERE id = 1'))
        _run(sqlite_engine, migration, "downgrade")
        with sqlite_engine.connect() as conn:
            assert conn.execute(sa.text("SELECT id FROM runs")).scalar_one() == 1

    def test_downgrade_drops_exactly_what_upgrade_added(self) -> None:
        # Symmetry invariant: the downgrade drops exactly the columns AND
        # indexes the upgrade added — nothing more, and never the owning table.
        # Index symmetry matters on SQLite, where DROP COLUMN of an indexed
        # column fails unless the index was already dropped by the downgrade.
        for revision in (_REVISION_0113, _REVISION_0116):
            source = _source(revision)
            assert _migration_columns(source, "add_column") == _migration_columns(source, "drop_column"), (
                f"migration {revision} downgrade must drop exactly its upgrade columns"
            )
            assert _index_ops(source, "create_index") == _index_ops(source, "drop_index"), (
                f"migration {revision} downgrade must drop exactly its upgrade indexes"
            )

    def test_downgrades_never_drop_the_tables(self) -> None:
        for revision in (_REVISION_0113, _REVISION_0116):
            source = _source(revision)
            assert "DROP TABLE" not in source.upper()
            assert "drop_table(" not in source
            assert "truncate" not in source.lower()
            assert "delete from" not in source.lower()


class TestModelMigrationConsistency:
    """The ORM models must match the schema at each revision."""

    def test_eval_definition_model_matches_0116_schema(self) -> None:
        from modulo.db.models.eval_definition import EvalDefinition

        columns = EvalDefinition.__table__.c
        assert "deleted_at" in columns
        assert "deleted_by" in columns
        assert isinstance(columns["deleted_at"].type, sa.DateTime)
        assert columns["deleted_at"].type.timezone is True
        assert isinstance(columns["deleted_by"].type, sa.Uuid)

    def test_pipeline_snapshot_model_matches_0116_schema(self) -> None:
        from modulo.db.models.pipeline_snapshot import PipelineSnapshot

        column = PipelineSnapshot.__table__.c.guardrail_pins_fingerprint
        assert isinstance(column.type, sa.String)
        assert column.type.length == 64
        assert column.nullable

    def test_run_model_matches_0113_schema(self) -> None:
        from modulo.db.models.run import Run

        column = Run.__table__.c.guardrail_summary_json
        assert isinstance(column.type, sa.JSON)
        assert column.nullable

    def test_drift_guard_trust_surface_consistent_both_directions(self) -> None:
        # A future mismatch is caught here: a trust column that exists on one
        # side (model OR migration) but not the other fails this test.
        from modulo.db.models.eval_definition import EvalDefinition
        from modulo.db.models.pipeline_snapshot import PipelineSnapshot
        from modulo.db.models.run import Run

        model_columns = {
            "eval_definitions": set(EvalDefinition.__table__.c.keys()),
            "pipeline_snapshots": set(PipelineSnapshot.__table__.c.keys()),
            "runs": set(Run.__table__.c.keys()),
        }
        migration_columns = {
            revision: _migration_columns(_source(revision), "add_column")
            for revision in (_REVISION_0113, _REVISION_0116)
        }
        _assert_drift_free(model_columns, migration_columns)

    def test_drift_guard_catches_model_migration_mismatch(self) -> None:
        # Prove-the-fix: the drift guard must FAIL when the trust surface
        # drifts. Feed synthetic mismatches — a trust column removed from its
        # owning migration, and a migration column absent from its model —
        # and assert the guard raises instead of silently passing.
        from modulo.db.models.eval_definition import EvalDefinition
        from modulo.db.models.pipeline_snapshot import PipelineSnapshot
        from modulo.db.models.run import Run

        model_columns = {
            "eval_definitions": set(EvalDefinition.__table__.c.keys()),
            "pipeline_snapshots": set(PipelineSnapshot.__table__.c.keys()),
            "runs": set(Run.__table__.c.keys()),
        }
        real_migration_columns = {
            revision: _migration_columns(_source(revision), "add_column")
            for revision in (_REVISION_0113, _REVISION_0116)
        }
        # (a) a migration stops creating a trust column (e.g. a column dropped
        # from 0116's upgrade) — the downgrade symmetry guard would also fire.
        migration_removed = {rev: set(cols) for rev, cols in real_migration_columns.items()}
        migration_removed[_REVISION_0116].discard(("eval_definitions", "deleted_at"))
        with pytest.raises(AssertionError, match=r"must create eval_definitions\.deleted_at"):
            _assert_drift_free(model_columns, migration_removed)
        # (b) the ORM model loses a column a migration still creates.
        model_missing = {table: set(cols) for table, cols in model_columns.items()}
        model_missing["runs"].discard("guardrail_summary_json")
        with pytest.raises(AssertionError, match="is missing trust column guardrail_summary_json"):
            _assert_drift_free(model_missing, real_migration_columns)
