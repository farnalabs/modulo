"""Shared test helpers for the ``graph_validator`` unit package.

``codes_of`` was defined near-identically in nine test modules; it is defined
once here so the issue-code extraction has a single source.
"""

from __future__ import annotations

from modulo.core.graph_validator import ValidationResult


def codes_of(result: ValidationResult) -> set[str]:
    """Return the set of issue codes present in *result*."""
    return {i.code for i in result.issues}
