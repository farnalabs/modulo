"""Unit tests for migration 0336_drop_analytics_kill_switch_row (FAR-2179).

FAR-2063 retired the product-analytics licence-enforcement kill switch in code;
this migration removes the inert ``system_config`` row a pre-retirement instance
may still hold. The tests pin the two properties that matter:

* **Chain shape** - 0336 revises ``0335_eval_verdict_contract_stage2``, the
  revision id matches the filename, and the graph keeps its single linear head
  (asserted through the shared helper, never a hard-coded head value).
* **Idempotent, bounded cleanup** - on a real (in-memory SQLite) connection the
  upgrade deletes ONLY the retired key, leaves every other ``system_config`` row
  untouched, is a no-op when the row (or the whole table) is absent, and the
  downgrade does not attempt to reconstruct data it cannot know.
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

_MIGRATION_NAME = "0336_drop_analytics_kill_switch_row"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)
_DOWN_REVISION = "0335_eval_verdict_contract_stage2"
_RETIRED_KEY = "product_analytics_license_enforcement_kill_switch"
_OTHER_KEY = "product_analytics_enabled"
_OTHER_VALUE = '{"keep": true}'


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scaffold(conn: sa.Connection, *, kill_switch_value: str = "true") -> None:
    """The pre-migration table shape plus one retired row and one unrelated row."""
    conn.execute(
        sa.text("CREATE TABLE system_config (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, value TEXT NOT NULL)")
    )
    conn.execute(
        sa.text("INSERT INTO system_config (id, key, value) VALUES (1, :key, :value)"),
        {"key": _RETIRED_KEY, "value": kill_switch_value},
    )
    conn.execute(
        sa.text("INSERT INTO system_config (id, key, value) VALUES (2, :key, :value)"),
        {"key": _OTHER_KEY, "value": _OTHER_VALUE},
    )


def _run(engine: sa.Engine, module: ModuleType, fn: str = "upgrade") -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, fn)()


def _keys(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(sa.text("SELECT key FROM system_config ORDER BY id")).scalars())


def _value(engine: sa.Engine, key: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT value FROM system_config WHERE key = :key"),
            {"key": key},
        ).scalar_one_or_none()


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestChain:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_down_revision_is_0335(self) -> None:
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
        assert _RETIRED_KEY not in _keys(sqlite_engine)

    def test_keeps_every_other_row(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration())
        assert _keys(sqlite_engine) == [_OTHER_KEY]
        assert _value(sqlite_engine, _OTHER_KEY) == _OTHER_VALUE

    def test_is_idempotent(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module)
        assert _keys(sqlite_engine) == [_OTHER_KEY]

    def test_absent_row_is_a_no_op(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE system_config (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, value TEXT NOT NULL)"
                )
            )
            conn.execute(
                sa.text("INSERT INTO system_config (id, key, value) VALUES (1, :key, :value)"),
                {"key": _OTHER_KEY, "value": _OTHER_VALUE},
            )
        _run(sqlite_engine, _load_migration())
        assert _keys(sqlite_engine) == [_OTHER_KEY]

    def test_missing_table_is_a_no_op(self, sqlite_engine: sa.Engine) -> None:
        _run(sqlite_engine, _load_migration())
        with sqlite_engine.connect() as conn:
            assert not sa.inspect(conn).has_table("system_config")

    def test_logs_the_removed_value_for_audit(self, sqlite_engine: sa.Engine, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="alembic.runtime.migration")
        with sqlite_engine.begin() as conn:
            _scaffold(conn, kill_switch_value="false")
        _run(sqlite_engine, _load_migration())
        record_text = " ".join(record.getMessage() for record in caplog.records)
        assert _RETIRED_KEY in record_text
        assert "'false'" in record_text


class TestDowngrade:
    def test_does_not_reconstruct_the_removed_row(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module, "downgrade")
        assert _RETIRED_KEY not in _keys(sqlite_engine)

    def test_leaves_other_rows_untouched(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module)
        _run(sqlite_engine, module, "downgrade")
        assert _value(sqlite_engine, _OTHER_KEY) == _OTHER_VALUE
