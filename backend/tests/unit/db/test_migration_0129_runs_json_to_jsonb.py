"""Unit tests for migration 0129_runs_json_to_jsonb (FAR-403).

The migration converts seven ``runs`` columns from ``json`` to ``jsonb``. The
original version did it with seven blocking ``ALTER COLUMN ... TYPE`` statements,
each taking an ACCESS EXCLUSIVE lock + full-table rewrite on the hot, continually
written ``runs`` table — which hung the migration forever and wedged deploys.

These tests are **structural**: they load the migration module, pin the
``_JSON_COLUMNS`` contract (an accidental change to the target columns would
silently convert the wrong set) and assert the emitted SQL is the non-blocking,
resumable design (temp-column ADD + bounded batch UPDATE backfill) rather than
the old blocking ALTER COLUMN TYPE form. They run without a database.

Gap: there is no live-Postgres integration test for 0129 asserting the seven
columns are ``jsonb`` and that running the migration twice is a no-op. That
requires the Testcontainers integration harness (as
``backend/tests/integration/db/test_migration_0126_eval_suite.py`` does); it is
out of scope here and not run by this unit test.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0129_runs_json_to_jsonb"
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

    The docstring legitimately quotes the old blocking ``ALTER COLUMN ... TYPE``
    form to explain why it is being rewritten, so assertions on the SQL emitted
    by the migration must not match that historical prose.
    """
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def test_json_columns_contract_is_unchanged() -> None:
    module = _load_migration()
    assert module._JSON_COLUMNS == (
        "cost_breakdown",
        "node_token_usage",
        "input_payload",
        "outputs_json",
        "node_telemetry_json",
        "guardrail_summary_json",
        "variant_config_snapshot",
    )


def test_metadata_unchanged() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0128_add_fk_lookup_indexes"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_and_downgrade_are_callable() -> None:
    module = _load_migration()
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_upgrade_does_not_emit_blocking_alter_column_type() -> None:
    """The rewritten migration must not use the blocking ALTER COLUMN TYPE form.

    A regression back to ``ALTER COLUMN "cost_breakdown" TYPE jsonb`` would re-
    introduce the ACCESS EXCLUSIVE lock + full-table rewrite that hung deploys.
    """
    assert '" TYPE "' not in _source_code(), "blocking ALTER COLUMN ... TYPE on runs is forbidden"


def test_upgrade_uses_non_blocking_temp_column_and_batch_backfill() -> None:
    """The rewrite must use the additive temp-column + bounded batch UPDATE design."""
    code = _source_code()
    assert 'ADD COLUMN "' in code, "must ADD a temp column rather than ALTER TYPE"
    assert "update(" in code, "backfill must use an UPDATE statement"
    assert "_BATCH_SIZE" in code, "backfill must be bounded (row-level, no table lock)"
    assert ".cast(" in code, "backfill must copy via the lossless ::cast"


def test_helpers_use_dedicated_autocommit_connection() -> None:
    """The migration must run SQL on a dedicated autocommit connection.

    Alembic wraps each migration in ``context.begin_transaction()``. Calling an
    explicit ``.commit()`` on ``op.get_bind()`` closes that transaction and
    breaks every subsequent statement — the root cause of the CI migration break
    (``Can't operate on closed transaction inside context manager``) this fix
    addresses. The migration must instead obtain its own autocommit connection
    from the engine so per-statement commits don't fight alembic's transaction.
    """
    code = _source_code()
    assert ".commit(" not in code, "no explicit .commit() on alembic's connection"
    assert "AUTOCOMMIT" in code, "must use an isolation_level=AUTOCOMMIT connection"
    assert "isolation_level=" in code, "must set isolation_level on the connection"
    assert "engine.connect(" in code, "must connect via the engine, independent of alembic"
