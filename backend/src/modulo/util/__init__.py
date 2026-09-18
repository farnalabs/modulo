"""Shared, dependency-free utilities used across all modulo layers.

This module MUST NOT import from ``modulo.core``, ``modulo.api`` or
``modulo.db``: it is intentionally a leaf so that the DB, core and API layers
can all import from it without violating the import-linter layer contracts.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = ["DEFAULT_LOG_LIMIT", "is_valid_http_url", "sanitise_log_value"]

#: Default cap (in code points) for :func:`sanitise_log_value`. Overridable via
#: the ``limit`` argument when a call site needs a tighter bound.
DEFAULT_LOG_LIMIT = 200


def is_valid_http_url(value: object) -> bool:
    """Return True only for ``http``/``https`` URLs that carry a host.

    Surrounding whitespace is stripped before parsing, so a value like
    ``" http://x.com "`` is validated against the bare URL and a whitespace-
    padded hostname can never pass the check. Unlike a bare scheme check, this
    still rejects malformed values such as ``https:example.com`` (opaque, no
    ``//``) and ``https://`` (no netloc). The scheme test is case-insensitive
    per RFC 3986, so ``HTTP://host`` is accepted and normalised by the
    downstream stack.
    """
    parsed = urlparse(str(value).strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def sanitise_log_value(value: object, limit: int = DEFAULT_LOG_LIMIT) -> str:
    """Sanitise a value for logging: escape CR/LF and cap length.

    Prevents log injection (S5145) by replacing newline and carriage-return
    characters with their literal ``\\n`` / ``\\r`` forms so a malicious value
    cannot forge log entries, and bounds the size of the logged value.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]
