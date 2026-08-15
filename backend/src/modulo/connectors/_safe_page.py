"""Shared safe-page extraction helper for connector clients.

The Azure Repos (``value``), Bitbucket (``values``), Microsoft Teams
(``value``), and SharePoint (``value``) connectors each guard their list
pagination against corrupt or hostile response bodies. A corrupt or hostile
response may return a non-dict body (list, string, number, ...) or a non-list
page field — either crashes the connector with ``AttributeError`` on the bare
``body.get(key, [])`` chain or returns a bare string as the records list.
Keeping a single implementation in one place avoids drift between the copies
(mirrors ``_safe_int`` / ``_safe_cursor`` / ``_safe_datetime``).
"""

from __future__ import annotations

from typing import Any


def safe_records(body: object, key: str) -> list[dict[str, Any]]:
    """Return the *key* page list from *body*, or an empty page for corrupt bodies.

    Only a dict body whose *key* field holds a list yields records; anything
    else (non-dict body, missing key, non-list value) falls back to an empty
    page so the caller's list query degrades gracefully instead of crashing.
    """
    if not isinstance(body, dict):
        return []
    records = body.get(key, [])
    return records if isinstance(records, list) else []
