"""Schema generation service — uses an LLM to generate JSON Schema from description + examples."""

import json
import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from modulo.core.schema_registry._common import invoke_and_parse
from modulo.model_backends.base import ModelBackendBase

_log = logging.getLogger(__name__)

_GENERATION_SYSTEM_PROMPT = (
    "You are a JSON Schema generation assistant. Given a natural language "
    "description and optional example records, generate the JSON Schema "
    "(draft-07 or 2020-12) that best matches the described data.\n\n"
    "Rules:\n"
    "1. Return ONLY a valid JSON Schema object. "
    "No markdown, no explanation, no code fences.\n"
    "2. Use 'type', 'properties', 'items', 'required', 'description', "
    "'minimum', 'maximum', 'enum', 'pattern', 'format' etc. as appropriate.\n"
    "3. Infer types, constraints, and structure from the description and examples.\n"
    "4. If examples are provided, ensure the schema is compatible with all of them.\n"
    "5. Use reasonable descriptions for each property.\n"
    "6. The top level must have 'type': 'object' and 'properties': {}."
)

_GENERATE_TIMEOUT = 60.0


_MAX_EXAMPLE_RECORDS = 50


def _build_generate_prompt(
    description: str,
    examples: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
    max_examples: int = _MAX_EXAMPLE_RECORDS,
) -> list[BaseMessage]:
    parts = [f"Description:\n{description}\n"]
    if examples:
        display = examples[:max_examples]
        try:
            sample_text = json.dumps(display, indent=2, default=str)
        except ValueError as exc:
            raise ValueError(f"Example data contains non-serializable values (e.g. circular references): {exc}") from exc
        parts.append(f"Example records ({len(display)}):\n```\n{sample_text}\n```\n")
    parts.append("Return ONLY the JSON Schema object.")
    return [
        SystemMessage(content=system_prompt or _GENERATION_SYSTEM_PROMPT),
        HumanMessage(content="\n".join(parts)),
    ]


class SchemaGenerationError(Exception):
    """Raised when schema generation fails (LLM error, parse error, etc.)."""


class SchemaGenerationService:
    """Uses a ModelBackend to generate JSON Schema from description + examples."""

    def __init__(
        self,
        backend: ModelBackendBase,
        *,
        system_prompt: str | None = None,
        timeout: float = _GENERATE_TIMEOUT,
        max_example_records: int = _MAX_EXAMPLE_RECORDS,
    ) -> None:
        self._backend = backend
        self._system_prompt = system_prompt
        self._timeout = timeout
        self._max_example_records = max_example_records

    async def generate(
        self,
        description: str,
        examples: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not description or not description.strip():
            raise SchemaGenerationError("description must be a non-empty string")

        try:
            messages = _build_generate_prompt(description, examples, self._system_prompt, self._max_example_records)
        except ValueError as exc:
            raise SchemaGenerationError(str(exc)) from exc
        return await invoke_and_parse(
            self._backend,
            messages,
            timeout=self._timeout,
            error_cls=SchemaGenerationError,
            context="generation",
        )
