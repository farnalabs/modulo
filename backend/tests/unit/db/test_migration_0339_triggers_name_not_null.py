"""FAR-2227: migration 0339 - backfill NULL trigger names, then ``name`` NOT NULL.

Executes the migration against an in-memory SQLite engine (batch mode
recreates the table there; Postgres emits a plain ``ALTER COLUMN``). Proves:

* NULL names (live and soft-deleted) are backfilled with
  ``<trigger_type>-<first 8 hex chars of the id>`` and the column becomes
  NOT NULL;
* a backfilled candidate that collides with an existing live name on the same
  pipeline falls back to the full hex id, so the unique identity holds;
* explicit names are never touched;
* the downgrade makes the column nullable again;
* the ORM model carries the NOT NULL column.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from modulo.db.models.trigger import Trigger

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_REVISION = "0339_triggers_name_not_null"

_ID_A = "aaaaaaaa11112222333344445555aaaa"
_ID_B = "bbbbbbbb11112222333344445555bbbb"
_ID_C = "cccccccc11112222333344445555cccc"
_ID_D = "dddddddd11112222333344445555dddd"


def _load_migration() -> ModuleType:
    path = _VERSIONS / f"{_REVISION}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{_REVISION}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: sa.Engine, fn: str) -> None:
    module = _load_migration()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, fn)()


def _scaffold(conn: sa.Connection) -> None:
    """The post-0203 ``triggers`` shape: nullable ``name`` + the identity index."""
    conn.execute(
        sa.text(
            "CREATE TABLE triggers ("
            "id CHAR(32) PRIMARY KEY, "
            "organisation_id CHAR(32) NOT NULL, "
            "pipeline_id CHAR(32) NOT NULL, "
            "name VARCHAR(255) NULL, "
            "trigger_type VARCHAR(20) NOT NULL, "
            "deleted_at TIMESTAMP NULL)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_triggers_org_pipeline_name ON triggers (organisation_id, pipeline_id, name) "
            "WHERE deleted_at IS NULL AND name IS NOT NULL"
        )
    )


def _insert(
    conn: sa.Connection,
    row_id: str,
    trigger_type: str,
    name: str | None,
    *,
    deleted: bool = False,
    pipeline: str = "p1",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO triggers (id, organisation_id, pipeline_id, name, trigger_type, deleted_at) "
            "VALUES (:id, 'org1', :pipeline, :name, :trigger_type, :deleted_at)"
        ),
        {
            "id": row_id,
            "pipeline": pipeline,
            "name": name,
            "trigger_type": trigger_type,
            "deleted_at": "2026-01-01 00:00:00" if deleted else None,
        },
    )


def _names(engine: sa.Engine) -> dict[str, str | None]:
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(sa.text("SELECT id, name FROM triggers"))}


def _name_is_nullable(engine: sa.Engine) -> bool:
    with engine.connect() as conn:
        columns = {c["name"]: c for c in sa.inspect(conn).get_columns("triggers")}
    return bool(columns["name"]["nullable"])


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestBackfill:
    def test_null_names_get_type_and_short_id(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_A, "webhook", None)
            _insert(conn, _ID_B, "cron", None)
        _run(sqlite_engine, "upgrade")
        assert _names(sqlite_engine) == {_ID_A: "webhook-aaaaaaaa", _ID_B: "cron-bbbbbbbb"}

    def test_soft_deleted_null_names_are_backfilled(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_A, "manual", None, deleted=True)
        _run(sqlite_engine, "upgrade")
        assert _names(sqlite_engine)[_ID_A] == "manual-aaaaaaaa"

    def test_explicit_names_are_untouched(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_A, "cron", "nightly")
            _insert(conn, _ID_B, "cron", None)
        _run(sqlite_engine, "upgrade")
        assert _names(sqlite_engine)[_ID_A] == "nightly"

    def test_collision_with_existing_live_name_uses_full_id(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_D, "cron", "cron-aaaaaaaa")
            _insert(conn, _ID_A, "cron", None)
        _run(sqlite_engine, "upgrade")
        assert _names(sqlite_engine)[_ID_A] == f"cron-{_ID_A}"

    def test_same_short_id_on_other_pipeline_is_not_a_collision(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_D, "cron", "cron-aaaaaaaa", pipeline="p2")
            _insert(conn, _ID_A, "cron", None, pipeline="p1")
        _run(sqlite_engine, "upgrade")
        assert _names(sqlite_engine)[_ID_A] == "cron-aaaaaaaa"


class TestNotNull:
    def test_name_column_becomes_not_null(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_A, "cron", None)
        _run(sqlite_engine, "upgrade")
        assert _name_is_nullable(sqlite_engine) is False

    def test_null_insert_rejected_after_upgrade(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, "upgrade")
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            _insert(conn, _ID_C, "cron", None)

    def test_downgrade_makes_name_nullable_and_keeps_values(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            _insert(conn, _ID_A, "cron", None)
        _run(sqlite_engine, "upgrade")
        _run(sqlite_engine, "downgrade")
        assert _name_is_nullable(sqlite_engine) is True
        assert _names(sqlite_engine)[_ID_A] == "cron-aaaaaaaa"

    def test_upgrade_keeps_live_name_uniqueness(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, "upgrade")
        with sqlite_engine.begin() as conn:
            _insert(conn, _ID_A, "cron", "nightly")
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            _insert(conn, _ID_B, "cron", "nightly")


class TestModelParity:
    def test_model_name_column_is_not_null(self) -> None:
        assert Trigger.__table__.c.name.nullable is False
