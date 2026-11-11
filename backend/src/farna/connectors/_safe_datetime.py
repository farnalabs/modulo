"""Shared datetime-coercion helper for connector clients.

Ticket-tracker connectors (GitHub Issues, Trello) pass the raw
``created_at``/``updated_at``/``dateLastActivity`` fields straight into
``datetime.fromisoformat``. A corrupt or hostile response may place anything
there — a non-string, an empty string, or a string that is not ISO 8601
(``"not-a-date"``, ``"2025-13-99"``). ``fromisoformat`` then raises
``ValueError``/``TypeError`` and takes down the whole list query. Keeping a
single ``safe_datetime`` in one place avoids drift between the per-connector
copies (mirrors ``_safe_cursor`` / ``_safe_int``).
"""

from __future__ import annotations

from datetime import datetime


def safe_datetime(value: object) -> datetime | None:
    """Coerce *value* to a ``datetime``, or ``None`` when it is not parseable.

    Only non-empty strings that ``datetime.fromisoformat`` accepts are
    meaningful timestamps; anything else (``None``, bool, number, dict, list,
    garbage string) must not crash ticket/run listing.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
