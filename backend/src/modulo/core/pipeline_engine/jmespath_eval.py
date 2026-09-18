"""Shared JMESPath condition evaluation for the pipeline engine.

Centralises the one truthiness rule (``bool(...)``) used across every JMESPath
guard site in the engine (conditional edges, loop counters, HITL gate
conditions, polling triggers, and the new Router node). Previously each site
compiled + searched inline; consolidating here keeps the semantics identical
and gives the Router a single evaluator to lower onto.

The truthiness rule is deliberately ``bool(result)`` — NOT a richer
list/dict/number-aware check — to preserve the exact behaviour of the original
inline sites (graph_cache conditional router used ``_is_truthy`` == ``bool``).
"""

from __future__ import annotations

from typing import Any

import jmespath
from jmespath.exceptions import JMESPathError

#: Process-wide cache of compiled JMESPath expressions keyed by source string.
_COMPILED_CACHE: dict[str, Any] = {}


def compile_jmespath(expr: str | None) -> Any:
    """Compile a JMESPath *expr* (or ``None``) to a compiled expression.

    Raises ``ValueError`` (with the offending expression) when *expr* is not
    valid JMESPath. A ``None``/empty expr compiles to ``None`` so callers can
    treat "no guard" as falsy without special-casing.
    """
    if not expr:
        return None
    cached = _COMPILED_CACHE.get(expr)
    if cached is not None:
        return cached
    try:
        compiled = jmespath.compile(expr)
    except JMESPathError as exc:
        raise ValueError(f"Invalid JMESPath expression: {expr}") from exc
    _COMPILED_CACHE[expr] = compiled
    return compiled


def evaluate_jmespath_condition(state: Any, expr: str | None) -> bool:
    """Evaluate JMESPath *expr* against *state*, returning ``bool(...)`` truthiness.

    An empty/``None`` expr is treated as falsy (no guard present).
    """
    if not expr:
        return False
    compiled = compile_jmespath(expr)
    if compiled is None:
        return False
    result = compiled.search(state)
    return bool(result)
