"""Schema inference service - uses an LLM to infer JSON Schema from sample data."""

import json
import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from modulo.core.schema_registry._common import invoke_and_parse
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


def _build_infer_prompt(
    samples: list[dict[str, Any]],
    system_prompt: str | None = None,
    max_records: int = _MAX_SAMPLE_RECORDS,
) -> list[BaseMessage]:
    display = samples[:max_records]
    try:
        sample_text = json.dumps(display, indent=2, default=str)
    except ValueError as exc:
        raise ValueError(f"Sample data contains non-serializable values (e.g. circular references): {exc}") from exc
    message_text = (
        f"Sample data ({len(display)} records):\n```\n{sample_text}\n```\nReturn ONLY the JSON Schema object."
    )
    return [
        SystemMessage(content=system_prompt or _INFERENCE_SYSTEM_PROMPT),
        HumanMessage(content=message_text),
    ]


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

        try:
            messages = _build_infer_prompt(samples, self._system_prompt, self._max_sample_records)
        except ValueError as exc:
            raise SchemaInferenceError(str(exc)) from exc
        return await invoke_and_parse(
            self._backend,
            messages,
            timeout=self._timeout,
            error_cls=SchemaInferenceError,
            context="inference",
        )
