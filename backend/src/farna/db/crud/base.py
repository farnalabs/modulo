"""Shared types for CRUD service layer."""

from dataclasses import dataclass
from typing import Any

_IMMUTABLE_FIELDS = frozenset(
    {
        "id",
        "organisation_id",
        "created_at",
        "updated_at",
        "deleted_at",
    }
)


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
    next_cursor: str | None = None
    has_more: bool | None = None

    def __post_init__(self) -> None:
        """Compute ``has_more`` for an omitted value on the standard offset shape.

        A plain ``has_more: bool = False`` default silently reported "no more
        pages" for every producer that forgot to pass it — the very defect this
        type caused across the CRUD sweep (FAR-1877). Making the field default
        ``None`` and computing the offset-pagination identity here means an
        OMITTED ``has_more`` is correct by construction, while an explicitly
        passed value (cursor producers pass ``cp.has_more``; composite producers
        pass their own) always wins.

        The formula ``(page - 1) * page_size + len(items) < total`` is the
        validated contract for offset pagination: a full non-final page is
        ``True``; a final, partial or empty page is ``False``.
        """
        if self.has_more is None:
            self.has_more = (self.page - 1) * self.page_size + len(self.items) < self.total
