"""Shared, dependency-free utilities used across all modulo layers.

This module MUST NOT import from ``modulo.core``, ``modulo.api`` or
``modulo.db``: it is intentionally a leaf so that the DB, core and API layers
can all import from it without violating the import-linter layer contracts.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

__all__ = [
    "DEFAULT_LOG_LIMIT",
    "WorkspaceNetworkValidationError",
    "is_valid_http_url",
    "sanitise_log_value",
    "validate_workspace_network",
]

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


# ---------------------------------------------------------------------------
# workspace_network validation (FAR-1020)
# ---------------------------------------------------------------------------
#
# Docker network *modes* that defeat workspace isolation must never be
# accepted as a named network.  The deny-list is exhaustive for the known
# dangerous modes:
#
# - ``host``       → host network namespace (Docker socket proxy reachable)
# - ``container:*`` → shares another container's network namespace
# - ``bridge``     → the default Docker bridge (different isolation profile)
# - ``none``       → not a named network; opt-in via egress_policy instead
# - ``default``    → Docker alias for ``bridge``
#
# Accepted pattern: a plain Docker network *name* — lowercase alphanumeric
# with dots, dashes, and underscores (Docker's actual character set is
# wider, but deployment-owned bridge networks never need exotic characters).
#
# None / empty → provider default (the deployment-owned bridge).

_NETWORK_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{1,127})?$")

_DANGEROUS_NETWORK_MODES: frozenset[str] = frozenset(
    {
        "host",
        "bridge",
        "none",
        "default",
    }
)


class WorkspaceNetworkValidationError(ValueError):
    """Raised when a workspace_network value would defeat workspace isolation.

    Carries the offending value so callers can surface it in 4xx responses.
    """

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(
            f"workspace_network value {value!r} is not a valid named Docker network — "
            "the following network modes are explicitly rejected: host, container:*, bridge, none, default. "
            "Use a deployment-owned bridge network name (e.g. 'modulo-runner-workspace')."
        )


def validate_workspace_network(value: str | None) -> str | None:
    """Validate that *value* is a safe, deployment-owned bridge network name.

    Returns the validated (or ``None``) value on success.
    Raises :class:`WorkspaceNetworkValidationError` on a dangerous or
    malformed value.  This is the **single source of truth** for
    workspace_network validation — called at profile CRUD, at dispatch,
    and in the Docker provider.
    """
    if value is None or value.strip() == "":
        return None
    stripped = value.strip()
    low = stripped.lower()
    # Reject known Docker network modes that defeat isolation.
    if low in _DANGEROUS_NETWORK_MODES:
        raise WorkspaceNetworkValidationError(stripped)
    # Reject container:* (shares another container's network namespace).
    if low.startswith("container:"):
        raise WorkspaceNetworkValidationError(stripped)
    # Validate against the safe-name pattern.
    if not _NETWORK_NAME_RE.match(low):
        raise WorkspaceNetworkValidationError(stripped)
    return stripped
