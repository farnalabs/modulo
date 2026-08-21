"""Shared, dependency-free utilities used across all modulo layers.

This module MUST NOT import from ``modulo.core``, ``modulo.api`` or
``modulo.db``: it is intentionally a leaf so that the DB, core and API layers
can all import from it without violating the import-linter layer contracts.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = ["is_valid_http_url", "sanitise_log_value"]


def is_valid_http_url(value: object) -> bool:
    """Return True only for ``http``/``https`` URLs that carry a host.

    Unlike a bare scheme check, this rejects malformed values such as
    ``https:example.com`` (opaque, no ``//``), ``https://`` (no netloc) and
    `` http://x.com`` (leading whitespace defeats scheme detection). The
    scheme test is case-insensitive per RFC 3986, so ``HTTP://host`` is
    accepted and normalised by the downstream stack.
    """
    parsed = urlparse(str(value))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def sanitise_log_value(value: object, limit: int = 200) -> str:
    """Sanitise a value for logging: strip CR/LF and cap length.

    Prevents log injection (S5145) by removing newline characters that could
    forge log entries, and bounds the size of the logged value.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]
