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


def _safe_json_dumps(data: Any, indent: int = 2) -> str:
    try:
        return json.dumps(data, indent=indent, default=str)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Data contains non-serializable values (e.g. circular references): {exc}") from exc


def parse_schema_from_response(response_text: str) -> dict[str, Any]:
    text = response_text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    schema = json.loads(text)
    if not isinstance(schema, dict):
        raise ValueError("LLM response is not a JSON object")
    result: dict[str, Any] = {"type": "object", "properties": {}}
    result.update(schema)
    return result


_MAX_RETRIES = 3


async def _invoke_with_timeout(
    backend: ModelBackendBase,
    messages: list[BaseMessage],
    *,
    timeout: float,  # noqa: ASYNC109
) -> BaseMessage:
    async with asyncio.timeout(timeout):
        return await backend.invoke(messages)


def _extract_content(response: BaseMessage, *, context: str, error_cls: type[Exception]) -> str:
    try:
        content = response.content
    except AttributeError:
        _log.error(
            "Backend returned response without .content attribute for schema %s (response type: %s)",
            context,
            type(response).__name__,
        )
        raise error_cls("Backend returned unexpected response type") from None

    if not isinstance(content, str):
        _log.error("Backend returned non-string content for schema %s (got %s)", context, type(content).__name__)
        raise error_cls(f"Expected string response, got {type(content).__name__}")

    return content


def _parse_content(content: str, *, context: str, error_cls: type[Exception]) -> dict[str, Any]:
    try:
        return parse_schema_from_response(content)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.exception("Failed to parse %s schema from LLM response", context)
        raise error_cls(f"Failed to parse {context} schema from LLM response") from exc


async def invoke_and_parse(
    backend: ModelBackendBase,
    messages: list[BaseMessage],
    *,
    timeout: float,  # noqa: ASYNC109
    error_cls: type[Exception],
    context: str,
) -> dict[str, Any]:
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = await _invoke_with_timeout(backend, messages, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _log.error("Schema %s timed out after %ss (attempt %d/%d)", context, timeout, attempt, _MAX_RETRIES)
            if attempt == _MAX_RETRIES:
                raise error_cls(f"LLM call timed out after {timeout}s") from None
        except Exception as exc:
            _log.exception("LLM call failed during schema %s (attempt %d/%d)", context, attempt, _MAX_RETRIES)
            if attempt == _MAX_RETRIES:
                raise error_cls("LLM call failed") from exc
            await asyncio.sleep(2**attempt)
        else:
            content = _extract_content(response, context=context, error_cls=error_cls)
            return _parse_content(content, context=context, error_cls=error_cls)

    raise error_cls("LLM call failed after all retries (unreachable — safety net)")
