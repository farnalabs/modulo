"""Shared utilities for schema_registry — response parsing and LLM invocation."""

import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.messages import BaseMessage

from modulo.model_backends.base import ModelBackendBase

_log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)\n```", re.DOTALL)


def parse_schema_from_response(response_text: str) -> dict[str, Any]:
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


async def invoke_and_parse(
    backend: ModelBackendBase,
    messages: list[BaseMessage],
    *,
    timeout: float,  # noqa: ASYNC109
    error_cls: type[Exception],
    context: str,
) -> dict[str, Any]:
    _max_retries = 3

    for attempt in range(1, _max_retries + 1):
        try:
            async with asyncio.timeout(timeout):
                response = await backend.invoke(messages)
        except TimeoutError:
            _log.error("Schema %s timed out after %ss (attempt %d/%d)", context, timeout, attempt, _max_retries)
            if attempt == _max_retries:
                raise error_cls(f"LLM call timed out after {timeout}s") from None
            continue
        except Exception as exc:
            _log.exception("LLM call failed during schema %s (attempt %d/%d)", context, attempt, _max_retries)
            if attempt == _max_retries:
                raise error_cls("LLM call failed") from exc
            await asyncio.sleep(1 * attempt)
            continue

        try:
            content = response.content
        except AttributeError:
            _log.error("Backend returned response without .content attribute for schema %s (response type: %s)",
                       context, type(response).__name__)
            raise error_cls("Backend returned unexpected response type") from None

        if not isinstance(content, str):
            _log.error("Backend returned non-string content for schema %s (got %s)",
                       context, type(content).__name__)
            raise error_cls(f"Expected string response, got {type(content).__name__}")

        try:
            return parse_schema_from_response(content)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.exception("Failed to parse %s schema from LLM response", context)
            raise error_cls(f"Failed to parse {context} schema from LLM response") from exc

    raise error_cls("LLM call failed after all retries")
