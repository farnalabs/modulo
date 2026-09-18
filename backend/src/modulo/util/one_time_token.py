"""Shared one-time-token primitives.

Used by the MCP setup handoff (``core.mcp_setup_handoff``) and the FAR-461
user invitations (``db.crud.invitations``): both mint a 256-bit urlsafe
plaintext that is shown to the recipient exactly once and persist only its
SHA-256 hex hash, so a database leak does not leak usable tokens. The token
format must never change — existing un-consumed tokens are only recoverable
via their plaintexts.

Lives in ``modulo.util`` because both the db layer (invitations) and core
(mcp_setup_handoff) depend on it and the import-linter contract keeps
``modulo.db`` free of ``modulo.core`` imports.
"""

import hashlib
import secrets


def generate_token() -> str:
    """Mint a 256-bit urlsafe one-time token plaintext (shown once, never stored)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex of a token plaintext — the value persisted at rest."""
    return hashlib.sha256(token.encode()).hexdigest()
