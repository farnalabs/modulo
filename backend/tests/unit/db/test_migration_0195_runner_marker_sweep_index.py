"""Structural unit tests for migration 0195_runs_runner_marker_sweep_index (FAR-594 D8 qa F8).

These run WITHOUT a database. They pin the migration's contract: the partial
index shape (``runs (organisation_id) WHERE sandbox_dispatch_state IS NOT
NULL`` — the exact predicate the marker sweep's candidate scan filters on),
the idempotent IF NOT EXISTS form, the revision chain position (down_revision
= 0194, the current head at authoring time), and the deploy-safety markers
(plain CREATE INDEX, Postgres-guarded upgrade/downgrade — the 0193 precedent).
The live-Postgres behaviour (index creation + the sweep's batched cursor scan
using it) is covered by the testcontainers integration suite.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0195_runs_runner_marker_sweep_index"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """Return the migration's executable code, minus the module docstring.

    The docstring legitimately quotes the SQL forms under test (explaining
    them), so assertions must not match that historical prose.
    """
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def test_revision_chain_position() -> None:
    """The migration extends the CURRENT head (0194) — no branch, no collision."""
    module = _load_migration()
    assert module.revision == "0195_runs_runner_marker_sweep_index"
    assert module.down_revision == "0194_uuid_pk_server_defaults"
    assert module.branch_labels is None


def test_partial_index_shape_matches_the_sweep_predicate() -> None:
    """The index is the sweep's candidate-scan predicate: org-scoped, partial
    on ``sandbox_dispatch_state IS NOT NULL`` — the marker sweep filters
    exactly these rows (batched cursor scan on ``id``)."""
    module = _load_migration()
    assert module._TABLE == "runs"
    assert module._PREDICATE == "sandbox_dispatch_state IS NOT NULL"
    create_sql = module._CREATE_INDEX_SQL
    assert "CREATE INDEX IF NOT EXISTS" in create_sql
    assert "ON runs (organisation_id) WHERE sandbox_dispatch_state IS NOT NULL" in create_sql
    assert "CONCURRENTLY" not in create_sql
    drop_sql = module._DROP_INDEX_SQL
    assert "DROP INDEX IF EXISTS ix_runs_org_runner_marker_sweep" in drop_sql


def test_sweep_candidate_scan_matches_the_indexed_predicate() -> None:
    """The sweep's batched candidate SQL filters the SAME predicate the index
    covers (org + marker presence) and pages with a cursor + LIMIT — the
    index serves the probe, the batch bounds the materialisation."""
    from modulo.core.runner_capacity import _SWEEP_CANDIDATE_SQL, SWEEP_CANDIDATE_BATCH

    sql = str(_SWEEP_CANDIDATE_SQL)
    assert "sandbox_dispatch_state IS NOT NULL" in sql
    assert "organisation_id = :oid" in sql
    assert "ORDER BY id" in sql
    assert "LIMIT :batch" in sql
    assert "id > CAST(:after AS uuid)" in sql
    assert SWEEP_CANDIDATE_BATCH == 500


def test_postgres_guard_and_execute_shape() -> None:
    """upgrade()/downgrade() execute raw SQL only on Postgres (the SQLite
    unit-schema path skips silently), matching the runs-index precedent."""
    source = _source_code()
    assert "if not _is_postgres(bind):" in source
    assert "bind.execute(sa.text(_CREATE_INDEX_SQL))" in source
    assert "bind.execute(sa.text(_DROP_INDEX_SQL))" in source
