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
    apply_migration,
    create_migration,
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
    "SchemaGenerationError",
    "SchemaGenerationService",
    "SchemaInferenceError",
    "SchemaInferenceService",
    "SchemaValidationError",
    "SchemaValidationResult",
    "apply_migration",
    "create_migration",
    "transform_field",
    "validate_array_schema",
    "validate_union_and_array",
    "validate_union_schema",
]
