"""Canonical library primitives shipped with Modulo.

Each module defines the content_json for a LibraryPrimitive entry,
including prompts, schemas, and metadata needed to instantiate the
primitive in a pipeline.

Primitives are made available as library entries via seeding or
manual import.  See individual modules for usage.
"""

from modulo.core.library.complexity_reviewer import COMPLEXITY_REVIEWER

__all__ = [
    "COMPLEXITY_REVIEWER",
]
