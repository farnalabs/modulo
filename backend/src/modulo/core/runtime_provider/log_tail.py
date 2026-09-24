"""E2B log-entry parsing shared by the legacy probe and the R1 primitive.

FAR-1050 R1: ``node_runner._fetch_sandbox_log_tail`` (the flag-OFF legacy
probe) and ``E2BRuntimeProvider.read_log_tail`` (the flag-ON primitive) must
produce byte-identical tails over the same E2B ``logEntries`` payload. The
parsing helpers live here, once, so the two paths cannot drift; the
content-parity test pins the behaviour rather than re-asserting a copy.
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
