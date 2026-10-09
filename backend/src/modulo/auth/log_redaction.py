"""Helpers for keeping sensitive identifiers out of log lines."""

from __future__ import annotations

_TOKEN_FAMILY_LOG_CHARS = 8


def truncate_token_family(family_id: object) -> str:
    """Return a short, non-reversible form of a token family id for logging.

    The full family UUID is a bearer-adjacent identifier (it is embedded in
    refresh tokens), so log lines carry only the first 8 characters, enough
    to correlate events without exposing the whole value.
    """
    return str(family_id)[:_TOKEN_FAMILY_LOG_CHARS]
