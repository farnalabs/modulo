"""FAR-939 chunk 12: migration 0315 — ``policy_gates.evaluation_mode`` SQLite round-trip.

Executes the migration against an in-memory SQLite engine (the 0190
portable-DDL harness). The chunk shipped with the column added by a raw
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` run UNCONDITIONALLY — SQLite
has no ``ADD COLUMN IF NOT EXISTS`` (``near "EXISTS": syntax error``), so
the SQLite path of ``upgrade()`` died before reaching the CHECK branch.
The fix follows the authoritative 0265 pattern: raw existence-gated DDL
under ``_is_postgres()``, column via ``batch_alter_table`` on SQLite.

Proves:

* **Round-trip** — ``upgrade()`` adds the column on SQLite, ``downgrade()``
  drops it, and a second ``upgrade()`` re-adds it (schema asserted at every
  step).
* **Backfill** — a pre-existing gate row reads ``'automatic'`` after the
  upgrade (today's behaviour), and an INSERT that omits the column stores
  ``'automatic'``.
* **CHECK** — ``('automatic', 'human')`` is enforced on SQLite (an
  out-of-vocabulary write raises) and ``'human'`` is accepted.
* **Dialect guard** — the raw ``ADD COLUMN IF NOT EXISTS`` DDL only runs on
  Postgres (source assertion), so it can never reach SQLite again.
* **Symmetry** — the downgrade drops exactly the column the upgrade added
  and never the owning table.
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

_REVISION = "0315_policy_gate_evaluation_mode"
_PRE_UPGRADE_COLUMNS = {"id", "organisation_id", "eval_id", "node_id", "action", "version"}


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
    """A pre-migration ``policy_gates`` shape (no ``evaluation_mode``)."""
    conn.execute(
        sa.text(
            "CREATE TABLE policy_gates ("
            "id INTEGER PRIMARY KEY, "
            "organisation_id TEXT NOT NULL, "
            "eval_id TEXT NOT NULL, "
            "node_id TEXT, "
            "action TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1)"
        )
    )


def _insert_legacy_gate(conn: sa.Connection) -> None:
    """A gate row created BEFORE the upgrade — no ``evaluation_mode``."""
    conn.execute(
        sa.text(
            "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, action, version) "
            "VALUES (1, 'org-1', 'eval-1', 'node-1', 'block', 1)"
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


def _column_mode(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT evaluation_mode FROM policy_gates WHERE id = 1")).scalar_one()


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestSqliteRoundTrip:
    def test_upgrade_adds_evaluation_mode_column(self, sqlite_engine: sa.Engine) -> None:
        """THE regression: pre-fix this raised ``near "EXISTS": syntax
        error`` — SQLite has no ``ADD COLUMN IF NOT EXISTS``."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "evaluation_mode" in _table_columns(sqlite_engine, "policy_gates")

    def test_legacy_gate_row_backfills_to_automatic(self, sqlite_engine: sa.Engine) -> None:
        """A gate written BEFORE the migration reads ``'automatic'`` — the
        NOT NULL DEFAULT backfills every pre-existing row."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert _column_mode(sqlite_engine) == "automatic"

    def test_insert_omitting_the_column_stores_automatic(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, action) "
                    "VALUES (2, 'org-1', 'eval-2', 'warn')"
                )
            )
        with sqlite_engine.connect() as conn:
            mode = conn.execute(sa.text("SELECT evaluation_mode FROM policy_gates WHERE id = 2")).scalar_one()
        assert mode == "automatic"

    def test_check_constraint_enforced_on_sqlite(self, sqlite_engine: sa.Engine) -> None:
        """``('automatic', 'human')``: ``'human'`` lands, anything else raises."""
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text("UPDATE policy_gates SET evaluation_mode = 'human' WHERE id = 1"))
        assert _column_mode(sqlite_engine) == "human"
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            conn.execute(sa.text("UPDATE policy_gates SET evaluation_mode = 'telepathic' WHERE id = 1"))
        # The rejected write rolled back — the row still carries 'human'.
        assert _column_mode(sqlite_engine) == "human"

    def test_downgrade_removes_the_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert_legacy_gate(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        assert "evaluation_mode" not in _table_columns(sqlite_engine, "policy_gates")
        # Only the pre-upgrade columns remain and the row survives.
        assert _table_columns(sqlite_engine, "policy_gates") == _PRE_UPGRADE_COLUMNS
        with sqlite_engine.connect() as conn:
            action = conn.execute(sa.text("SELECT action FROM policy_gates WHERE id = 1")).scalar_one()
        assert action == "block"

    def test_second_upgrade_restores_the_column(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        _run(sqlite_engine, _load_migration(), "downgrade")
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert "evaluation_mode" in _table_columns(sqlite_engine, "policy_gates")


class TestSourceContract:
    def test_raw_add_column_is_postgres_only(self) -> None:
        """The ``ADD COLUMN IF NOT EXISTS`` DDL must sit inside the
        ``_is_postgres()`` guard — SQLite has no such syntax."""
        source = _source()
        match = re.search(r"def upgrade\(\).*?\n(?=def downgrade)", source, re.DOTALL)
        assert match is not None, "upgrade() not found in migration source"
        upgrade_body = match.group(0)
        # The raw SQL literal (not the prose comment) must appear only
        # after the Postgres guard opens.
        raw_ddl = upgrade_body.find('"ALTER TABLE policy_gates ADD COLUMN IF NOT EXISTS')
        assert raw_ddl != -1, "raw ADD COLUMN DDL missing from upgrade()"
        assert upgrade_body.find("_is_postgres()") < raw_ddl
        # And the SQLite path adds the column through batch mode.
        assert "batch_op.add_column" in upgrade_body

    def test_postgres_check_keeps_not_valid_then_validate(self) -> None:
        source = _source()
        assert "_ADD_CHECK_NOT_VALID" in source
        assert "_VALIDATE_CHECK" in source

    def test_downgrade_never_drops_the_table(self) -> None:
        source = _source()
        assert "DROP TABLE" not in source.upper()
        assert "drop_table(" not in source
