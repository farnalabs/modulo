"""Migration 0290 — ``scheduled_reports`` due-scan index + ``active`` default.

Executes the migration against an in-memory SQLite engine (the portable-DDL
template — the migration uses only ``op.create_index``, which both Postgres
and SQLite accept; the Postgres-only ``SET DEFAULT`` leg is dialect-guarded
and skipped here, the 0246 precedent). Proves:

* **Revision chain** — revision/down_revision pin to the 0289 head.
* **Round-trip** — the upgrade creates ``ix_scheduled_reports_due_scan``,
  the downgrade drops it, and a second upgrade re-creates it.
* **Index shape** — composite on ``(organisation_id, next_send_at)``.
* **Predicate parity** — the partial predicate matches the every-tick
  due-report scan (``active IS TRUE AND next_send_at IS NOT NULL``).
* **Default leg present** — the Postgres ``SET DEFAULT true`` / ``DROP
  DEFAULT`` statements exist in source (skipped on SQLite by the guard).
* **Model parity** — the ORM model carries the ``active`` server_default
  the migration sets, so ``create_all`` schemas agree with migrated ones.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_REVISION = "0290_scheduled_reports_due_scan"
_INDEX = "ix_scheduled_reports_due_scan"


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
    """A pre-migration ``scheduled_reports`` shape (no due-scan index)."""
    conn.execute(
        sa.text(
            "CREATE TABLE scheduled_reports ("
            "id CHAR(36) PRIMARY KEY, "
            "organisation_id CHAR(36) NOT NULL, "
            "report_type VARCHAR(50) NOT NULL, "
            "active BOOLEAN NOT NULL, "
            "next_send_at TIMESTAMP)"
        )
    )


def _run(engine: sa.Engine, module: ModuleType, fn: str) -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        # Operations.context installs the alembic.op proxy, so the migration
        # module's ``alembic.op`` calls route to THIS engine/connection.
        with Operations.context(context):
            getattr(module, fn)()


def _index_names(engine: sa.Engine, table: str) -> dict[str, dict]:
    with engine.connect() as conn:
        return {idx["name"]: idx for idx in sa.inspect(conn).get_indexes(table) if idx["name"] is not None}


def test_revision_chain_pins_to_0289_head() -> None:
    module = _load_migration()
    assert module.revision == _REVISION
    assert module.down_revision == "0289_pipelines_environment_profile"


def test_upgrade_creates_due_scan_index() -> None:
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _scaffold(conn)
    _run(engine, module, "upgrade")
    indexes = _index_names(engine, "scheduled_reports")
    assert _INDEX in indexes
    assert indexes[_INDEX]["column_names"] == ["organisation_id", "next_send_at"]


def test_downgrade_drops_index_and_reupgrade_restores() -> None:
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _scaffold(conn)
    _run(engine, module, "upgrade")
    _run(engine, module, "downgrade")
    assert _INDEX not in _index_names(engine, "scheduled_reports")
    _run(engine, module, "upgrade")
    assert _INDEX in _index_names(engine, "scheduled_reports")


def test_partial_predicate_matches_due_scan() -> None:
    source = _source()
    assert "active IS TRUE" in source
    assert "next_send_at IS NOT NULL" in source


def test_postgres_default_legs_present_in_source() -> None:
    source = _source()
    assert re.search(r"ALTER COLUMN active SET DEFAULT true", source) is not None
    assert re.search(r"ALTER COLUMN active DROP DEFAULT", source) is not None


def test_model_carries_active_server_default() -> None:
    from modulo.db.models.scheduled_report import ScheduledReport

    server_default = ScheduledReport.__table__.c.active.server_default
    assert server_default is not None
    assert "true" in str(server_default.arg).lower()
