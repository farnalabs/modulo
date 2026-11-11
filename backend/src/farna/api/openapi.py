"""Keep the OpenAPI document truthful about error response bodies.

FastAPI auto-declares a ``422`` response for every operation that has request
validation, pointing at its own ``HTTPValidationError`` shape — ``detail`` as an
array of Pydantic errors. This app never sends that body: the
``RequestValidationError`` handler renders RFC 9457 Problem Details
(``modulo.api.models.problem``), where ``detail`` is a human-readable string.

The document is therefore the defect, not the response: the Problem Details
envelope is the contract the frontend already consumes, and rewriting the wire
to satisfy the document would regress it. Correcting the document instead also
keeps the nightly ``Schemathesis API Fuzz`` gate's
``response_schema_conformance`` check honest about what clients actually parse.
"""

from __future__ import annotations

from typing import Any

VALIDATION_ERROR_REF = "#/components/schemas/HTTPValidationError"
PROBLEM_DETAIL_REF = "#/components/schemas/ProblemDetail"
PROBLEM_DETAIL_NAME = "ProblemDetail"

# FastAPI's auto-generated request-validation components. They are dropped only
# once nothing in the document still points at them (``ValidationError`` is
# shared, so it is pruned strictly on its own ref count).
_PRUNED_COMPONENT_NAMES = ("HTTPValidationError", "ValidationError")

PROBLEM_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": PROBLEM_DETAIL_NAME,
    "description": (
        "RFC 9457 Problem Details. ``detail`` is human-readable prose; clients "
        "discriminate on ``type``/``code`` rather than parsing it."
    ),
    "required": ["type", "title", "status", "detail"],
    "properties": {
        "type": {"type": "string", "description": "Problem type URN, e.g. urn:problem:modulo:validation_error"},
        "title": {"type": "string", "description": "Short, per-type summary (RFC 9457 section 3.1.3)"},
        "status": {"type": "integer", "description": "HTTP status code, duplicated for offline handling"},
        "detail": {"type": "string", "description": "Human-readable explanation of this specific occurrence"},
        "instance": {"type": "string", "description": "URI reference identifying the affected resource"},
        "request_id": {"type": "string", "description": "Correlation id, also echoed as the X-Request-ID header"},
        "code": {"type": "string", "description": "Machine-readable route error code, when the route defines one"},
    },
}


def _walk(node: Any) -> list[Any]:
    """Every dict/list nested inside ``node``, including ``node`` itself."""
    found = [node]
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _collect_refs(node: Any) -> set[str]:
    return {child["$ref"] for child in _walk(node) if isinstance(child, dict) and isinstance(child.get("$ref"), str)}


def _rewrite_validation_error_refs(node: Any) -> int:
    """Swap every ``HTTPValidationError`` ref for a ``ProblemDetail`` ref.

    Mutates in place and returns the number of swaps, so a second application
    is a no-op rather than a re-render.
    """
    swaps = 0
    if isinstance(node, dict):
        if node.get("$ref") == VALIDATION_ERROR_REF:
            node["$ref"] = PROBLEM_DETAIL_REF
            return 1
        for value in node.values():
            swaps += _rewrite_validation_error_refs(value)
    elif isinstance(node, list):
        for item in node:
            swaps += _rewrite_validation_error_refs(item)
    return swaps


def apply_error_response_schemas(openapi: dict[str, Any]) -> dict[str, Any]:
    """Point every declared request-validation response at the ProblemDetails shape.

    Idempotent: an already-corrected document yields 0 swaps and is returned
    unchanged apart from re-asserting the ``ProblemDetail`` component.
    """
    operations = (
        operation
        for container in (openapi.get("paths"), openapi.get("webhooks"))
        if isinstance(container, dict)
        for path in container.values()
        if isinstance(path, dict)
        for operation in path.values()
    )
    swaps = sum(_rewrite_validation_error_refs(operation) for operation in operations)

    schemas = openapi.setdefault("components", {}).setdefault("schemas", {})
    if swaps or PROBLEM_DETAIL_NAME in schemas:
        schemas[PROBLEM_DETAIL_NAME] = PROBLEM_DETAIL_SCHEMA

    if swaps:
        for name in _PRUNED_COMPONENT_NAMES:
            if f"#/components/schemas/{name}" not in _collect_refs(openapi):
                schemas.pop(name, None)

    return openapi
