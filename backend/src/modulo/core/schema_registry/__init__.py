"""Schema Registry — LLM-based schema inference, generation, validation,
migration between schema versions, and profile-based rendering/translation.
"""

from modulo.core.schema_registry.generation import (
    SchemaGenerationError,
    SchemaGenerationService,
)
from modulo.core.schema_registry.inference import (
    SchemaInferenceError,
    SchemaInferenceService,
    flag_rare_fields,
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
from modulo.core.schema_registry.rendering import (
    RenderResult,
    RenderWarning,
    SchemaProfile,
    preview_strip_warnings,
    render_for_profile,
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
    "RenderResult",
    "RenderWarning",
    "SchemaGenerationError",
    "SchemaGenerationService",
    "SchemaInferenceError",
    "SchemaInferenceService",
    "SchemaMigration",
    "SchemaProfile",
    "SchemaValidationError",
    "SchemaValidationResult",
    "add_field",
    "apply_migration",
    "convert_field",
    "create_migration",
    "flag_rare_fields",
    "preview_strip_warnings",
    "remove_field",
    "rename_field",
    "render_for_profile",
    "set_default",
    "transform_field",
    "validate_array_schema",
    "validate_union_and_array",
    "validate_union_schema",
]
