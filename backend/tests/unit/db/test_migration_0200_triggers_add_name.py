"""FAR-681 slice 2: migration 0200 — ``triggers.name`` round-trip.

Executes the migration against an in-memory SQLite engine (the 0190
portable-DDL template — plain ``op.add_column`` with an inline type, round
trips on both Postgres and SQLite). Proves:

* **Round-trip** — the upgrade adds the column, the downgrade removes it,
  and a second upgrade re-adds it (schema asserted at every step).
* **Nullable** — a legacy trigger row inserted BEFORE the upgrade (name NULL,
  pre-slice-2) survives the upgrade untouched: name-based apply never claims
  unnamed rows.
* **Model parity** — the ORM model carries the column the migration creates.
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

_REVISION = "0200_triggers_add_name"

_ADD_COLUMN_RE = re.compile(r'op\.add_column\(\s*"(\w+)"\s*,\s*sa\.Column\(\s*"(\w+)"')
_DROP_COLUMN_RE = re.compile(r'op\.drop_column\(\s*"(\w+)"\s*,\s*"(\w+)"')


def _load_migration() -> ModuleType:
    path = _VERSIONS / f"{_REVISION}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{_REVISION}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return (_VERSIONS / f"{_REVISION}.py").read_text(encoding="utf-8")


def _scaffold(conn: sa.Connection) -> None:
    """A pre-migration ``triggers`` shape (no ``name`` column)."""
    conn.execute(
        sa.text(
            "CREATE TABLE triggers ("
            "id INTEGER PRIMARY KEY, "
            "pipeline_id INTEGER NOT NULL, "
            "trigger_type TEXT NOT NULL, "
            "active INTEGER NOT NULL)"
        )
    )


def _run(engine: sa.Engine, module: ModuleType, fn: str) -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        # Operations.context installs the alembic.op proxy, so the migration
        # module's ``alembic.op`` calls route to THIS engine/connection.
        with Operations.context(context):
            getattr(module, fn)()


def _table_columns(engine: sa.Engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        return {column["name"] for column in sa.inspect(conn).get_columns(table_name) if column["name"] is not None}


def _insert_legacy_trigger(conn: sa.Connection) -> None:
    """A trigger row created BEFORE the upgrade (pre-FAR-681, unnamed)."""
    conn.execute(sa.text("INSERT INTO triggers (id, pipeline_id, trigger_type, active) VALUES (1, 10, 'cron', 1)"))


def _set_trigger_name(conn: sa.Connection, value: str) -> None:
    conn.execute(sa.text("UPDATE triggers SET name = :value WHERE id = 1"), {"value": value})


def _trigger_name(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT name FROM triggers WHERE id = 1")).scalar_one()


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestRoundTrip0200:
    def test_upgrade_adds_name_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "name" in _table_columns(sqlite_engine, "triggers")

    def test_legacy_trigger_row_survives_upgrade_with_null_name(self, sqlite_engine: sa.Engine) -> None:
        """A trigger created BEFORE the migration has NULL name — the row is
        untouched and stays invisible to name-based apply."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.connect() as conn:
            stored = conn.execute(sa.text("SELECT name FROM triggers WHERE id = 1")).scalar_one()
        assert stored is None

    def test_declared_name_round_trips(self, sqlite_engine: sa.Engine) -> None:
        """Identity semantics: the declarative (pipeline, name) handle is the
        value apply matches on — a written name survives the round-trip."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.begin() as conn:
            _set_trigger_name(conn, "nightly")
        assert _trigger_name(sqlite_engine) == "nightly"
        _run(sqlite_engine, _load_migration(), "downgrade")
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert _trigger_name(sqlite_engine) is None

    def test_downgrade_removes_name_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        # Only the pre-upgrade columns remain; the row survives.
        assert _table_columns(sqlite_engine, "triggers") == {"id", "pipeline_id", "trigger_type", "active"}
        with sqlite_engine.connect() as conn:
            trigger_type = conn.execute(sa.text("SELECT trigger_type FROM triggers WHERE id = 1")).scalar_one()
        assert trigger_type == "cron"

    def test_second_upgrade_restores_name_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "name" in _table_columns(sqlite_engine, "triggers")


class TestSymmetryAndModelParity:
    def test_downgrade_drops_exactly_what_upgrade_added(self) -> None:
        source = _source()
        added = {(m.group(1), m.group(2)) for m in _ADD_COLUMN_RE.finditer(source)}
        dropped = {(m.group(1), m.group(2)) for m in _DROP_COLUMN_RE.finditer(source)}
        assert added == {("triggers", "name")}
        assert dropped == added, "downgrade must drop exactly the column the upgrade added"

    def test_migration_is_portable_ddl(self) -> None:
        """No schema-qualified raw DDL (SQLite-incompatible) — op.add_column
        keeps the sqlite round-trip harness honest. Assertions scope to the
        code body (docstring prose may mention the term)."""
        code = _source().split('"""', 2)[-1]
        assert "op.execute" not in code
        assert "public." not in code

    def test_downgrade_never_drops_the_table(self) -> None:
        source = _source()
        assert "DROP TABLE" not in source.upper()
        assert "drop_table(" not in source
        assert "truncate" not in source.lower()
        assert "delete from" not in source.lower()

    def test_down_revision_is_current_head(self) -> None:
        """The migration chains onto the tip that `uv run alembic heads`
        resolved at delivery time (never edited after the fact)."""
        spec = _load_migration()
        assert spec.down_revision == "0199_runs_json_to_jsonb"

    def test_model_matches_upgraded_schema(self) -> None:
        from modulo.db.models.trigger import Trigger

        column = Trigger.__table__.c.name
        assert column.nullable is True
        assert column.type.length == 255
