"""Schema inference service - uses an LLM to infer JSON Schema from sample data."""

import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from modulo.model_backends.base import ModelBackendBase

_log = logging.getLogger(__name__)

_INFERENCE_SYSTEM_PROMPT = (
    "You are a schema inference assistant. Given sample data records, infer "
    "the JSON Schema that describes their structure.\n\n"
    "Rules:\n"
    "1. Return ONLY a valid JSON Schema object (draft-07 or 2020-12). "
    "No markdown, no explanation, no code fences.\n"
    "2. Infer types from actual values in the samples. Use 'type', "
    "'properties', 'items', 'required', 'description' etc.\n"
    "3. If a field appears in some but not all records, mark it as not required.\n"
    "4. If a field value is always null or missing, omit it from the schema.\n"
    "5. Use reasonable descriptions for each property based on the field "
    "name and sample values.\n"
    "6. The top level must have 'type': 'object' and 'properties': {}."
)

_MAX_SAMPLE_RECORDS = 50
_INFER_TIMEOUT = 60.0

_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n(.*?)\n```", re.DOTALL)


def _build_infer_prompt(
    samples: list[dict[str, Any]],
    system_prompt: str | None = None,
) -> list[BaseMessage]:
    display = samples[:_MAX_SAMPLE_RECORDS]
    sample_text = json.dumps(display, indent=2, default=str)
    message_text = (
        f"Sample data ({len(display)} records):\n```\n{sample_text}\n```\nReturn ONLY the JSON Schema object."
    )
    return [
        SystemMessage(content=system_prompt or _INFERENCE_SYSTEM_PROMPT),
        HumanMessage(content=message_text),
    ]


def _parse_schema_from_response(response_text: str) -> dict[str, Any]:
    text = response_text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    schema = json.loads(text)
    if not isinstance(schema, dict):
        raise ValueError("LLM response is not a JSON object")
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


class SchemaInferenceError(Exception):
    """Raised when schema inference fails (LLM error, parse error, etc.)."""


class SchemaInferenceService:
    """Uses a ModelBackend to infer JSON Schema from record samples."""

    def __init__(
        self,
        backend: ModelBackendBase,
        *,
        system_prompt: str | None = None,
        max_sample_records: int = _MAX_SAMPLE_RECORDS,
        timeout: float = _INFER_TIMEOUT,
    ) -> None:
        self._backend = backend
        self._system_prompt = system_prompt
        self._max_sample_records = max_sample_records
        self._timeout = timeout

    async def infer(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        if not all(isinstance(r, dict) for r in samples):
            raise ValueError("samples must be a list of dicts")

        messages = _build_infer_prompt(samples, self._system_prompt)
        try:
            response = await asyncio.wait_for(
                self._backend.invoke(messages),
                timeout=self._timeout,
            )
        except TimeoutError:
            _log.error("Schema inference timed out after %ss", self._timeout)
            raise SchemaInferenceError(f"LLM call timed out after {self._timeout}s") from None
        except Exception as exc:
            _log.exception("LLM call failed during schema inference")
            raise SchemaInferenceError("LLM call failed") from exc

        if not hasattr(response, "content"):
            raise SchemaInferenceError("Backend returned unexpected response type")

        content = response.content
        if not isinstance(content, str):
            raise SchemaInferenceError(f"Expected string response, got {type(content).__name__}")

        try:
            return _parse_schema_from_response(content)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.exception("Failed to parse inferred schema from LLM response")
            raise SchemaInferenceError("Failed to parse inferred schema from LLM response") from exc
