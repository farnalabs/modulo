"""Schema for CompositeBinding stored in PipelineSnapshot."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvalDefinitionConfig(BaseModel):
    """Configuration for a single eval definition used in output validation."""

    id: str
    name: str
    type: Literal["regex", "json_schema", "llm_judge"]
    config: dict[str, Any] = Field(default_factory=dict)
    failure_behaviour: Literal["retry", "block", "warn"] = "retry"


class OutputValidation(BaseModel):
    """Configuration for composite output validation."""

    eval_definitions: list[EvalDefinitionConfig] = Field(default_factory=list)
    max_validation_retries: int = 0


class ValidationResult(BaseModel):
    """Result of running output validation against a composite output."""

    passed: bool = True
    failures: list[str] = Field(default_factory=list)
    retry_count: int = 0


class CompositeValidationError(RuntimeError):
    """Raised when composite output validation fails and retry budget is exhausted."""

    def __init__(self, failures: list[str], retry_count: int) -> None:
        super().__init__(f"Composite output validation failed after {retry_count} retries: {'; '.join(failures)}")
        self.failures = failures
        self.retry_count = retry_count


class CompositeBinding(BaseModel):
    """Binding of a composite template to a pipeline snapshot.

    Stored in ``PipelineSnapshot.composite_bindings_json`` to capture
    which version of which composite template was bound, along with
    parameter values and input/output field mappings.
    """

    composite_template_id: uuid.UUID
    composite_version: str
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    input_mapping: dict | None = None
    output_mapping: dict | None = None
    output_validation: OutputValidation = Field(default_factory=OutputValidation)
