"""Schema for CompositeBinding stored in PipelineSnapshot."""

import uuid
from typing import Any

from pydantic import BaseModel, Field


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
