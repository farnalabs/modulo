"""Shared cursor-coercion helper for connector clients.

Connectors that list resources by cursor (n8n, Notion, CircleCI, Linear) pass
the raw ``next_*`` field from a paginated response straight into
``ConnectorResult.next_cursor``. A corrupt or hostile response may place
anything there (bool, number, dict, list, ...). A non-string cursor then flows
into the next request's query params or JSON body, where httpx raises on
dict/list values and booleans/numbers are silently mis-serialised. Keeping a
single ``safe_cursor`` in one place avoids drift between the per-connector
copies (mirrors ``_safe_int``).
"""

from __future__ import annotations


def safe_cursor(value: object) -> str | None:
    """Return *value* when it is a non-empty string, else ``None``.

    Only non-empty strings are meaningful pagination cursors; anything else
    (``None``, bool, number, dict, list, ``""``) must not be emitted as a
    cursor, or the next page request will crash or loop.
    """
    if isinstance(value, str) and value:
        return value
    return None
