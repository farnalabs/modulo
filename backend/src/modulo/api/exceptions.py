"""Convenience re-exports and domain-specific exception utilities for Modulo API.

The primary exception mechanism is ProblemException (from models/problem.py).
This module provides re-exports and convenience base classes for common patterns.
"""

from modulo.api.models.problem import ProblemDetail, ProblemException, ProblemType

__all__ = [
    "ProblemDetail",
    "ProblemException",
    "ProblemType",
]
