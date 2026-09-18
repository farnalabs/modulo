"""FAR-634: migration 0195 — ``hitl_claims.gate_config_json`` round-trip.

Executes the migration against an in-memory SQLite engine (the 0190-style
portable-DDL template — the migration is a plain ``op.add_column`` with an
inline type, portable across Postgres and SQLite; Postgres takes the ``jsonb``
branch, non-Postgres dialects the generic-JSON branch). Proves:

* **Round-trip** — the upgrade adds the column, the downgrade removes it, and
  a second upgrade re-adds it (schema asserted at every step).
* **Nullable** — a legacy gate row inserted BEFORE the upgrade (config NULL)
  survives the upgrade untouched, and a JSON document inserted after the
  upgrade round-trips as a JSON value (SQLite stores jsonb as TEXT; the
  contract under test is "any JSON value is preserved").
* **Symmetry** — the downgrade drops exactly the column the upgrade added,
  and never the owning table.
* **Model parity** — the ORM model carries the column the migration creates.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_REVISION = "0195_hitl_claim_gate_config_json"

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
    """A pre-migration ``hitl_claims`` shape (no ``gate_config_json`` column)."""
    conn.execute(
        sa.text(
            "CREATE TABLE hitl_claims ("
            "id INTEGER PRIMARY KEY, "
            "run_id INTEGER NOT NULL, "
            "gate_id TEXT NOT NULL, "
            "decision_payload TEXT, "
            "context_json TEXT)"
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


def _insert_legacy_gate(conn: sa.Connection) -> None:
    """A gate row created BEFORE the upgrade — no stamped config (pre-FAR-634)."""
    conn.execute(sa.text("INSERT INTO hitl_claims (id, run_id, gate_id) VALUES (1, 10, 'hitl_gate_a_b')"))


def _gate_config(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT gate_config_json FROM hitl_claims WHERE id = 1")).scalar_one()


def _set_gate_config(conn: sa.Connection, value: str) -> None:
    conn.execute(
        sa.text("UPDATE hitl_claims SET gate_config_json = :value WHERE id = 1"),
        {"value": value},
    )


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestRoundTrip0195:
    def test_upgrade_adds_gate_config_json_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "gate_config_json" in _table_columns(sqlite_engine, "hitl_claims")

    def test_legacy_gate_row_survives_upgrade_with_null_config(self, sqlite_engine: sa.Engine) -> None:
        """A gate that fired BEFORE the migration has NULL config — the row is
        untouched (the resolver's snapshot/live walk covers it)."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert _gate_config(sqlite_engine) is None

    def test_json_document_round_trips(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        payload = json.dumps({"human_only": True, "label": "Sign-off"})
        with sqlite_engine.begin() as conn:
            _set_gate_config(conn, payload)
        assert _gate_config(sqlite_engine) is not None
        assert json.loads(str(_gate_config(sqlite_engine))) == {
            "human_only": True,
            "label": "Sign-off",
        }

    def test_downgrade_removes_gate_config_json_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        # Only the pre-upgrade columns remain; the row survives.
        assert _table_columns(sqlite_engine, "hitl_claims") == {
            "id",
            "run_id",
            "gate_id",
            "decision_payload",
            "context_json",
        }
        with sqlite_engine.connect() as conn:
            gate_id = conn.execute(sa.text("SELECT gate_id FROM hitl_claims WHERE id = 1")).scalar_one()
        assert gate_id == "hitl_gate_a_b"

    def test_second_upgrade_restores_gate_config_json_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "gate_config_json" in _table_columns(sqlite_engine, "hitl_claims")


class TestSymmetryAndModelParity:
    def test_downgrade_drops_exactly_what_upgrade_added(self) -> None:
        source = _source()
        # The two upgrade matches are the dialect branches (Postgres jsonb vs
        # generic JSON) — mutually exclusive at runtime, so the PAIR SET is
        # the contract: both add the same (table, column).
        added = {(m.group(1), m.group(2)) for m in _ADD_COLUMN_RE.finditer(source)}
        dropped = {(m.group(1), m.group(2)) for m in _DROP_COLUMN_RE.finditer(source)}
        assert added == {("hitl_claims", "gate_config_json")}
        assert dropped == added, "downgrade must drop exactly the column the upgrade added"

    def test_both_dialect_branches_present(self) -> None:
        """Postgres takes the jsonb branch; non-Postgres dialects take the
        generic-JSON branch — both must exist in the source."""
        source = _source()
        assert source.count('sa.Column("gate_config_json", JSONB(), nullable=True)') == 1
        assert source.count('sa.Column("gate_config_json", sa.JSON(), nullable=True)') == 1

    def test_migration_is_portable_ddl(self) -> None:
        """No schema-qualified raw ALTER (SQLite-incompatible) — op.add_column
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

    def test_model_matches_upgraded_schema(self) -> None:
        from modulo.db.models.hitl_claim import HitlClaim

        column = HitlClaim.__table__.c.gate_config_json
        assert column.nullable is True
