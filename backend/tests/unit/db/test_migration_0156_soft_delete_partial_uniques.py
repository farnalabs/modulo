"""Unit tests for migration 0156_add_soft_delete_partial_uniques parity.

Structural: load the migration module and assert its contract without a
database, then pin model/migration parity for the partial unique indexes that
still have an ORM model (``saved_views`` no longer does — its table remains in
migration history, but the Saved Views feature and its model were retired):

* the chain is pinned (revision -> 0175_dedupe_soft_delete_names) so the
  pre-commit check-migration-heads hook can never be ambushed by a rebase;
* the upgrade creates exactly the six ``(organisation_id, name) WHERE
  deleted_at IS NULL`` indexes the soft-delete natural-key fix relies on,
  using the repo's idempotent ``CREATE UNIQUE INDEX IF NOT EXISTS``
  convention (0128/0154/0155/0171);
* the downgrade drops all six;
* every index that still has an ORM model is declared on that model's
  ``__table_args__`` (FAR-1755 closed the model-side gap — 0156 originally
  shipped with only ``uq_composite_templates_org_name`` declared on
  ``CompositeTemplate``);
* each model index carries the same column order, the unique flag, and both
  dialect where-clauses, so ``create_all``'d schemas and an alembic
  ``--autogenerate`` dry-run stay in sync with the migrated DB.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import Index
from sqlalchemy.sql.elements import TextClause

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0156_add_soft_delete_partial_uniques"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

# (index name, model class path, table name) — the 0156 indexes that still have
# a live ORM model.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("uq_pipelines_org_name", "modulo.db.models.pipeline:Pipeline", "pipelines"),
    ("uq_variant_groups_org_name", "modulo.db.models.variant_group:VariantGroup", "variant_groups"),
    (
        "uq_environment_profiles_org_name",
        "modulo.db.models.environment_profile:EnvironmentProfile",
        "environment_profiles",
    ),
    ("uq_composite_templates_org_name", "modulo.db.models.composite_template:CompositeTemplate", "composite_templates"),
    ("uq_lifecycle_maps_org_name", "modulo.db.models.lifecycle_map:LifecycleMap", "lifecycle_maps"),
)

# 0156 indexes whose table survives in migration history but whose ORM model was
# retired with its feature (Saved Views, FAR-2233). No model-side parity check
# is possible or wanted; the migration must still create the index.
_RETIRED_MODEL_INDEXES: tuple[tuple[str, str], ...] = (("uq_saved_views_org_name", "saved_views"),)


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


def _import_model(path: str) -> type:
    module_name, class_name = path.split(":")
    module = importlib.import_module(module_name)
    model = getattr(module, class_name)
    assert isinstance(model, type)
    return model


def test_metadata_pins_chain() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0175_dedupe_soft_delete_names"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_migration_creates_exactly_the_six_partial_uniques() -> None:
    module = _load_migration()
    expected = sorted([(name, table) for name, _, table in _INDEXES] + list(_RETIRED_MODEL_INDEXES))
    assert sorted(module._PARTIAL_UNIQUES) == expected
    assert len(module._PARTIAL_UNIQUES) == 6

    code = _source_code()
    # The upgrade assembles the CREATE from f-strings, so assert the template
    # pieces plus the (name, table) pairs rather than a full literal statement.
    assert "CREATE UNIQUE INDEX IF NOT EXISTS {name}" in code
    assert 'ON public."{table}" (organisation_id, name)' in code
    assert "WHERE deleted_at IS NULL;" in code
    # Idempotent convention, not op.create_index.
    assert "op.create_index" not in code
    assert "op.drop_index" not in code
    code_downgrade = code.split("def downgrade", 1)[1]
    assert "DROP INDEX IF EXISTS {name};" in code_downgrade


def test_every_0156_index_is_declared_on_its_model() -> None:
    """FAR-1755: every model-backed 0156 index must be on its ORM metadata.

    Before FAR-1755 only ``uq_composite_templates_org_name`` was declared;
    the other four existed in the DB but not on the models, so ``create_all``
    schemas and autogenerate drifted from the migrated DB. ``saved_views`` is
    excluded: its model was retired with the Saved Views feature (FAR-2233),
    while the table and its index remain in migration history.
    """
    for name, model_path, table_name in _INDEXES:
        model = _import_model(model_path)
        assert model.__tablename__ == table_name
        matching = [idx for idx in model.__table__.indexes if idx.name == name]
        assert matching, (
            f"model/migration drift: {name} missing from {model.__name__} — "
            f"declare it in __table_args__ (see composite_template.py)"
        )
        index = matching[0]
        assert isinstance(index, Index)
        assert index.unique is True, f"{name} on {model.__name__} must be unique"
        assert [col.name for col in index.columns] == ["organisation_id", "name"], (
            f"{name} on {model.__name__} must index (organisation_id, name) in that order — matching migration 0156"
        )


def test_each_model_index_carries_both_dialect_where_clauses() -> None:
    for name, model_path, _table_name in _INDEXES:
        model = _import_model(model_path)
        index = next(idx for idx in model.__table__.indexes if idx.name == name)
        postgres_where = index.dialect_options["postgresql"].get("where")
        sqlite_where = index.dialect_options["sqlite"].get("where")
        assert isinstance(postgres_where, TextClause), f"{name}: postgresql_where missing on {model.__name__}"
        assert isinstance(sqlite_where, TextClause), (
            f"{name}: sqlite_where missing on {model.__name__} — postgresql_where is ignored on SQLite, "
            "so create_all'd schemas would build a FULL unique index"
        )
        expected = "deleted_at IS NULL"
        assert str(postgres_where).strip() == expected, f"{name}: postgresql_where on {model.__name__}"
        assert str(sqlite_where).strip() == expected, f"{name}: sqlite_where on {model.__name__}"
