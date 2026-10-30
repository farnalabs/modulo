"""Unit tests for migration 0340_drop_assistant_ui_driving_flag_row (FAR-2232).

FAR-2232 retired Assistant browser UI driving in code, including the
``assistant_ui_driving`` entry in ``_KNOWN_FLAGS``. Migration 0317's sweep froze
its live-registry snapshot *before* that retirement, so the flag survived it. This
migration removes the leftover catalog row. The tests pin the two properties that
matter:

* **Chain shape** - 0340 revises ``0339_triggers_name_not_null``, the revision id
  matches the filename, and the graph keeps its single linear head (asserted
  through the shared helper, never a hard-coded head value).
* **Idempotent, bounded cleanup** - on a real (in-memory SQLite) connection the
  upgrade deletes ONLY the retired flag, leaves every other catalog row
  untouched, is a no-op when the row (or the whole table) is absent, and the
  downgrade restores the known historical row idempotently without an FK hazard.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_MIGRATION_NAME = "0340_drop_assistant_ui_driving_flag_row"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)
_DOWN_REVISION = "0339_triggers_name_not_null"
_RETIRED_NAME = "assistant_ui_driving"
_OTHER_NAME = "assistant"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scaffold(conn: sa.Connection) -> None:
    """The pre-migration catalog shape plus one retired row and one live row."""
    conn.execute(sa.text("CREATE TABLE tier_catalog (tier_id TEXT PRIMARY KEY)"))
    conn.execute(sa.text("INSERT INTO tier_catalog (tier_id) VALUES ('community')"))
    conn.execute(
        sa.text(
            "CREATE TABLE feature_flag_catalog ("
            "name TEXT PRIMARY KEY, description TEXT, tier_id TEXT NOT NULL "
            "REFERENCES tier_catalog (tier_id), depends_on TEXT, is_active BOOLEAN DEFAULT 1)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
            "VALUES (:name, 'retired', 'community', NULL, 1)"
        ),
        {"name": _RETIRED_NAME},
    )
    conn.execute(
        sa.text(
            "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
            "VALUES (:name, 'live', 'community', NULL, 1)"
        ),
        {"name": _OTHER_NAME},
    )


def _run(engine: sa.Engine, module: ModuleType, fn: str = "upgrade") -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, fn)()


def _names(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(sa.text("SELECT name FROM feature_flag_catalog ORDER BY name")).scalars())


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestChain:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_down_revision_is_0339(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_deletes_the_retired_row(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration())
        assert _RETIRED_NAME not in _names(sqlite_engine)

    def test_keeps_every_other_row(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration())
        assert _names(sqlite_engine) == [_OTHER_NAME]

    def test_is_idempotent(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module)
        assert _names(sqlite_engine) == [_OTHER_NAME]

    def test_absent_row_is_a_no_op(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE tier_catalog (tier_id TEXT PRIMARY KEY)"))
            conn.execute(sa.text("INSERT INTO tier_catalog (tier_id) VALUES ('community')"))
            conn.execute(
                sa.text(
                    "CREATE TABLE feature_flag_catalog ("
                    "name TEXT PRIMARY KEY, description TEXT, tier_id TEXT NOT NULL, "
                    "depends_on TEXT, is_active BOOLEAN)"
                )
            )
        _run(sqlite_engine, _load_migration())
        assert not _names(sqlite_engine)

    def test_missing_table_is_a_no_op(self, sqlite_engine: sa.Engine) -> None:
        _run(sqlite_engine, _load_migration())
        with sqlite_engine.connect() as conn:
            assert not sa.inspect(conn).has_table("feature_flag_catalog")

    def test_logs_the_retired_name_for_audit(self, sqlite_engine: sa.Engine, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="alembic.runtime.migration")
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration())
        record_text = " ".join(record.getMessage() for record in caplog.records)
        assert _RETIRED_NAME in record_text
        assert "1" in record_text


class TestDowngrade:
    def test_restores_the_retired_row(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module, "downgrade")
        assert _names(sqlite_engine) == sorted([_OTHER_NAME, _RETIRED_NAME])

    def test_restore_is_idempotent(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module, "downgrade")
        _run(sqlite_engine, module, "downgrade")
        assert _names(sqlite_engine) == sorted([_OTHER_NAME, _RETIRED_NAME])

    def test_restore_is_gated_on_the_tier_existing(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM tier_catalog"))
        _run(sqlite_engine, module, "downgrade")
        assert _RETIRED_NAME not in _names(sqlite_engine)

    def test_leaves_other_rows_untouched(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module, "downgrade")
        assert _OTHER_NAME in _names(sqlite_engine)
