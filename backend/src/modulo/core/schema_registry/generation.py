"""Schema generation service — uses an LLM to generate JSON Schema from description + examples."""

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from modulo.core.schema_registry.inference import _parse_schema_from_response
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


def _build_generate_prompt(
    description: str,
    examples: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
) -> list[BaseMessage]:
    parts = [f"Description:\n{description}\n"]
    if examples:
        sample_text = json.dumps(examples, indent=2, default=str)
        parts.append(f"Example records ({len(examples)}):\n```\n{sample_text}\n```\n")
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
    ) -> None:
        self._backend = backend
        self._system_prompt = system_prompt
        self._timeout = timeout

    async def generate(
        self,
        description: str,
        examples: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not description or not description.strip():
            raise ValueError("description must be a non-empty string")

        messages = _build_generate_prompt(description, examples, self._system_prompt)
        try:
            response = await asyncio.wait_for(
                self._backend.invoke(messages),
                timeout=self._timeout,
            )
        except TimeoutError:
            _log.error("Schema generation timed out after %ss", self._timeout)
            raise SchemaGenerationError(
                f"LLM call timed out after {self._timeout}s"
            ) from None
        except Exception as exc:
            _log.exception("LLM call failed during schema generation")
            raise SchemaGenerationError("LLM call failed") from exc

        if not hasattr(response, "content"):
            raise SchemaGenerationError("Backend returned unexpected response type")

        content = response.content
        if not isinstance(content, str):
            raise SchemaGenerationError(
                f"Expected string response, got {type(content).__name__}"
            )

        try:
            return _parse_schema_from_response(content)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.exception("Failed to parse generated schema from LLM response")
            raise SchemaGenerationError(
                "Failed to parse generated schema from LLM response"
            ) from exc
