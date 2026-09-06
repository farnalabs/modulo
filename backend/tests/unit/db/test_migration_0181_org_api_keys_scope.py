"""FAR-620 Phase 1: migration 0181 — ``org_api_keys.scope`` round-trip.

Executes the migration against an in-memory SQLite engine (the 0126-style
portable-DDL template — the migration uses only plain ``ADD COLUMN`` with an
inline CHECK, which both Postgres and SQLite accept). Proves:

* **Round-trip** — the upgrade adds the column, the downgrade removes it, and
  a second upgrade re-adds it (schema asserted at every step).
* **Pre-existing rows** read ``'org'`` via the server_default (every key
  minted before the column existed is an org-level key).
* **Invalid values rejected** — the inline CHECK refuses anything outside
  ``('org', 'user')``.
* **Silent widening pinned as accepted** — a user-scoped key read after a
  downgrade + re-upgrade reads ``'org'`` (FAR-620 rollback semantics).
* **Symmetry** — the downgrade drops exactly the column the upgrade added,
  and never the owning table.
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

_REVISION = "0181_org_api_keys_scope"

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
    """A pre-migration ``org_api_keys`` shape (no ``scope`` column)."""
    conn.execute(
        sa.text(
            "CREATE TABLE org_api_keys ("
            "id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "account_id INTEGER NOT NULL)"
        )
    )


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


def _insert_key(conn: sa.Connection, *, name: str, scope: str | None = None) -> None:
    if scope is None:
        conn.execute(
            sa.text("INSERT INTO org_api_keys (id, name, role, account_id) VALUES (1, :name, 'operator', 2)"),
            {"name": name},
        )
    else:
        conn.execute(
            sa.text(
                "INSERT INTO org_api_keys (id, name, role, account_id, scope) VALUES (1, :name, 'operator', 2, :scope)"
            ),
            {"name": name, "scope": scope},
        )


def _key_scope(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT scope FROM org_api_keys WHERE id = 1")).scalar_one()


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestRoundTrip0181:
    def test_upgrade_adds_scope_column_with_default(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_key(conn, name="pre-existing")
        migration = _load_migration()
        _run(sqlite_engine, migration, "upgrade")
        assert "scope" in _table_columns(sqlite_engine, "org_api_keys")
        # Pre-existing rows read 'org' via the server_default.
        assert _key_scope(sqlite_engine) == "org"

    def test_downgrade_removes_scope_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_key(conn, name="keeper")
        migration = _load_migration()
        _run(sqlite_engine, migration, "upgrade")
        _run(sqlite_engine, migration, "downgrade")
        # Only the pre-upgrade columns remain; the row survives.
        assert _table_columns(sqlite_engine, "org_api_keys") == {"id", "name", "role", "account_id"}
        with sqlite_engine.connect() as conn:
            name = conn.execute(sa.text("SELECT name FROM org_api_keys WHERE id = 1")).scalar_one()
        assert name == "keeper"

    def test_second_upgrade_restores_scope_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        migration = _load_migration()
        _run(sqlite_engine, migration, "upgrade")
        _run(sqlite_engine, migration, "downgrade")
        _run(sqlite_engine, migration, "upgrade")
        assert "scope" in _table_columns(sqlite_engine, "org_api_keys")


class TestScopeCheckEnforcement:
    def test_user_scope_value_accepted(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.begin() as conn:
            _insert_key(conn, name="user:duncan", scope="user")
        assert _key_scope(sqlite_engine) == "user"

    def test_invalid_scope_value_rejected(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            _insert_key(conn, name="bad", scope="team")


class TestDowngradeWideningPinned:
    def test_user_key_widens_to_org_after_downgrade_reupgrade(self, sqlite_engine: sa.Engine) -> None:
        """The silent widening is ACCEPTED (FAR-620 rollback semantics): a
        user-scoped key whose column is dropped and re-added reads 'org' —
        disabling the feature revokes-broadens on the way back, and the
        flag + auth gate are the compensating controls while deployed."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        migration = _load_migration()
        _run(sqlite_engine, migration, "upgrade")
        with sqlite_engine.begin() as conn:
            _insert_key(conn, name="user:duncan", scope="user")
        _run(sqlite_engine, migration, "downgrade")
        _run(sqlite_engine, migration, "upgrade")
        assert _key_scope(sqlite_engine) == "org"


class TestSymmetryAndModelParity:
    def test_downgrade_drops_exactly_what_upgrade_added(self) -> None:
        source = _source()
        added = {(m.group(1), m.group(2)) for m in _ADD_COLUMN_RE.finditer(source)}
        dropped = {(m.group(1), m.group(2)) for m in _DROP_COLUMN_RE.finditer(source)}
        assert added == {("org_api_keys", "scope")}
        assert dropped == added, "downgrade must drop exactly the column the upgrade added"

    def test_downgrade_never_drops_the_table(self) -> None:
        source = _source()
        assert "DROP TABLE" not in source.upper()
        assert "drop_table(" not in source
        assert "truncate" not in source.lower()
        assert "delete from" not in source.lower()

    def test_migration_is_portable_ddl(self) -> None:
        """No table-level constraint op (SQLite-incompatible) — the CHECK is
        inline in the added column, keeping the sqlite round-trip harness
        honest. Assertions scope to the code body (docstring prose may
        mention the term)."""
        source = _source()
        code = source.split('"""', 2)[-1]
        assert "create_check_constraint" not in code
        assert "sa.CheckConstraint(" in code

    def test_model_matches_upgraded_schema(self) -> None:
        from modulo.db.models.api_key import OrgApiKey

        column = OrgApiKey.__table__.c.scope
        assert isinstance(column.type, sa.String)
        assert column.nullable is False
        names = {c.name for c in OrgApiKey.__table__.constraints if c.name}
        assert "ck_org_api_keys_scope" in names
        assert "ck_org_api_keys_role" in names
