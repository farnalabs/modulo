"""Backward-compatibility shim -- prefer ``modulo.util`` for new code.

This package is deprecated; all utilities live in ``modulo.util`` now.
"""

from modulo.util.uuid import coerce_uuid

__all__ = ["coerce_uuid"]
