"""Email normalisation helpers.

This module lives in the dependency-free ``modulo.util`` leaf so the DB, core
and API layers can all import it without violating the import-linter layer
contracts (same pattern as ``modulo.util``'s other shared utilities).

FAR-584: email addresses are case-insensitive in the real world. Every
account boundary (creation, lookup, SCIM/SSO provisioning, seeds) stores and
compares emails through :func:`normalize_email`, and the database enforces
case-insensitive uniqueness via the ``uq_accounts_email_lower`` functional
unique index (migration 0176).
"""

from __future__ import annotations

__all__ = ["normalize_email"]


def normalize_email(email: str) -> str:
    """Return the canonical form of an email address: trimmed and lowercased.

    The canonical form is what gets stored on every account write and what
    every email-keyed lookup compares against, so ``User@Example.COM`` and
    ``user@example.com`` are the same account everywhere.

    Unicode divergence (documented, no behaviour change): Python's
    ``str.lower()`` and Postgres ``LOWER()`` can disagree for exotic non-ASCII
    local parts (e.g. dotted/dotless İ under some collations). Emails are
    treated as ASCII-canonical in practice; because the DB index uses
    ``LOWER(email)``, a pathological Unicode case-fold mismatch would surface
    as an IntegrityError (mapped to 409) rather than a silent duplicate.
    """
    return email.strip().lower()
