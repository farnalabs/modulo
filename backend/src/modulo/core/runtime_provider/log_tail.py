"""E2B log-entry parsing for the provider log-tail primitive.

FAR-1050 R1: ``E2BRuntimeProvider.read_log_tail`` is the single log-tail
path; it must produce a bounded, preferred-level-first tail over the same E2B
``logEntries`` payload. The parsing helpers live here, once, so the primitive
cannot drift from a second copy. (R6 retired the legacy
``node_runner._fetch_sandbox_log_tail`` probe that originally shared these
helpers — the helper is gone, this module now has a single caller.)
"""

from __future__ import annotations

from typing import Any

# Informative levels sort ahead of the remainder so the most actionable lines
# survive the bounded tail window.
_PREFERRED_LEVELS = frozenset({"info", "warn", "warning", "error"})


def combine_log_entries(entries: list[Any], limit: int) -> list[str]:
    """Split E2B log entries into preferred-level and rest, then tail the union.

    Entries at informative levels (info/warn/warning/error) sort ahead of the
    remainder so the most actionable lines survive the ``limit`` window.
    """
    preferred: list[str] = []
    rest: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = log_entry_text(entry)
        if not text:
            continue
        if isinstance(entry.get("level"), str) and entry["level"].lower() in _PREFERRED_LEVELS:
            preferred.append(text)
        else:
            rest.append(text)
    return (preferred + rest)[-limit:]


def log_entry_text(entry: dict[str, Any]) -> str:
    """Extract the human-readable text of one E2B log entry."""
    msg = entry.get("message")
    if msg is None:
        msg = entry.get("fields")
    if not msg:
        return ""
    return str(msg)
