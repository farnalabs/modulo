"""Shared types for CRUD service layer."""

from dataclasses import dataclass
from typing import Any


_IMMUTABLE_FIELDS = frozenset({
    "id",
    "organisation_id",
    "created_at",
    "updated_at",
    "deleted_at",
})


def apply_updates(entity: Any, updates: dict[str, Any]) -> None:
    """Apply field updates, skipping immutable fields.

    Prevents accidental overwrite of id, organisation_id, and timestamps
    via the generic ``updates`` dict pattern used across CRUD modules.
    """
    for key, value in updates.items():
        if key in _IMMUTABLE_FIELDS:
            continue
        if hasattr(entity, key):
            setattr(entity, key, value)


@dataclass
class PageResult[T]:
    items: list[T]
    total: int
    page: int
    page_size: int
