"""Unit tests for migration 0227_env_profiles_initialisation_strategy_check (FAR-802).

Structural + SQLite round-trip (no Postgres/Testcontainers needed):

* the chain is pinned (revision -> 0226_agents_json_to_jsonb, the main-chain head
  this revision extends);
* the migration's hardcoded ``_VOCABULARY`` stays in sync with the model's
  ``INITIALISATION_STRATEGIES`` single source of truth — the model header
  declares it the source of truth and forbids hardcoding elsewhere, yet the
  migration MUST hardcode to express the CHECK; the sync guarantee therefore
  comes from this drift-guard (same pattern as test_trigger_event_vocabulary);
* the migration's CHECK SQL and constraint name stay in sync with the ORM
  ``CheckConstraint`` on ``EnvironmentProfile``;
* the Postgres path adds the CHECK ``NOT VALID`` then ``VALIDATE CONSTRAINT``
  under a ``pg_constraint`` existence guard (idempotent re-run safety);
* the SQLite path applies the CHECK via batch mode;
* the CHECK actually rejects a disallowed ``initialisation_strategy`` value and
  accepts an allowed one (SQLite round-trip exercising the migration's upgrade).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from modulo.db.models.environment_profile import INITIALISATION_STRATEGIES

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0227_env_profiles_initialisation_strategy_check"
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
    """Return the migration's executable code, minus the module docstring."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def test_metadata_pins_chain() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0226_agents_json_to_jsonb"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_vocabulary_drift_guard_matches_model() -> None:
    """The migration MUST hardcode the vocabulary (CHECK DDL cannot read a
    Python constant), but that hardcoded list must never diverge from the
    model's ``INITIALISATION_STRATEGIES`` single source of truth. A value added
    to one side and not the other breaks the constraint/model contract."""
    module = _load_migration()
    assert set(module._VOCABULARY) == INITIALISATION_STRATEGIES, (
        "migration 0227 vocabulary drifted from INITIALISATION_STRATEGIES — "
        "keep them in sync (model is the source of truth)"
    )


def test_check_sql_and_name_drift_guard_matches_model() -> None:
    """The migration's CHECK expression and constraint name must match the ORM
    ``CheckConstraint`` declared on ``EnvironmentProfile`` — same vocabulary,
    same name, or the DB constraint and the ORM contract disagree."""
    from modulo.db.models.environment_profile import (
        EnvironmentProfile,
        _initialisation_strategy_check_sql,
    )

    module = _load_migration()
    constraint = next(
        c
        for c in EnvironmentProfile.__table_args__
        if getattr(c, "name", None) == "ck_env_profiles_initialisation_strategy"
    )
    assert getattr(constraint, "name", None) == module._CONSTRAINT_NAME
    # CHECK expression parity (sorted vocabulary => identical text).
    assert _initialisation_strategy_check_sql() == module._CHECK_EXPR


def test_postgres_path_adds_not_valid_then_validates_guarded() -> None:
    """Postgres: the CHECK is added NOT VALID (instant DDL, no full-table scan
    under ACCESS EXCLUSIVE on the hot environment_profiles table) and validated
    in a separate guarded step (SHARE UPDATE EXCLUSIVE — non-blocking for
    INSERTs). The add is guarded on pg_constraint existence so a partial re-run
    is a no-op (matches the 0219 idempotency pattern)."""
    code = _source_code()
    assert "ck_env_profiles_initialisation_strategy" in code
    assert "NOT VALID" in code
    assert "VALIDATE CONSTRAINT" in code
    assert "pg_constraint" in code


def test_sqlite_path_uses_batch_alter_table() -> None:
    """SQLite / non-Postgres dialects apply the CHECK via Alembic batch mode
    (the only dialect-portable way to add a CHECK to an existing table)."""
    code = _source_code()
    assert "batch_alter_table" in code
    assert "create_check_constraint" in code


def test_check_rejects_disallowed_value_and_accepts_allowed_sqlite_roundtrip() -> None:
    """Prove-the-fix (SQLite round-trip): run the migration's upgrade() against
    an in-memory SQLite table and assert the resulting CHECK rejects a value
    outside the vocabulary while accepting one inside it. Exercises the actual
    migration upgrade path (the SQLite batch branch), not just source text."""
    from typing import Any

    import alembic
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    module = _load_migration()
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE environment_profiles ("
                    "id INTEGER PRIMARY KEY, "
                    "initialisation_strategy VARCHAR(30) NOT NULL DEFAULT 'git_clone')"
                )
            )
            conn.commit()

        with engine.connect() as mig_conn:
            ctx = MigrationContext.configure(mig_conn)
            op_ctx = Operations(ctx)
            # Point the migration's `from alembic import op` at our live context
            # by rebinding the package-level proxy and re-loading the module so
            # its import-time binding picks up the new op.
            op_attr = "op"
            original_op: Any = getattr(alembic, op_attr)
            setattr(alembic, op_attr, op_ctx)
            try:
                module = _load_migration()
                module.upgrade()
                mig_conn.commit()
            finally:
                setattr(alembic, op_attr, original_op)

        # Allowed values are accepted.
        with engine.connect() as ok_conn:
            ok_conn.execute(
                text("INSERT INTO environment_profiles (id, initialisation_strategy) VALUES (1, 'git_clone')")
            )
            ok_conn.execute(
                text("INSERT INTO environment_profiles (id, initialisation_strategy) VALUES (2, 'managed_inputs')")
            )
            ok_conn.commit()

        # A disallowed value is rejected by the CHECK constraint.
        with engine.connect() as bad_conn, bad_conn.begin(), pytest.raises(IntegrityError):
            bad_conn.execute(
                text("INSERT INTO environment_profiles (id, initialisation_strategy) VALUES (3, 'nonsense')")
            )
    finally:
        # Close the pooled SingletonThreadPool connection to avoid a
        # ResourceWarning on the in-memory SQLite engine at GC time.
        engine.dispose()
