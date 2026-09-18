"""Backward-compatibility shim -- prefer ``modulo.util.uuid`` for new code."""

from modulo.util.uuid import coerce_uuid

__all__ = ["coerce_uuid"]
