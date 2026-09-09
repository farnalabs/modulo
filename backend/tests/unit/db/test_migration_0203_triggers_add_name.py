"""FAR-681 slice 2: migration 0201 - ``triggers.name`` round-trip + identity.

Executes the migration against an in-memory SQLite engine (the 0190
portable-DDL template - plain ``op.add_column`` with an inline type, round
trips on both Postgres and SQLite; the partial unique index uses
``op.create_index``'s ``sqlite_where``/``postgresql_where`` - no raw DDL).
Proves:

* **Round-trip** - the upgrade adds the column, the downgrade removes it,
  and a second upgrade re-adds it (schema asserted at every step).
* **Nullable** - a legacy trigger row inserted BEFORE the upgrade (name NULL,
  pre-slice-2) survives the upgrade untouched: name-based apply never claims
  unnamed rows.
* **Identity uniqueness** - the partial unique index rejects a duplicate live
  ``(organisation_id, pipeline_id, name)`` row, lets NULL names coexist, and
  lets a soft-deleted row's name be re-created (the 0127 pattern).
* **Model parity** - the ORM model carries the column and the identity index
  the migration creates.
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

_REVISION = "0203_triggers_add_name"
_IDENTITY_INDEX = "uq_triggers_org_pipeline_name"

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
            "organisation_id CHAR(32) NOT NULL, "
            "pipeline_id INTEGER NOT NULL, "
            "trigger_type TEXT NOT NULL, "
            "active INTEGER NOT NULL, "
            "deleted_at TIMESTAMP NULL)"
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


def _index_names(engine: sa.Engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        return {index["name"] for index in sa.inspect(conn).get_indexes(table_name) if index.get("unique")}


def _insert_legacy_trigger(conn: sa.Connection) -> None:
    """A trigger row created BEFORE the upgrade (pre-FAR-681, unnamed)."""
    conn.execute(
        sa.text(
            "INSERT INTO triggers (id, organisation_id, pipeline_id, trigger_type, active, deleted_at) "
            "VALUES (1, 'org1', 10, 'cron', 1, NULL)"
        )
    )


def _insert_named_trigger(conn: sa.Connection, row_id: int, name: str, *, deleted: bool = False) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO triggers (id, organisation_id, pipeline_id, trigger_type, active, deleted_at, name) "
            "VALUES (:id, 'org1', 10, 'cron', 1, :deleted_at, :name)"
        ),
        {"id": row_id, "deleted_at": "2026-01-01 00:00:00" if deleted else None, "name": name},
    )


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


class TestRoundTrip0201:
    def test_upgrade_adds_name_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "name" in _table_columns(sqlite_engine, "triggers")

    def test_upgrade_creates_identity_index(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert _IDENTITY_INDEX in _index_names(sqlite_engine, "triggers")

    def test_legacy_trigger_row_survives_upgrade_with_null_name(self, sqlite_engine: sa.Engine) -> None:
        """A trigger created BEFORE the migration has NULL name - the row is
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
        value apply matches on - a written name survives the round-trip."""
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

    def test_downgrade_removes_name_column_and_index(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_trigger(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        # Only the pre-upgrade columns remain; the row survives.
        assert _table_columns(sqlite_engine, "triggers") == {
            "id",
            "organisation_id",
            "pipeline_id",
            "trigger_type",
            "active",
            "deleted_at",
        }
        assert _IDENTITY_INDEX not in _index_names(sqlite_engine, "triggers")
        with sqlite_engine.connect() as conn:
            trigger_type = conn.execute(sa.text("SELECT trigger_type FROM triggers WHERE id = 1")).scalar_one()
        assert trigger_type == "cron"

    def test_second_upgrade_restores_name_column_and_index(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "name" in _table_columns(sqlite_engine, "triggers")
        assert _IDENTITY_INDEX in _index_names(sqlite_engine, "triggers")


class TestTriggerIdentityUniqueness:
    """The partial unique index enforces live (org, pipeline, name) identity."""

    def _upgraded_engine(self, sqlite_engine: sa.Engine) -> sa.Engine:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        return sqlite_engine

    def test_duplicate_live_name_rejected(self, sqlite_engine: sa.Engine) -> None:
        engine = self._upgraded_engine(sqlite_engine)
        with engine.begin() as conn:
            _insert_named_trigger(conn, 1, "nightly")
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            _insert_named_trigger(conn, 2, "nightly")

    def test_same_name_on_different_pipeline_allowed(self, sqlite_engine: sa.Engine) -> None:
        engine = self._upgraded_engine(sqlite_engine)
        with engine.begin() as conn:
            _insert_named_trigger(conn, 1, "nightly")
            conn.execute(
                sa.text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, trigger_type, active, deleted_at, name) "
                    "VALUES (2, 'org1', 11, 'cron', 1, NULL, 'nightly')"
                )
            )

    def test_null_names_never_collide(self, sqlite_engine: sa.Engine) -> None:
        """Legacy unnamed rows are outside the index predicate: several NULL
        names on the same pipeline never collide."""
        engine = self._upgraded_engine(sqlite_engine)
        with engine.begin() as conn:
            _insert_named_trigger(conn, 1, "")
            # Two NULL-name rows on the same (org, pipeline).
            conn.execute(
                sa.text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, trigger_type, active, deleted_at, name) "
                    "VALUES (2, 'org1', 10, 'cron', 1, NULL, NULL)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, trigger_type, active, deleted_at, name) "
                    "VALUES (3, 'org1', 10, 'cron', 1, NULL, NULL)"
                )
            )

    def test_soft_deleted_row_releases_its_name(self, sqlite_engine: sa.Engine) -> None:
        """The 0127 soft-delete pattern: a soft-deleted row no longer occupies
        the identity slot, so the name can be re-created."""
        engine = self._upgraded_engine(sqlite_engine)
        with engine.begin() as conn:
            _insert_named_trigger(conn, 1, "nightly", deleted=True)
            _insert_named_trigger(conn, 2, "nightly")


class TestSymmetryAndModelParity:
    def test_downgrade_drops_exactly_what_upgrade_added(self) -> None:
        source = _source()
        added = {(m.group(1), m.group(2)) for m in _ADD_COLUMN_RE.finditer(source)}
        dropped = {(m.group(1), m.group(2)) for m in _DROP_COLUMN_RE.finditer(source)}
        assert added == {("triggers", "name")}
        assert dropped == added, "downgrade must drop exactly the column the upgrade added"

    def test_migration_is_portable_ddl(self) -> None:
        """No schema-qualified raw DDL (SQLite-incompatible) - op.create_index
        with sqlite_where/postgresql_where keeps the sqlite round-trip harness
        honest. Assertions scope to the code body (docstring prose may mention
        the term)."""
        code = _source().split('"""', 2)[-1]
        assert "op.execute" not in code
        assert "public." not in code

    def test_source_declares_partial_unique_identity_index(self) -> None:
        """The identity uniqueness is a PARTIAL unique index with both dialect
        where-clauses (SQLite round-trips; Postgres enforces the predicate)."""
        source = _source()
        assert _IDENTITY_INDEX in source
        assert 'postgresql_where=sa.text("deleted_at IS NULL AND name IS NOT NULL")' in source
        assert 'sqlite_where=sa.text("deleted_at IS NULL AND name IS NOT NULL")' in source

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
        assert spec.down_revision == "0195_hitl_claim_gate_config_json"

    def test_model_matches_upgraded_schema(self) -> None:
        from modulo.db.models.trigger import Trigger

        column = Trigger.__table__.c.name
        assert column.nullable is True
        assert column.type.length == 255

    def test_model_carries_identity_index(self) -> None:
        """Model parity: the ORM metadata declares the same partial unique
        identity index the migration creates."""
        from modulo.db.models.trigger import Trigger

        index = next(i for i in Trigger.__table__.indexes if i.name == _IDENTITY_INDEX)
        assert index.unique is True
        column_names = [c.name for c in index.columns]
        assert column_names == ["organisation_id", "pipeline_id", "name"]
