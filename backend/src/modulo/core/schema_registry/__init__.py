"""Schema Registry — LLM-based schema inference, generation, validation,
and migration between schema versions."""

from modulo.core.schema_registry.generation import (
    SchemaGenerationError,
    SchemaGenerationService,
)
from modulo.core.schema_registry.inference import (
    SchemaInferenceError,
    SchemaInferenceService,
)
from modulo.core.schema_registry.migration import (
    FieldChange,
    MigrationPlan,
    MigrationRegistry,
    MissingMigrationError,
    SchemaMigration,
    add_field,
    apply_migration,
    convert_field,
    create_migration,
    remove_field,
    rename_field,
    set_default,
    transform_field,
)
from modulo.core.schema_registry.validation import (
    SchemaValidationError,
    SchemaValidationResult,
    validate_array_schema,
    validate_union_and_array,
    validate_union_schema,
)

__all__ = [
    "FieldChange",
    "MigrationPlan",
    "MigrationRegistry",
    "MissingMigrationError",
    "SchemaGenerationError",
    "SchemaGenerationService",
    "SchemaInferenceError",
    "SchemaInferenceService",
    "SchemaMigration",
    "SchemaValidationError",
    "SchemaValidationResult",
    "add_field",
    "apply_migration",
    "convert_field",
    "create_migration",
    "remove_field",
    "rename_field",
    "set_default",
    "transform_field",
    "validate_array_schema",
    "validate_union_and_array",
    "validate_union_schema",
]
