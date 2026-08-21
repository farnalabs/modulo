"""Shared, dependency-free utilities used across all modulo layers.

This module MUST NOT import from ``modulo.core``, ``modulo.api`` or
``modulo.db``: it is intentionally a leaf so that the DB, core and API layers
can all import from it without violating the import-linter layer contracts.
"""

__all__ = ["sanitise_log_value"]


def sanitise_log_value(value: object, limit: int = 200) -> str:
    """Sanitise a value for logging: strip CR/LF and cap length.

    Prevents log injection (S5145) by removing newline characters that could
    forge log entries, and bounds the size of the logged value.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]
