"""Org-vault credential references for declarative config (FAR-1640).

A credential-bearing field (a model backend's ``api_key``, a connector's
``credentials``) may carry a *reference* to an org-vault entry instead of a
literal secret, so committed YAML / declarative config never contains the
value:

    secretref://<key>

The marker sits INSIDE the existing string field — the same "an alternative
value, not a new field" shape ``git+`` refs use in
:mod:`modulo.core.pipeline_engine.git_content` — so the REST contract and the
generated frontend API types are unchanged.

Resolution is SERVER-SIDE at write time, under the caller's organisation
context: the route resolves the key through
:meth:`modulo.core.secrets_backend.SecretsBackend.get_secret` and encrypts the
resolved value exactly as a literal credential would be. The value never
crosses a client boundary — the CLI only ever sends the key.

Fail closed
-----------
Every failure is a typed :class:`CredentialReferenceError` naming only the
offending key: a missing key, a key belonging to another organisation, an
unreadable/empty vault entry, or a malformed reference. There is no silent
fallback to storing the reference string as a literal credential, and a
resolution failure never becomes a 500.

Cross-org guard (FAR-1640)
--------------------------
The default Fernet backend partitions secrets by organisation
(``secrets.organisation_id``), so a reference can only ever resolve within the
caller's org. The externally-hosted backends (Vault, AWS Secrets Manager)
resolve a GLOBAL key namespace, so on a licensed multi-org deployment the same
key name could source another organisation's secret. Per-org namespacing cannot
be retrofitted to those backends without re-keying already-stored secrets (which
would silently break existing reads), so in a multi-org deployment the
reference path REFUSES such a backend fail-closed — a typed error, never a
cross-org read. Self-hosted single-org deployments are unaffected.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modulo.core.secrets_backend import SecretsBackend

#: Marker prefix that makes a credential field a vault reference.
SECRET_REF_SCHEME = "secretref://"


class CredentialReferenceError(ValueError):
    """A vault credential reference is malformed or cannot be resolved.

    ``key`` is the offending vault key (empty for a malformed reference that
    names no key); ``reason`` is a value-free explanation. Neither the reason
    nor ``str(exc)`` ever carries a secret value.
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"credential reference {key!r}: {reason}")

    def validation_detail(self, loc: list[str]) -> list[dict[str, Any]]:
        """FastAPI-style 422 detail list, naming the offending (and only that) key.

        ``type`` is a stable machine-readable code
        (``credential_reference_error``) so a client can branch on it without
        parsing prose; ``loc`` points at the request field.
        """
        return [
            {
                "type": "credential_reference_error",
                "loc": loc,
                "msg": f"vault key {self.key!r}: {self.reason}",
            }
        ]


def is_secret_ref(value: object) -> bool:
    """True when *value* is a string whose trimmed form starts with ``secretref://``."""
    return isinstance(value, str) and value.strip().startswith(SECRET_REF_SCHEME)


def _backend_is_org_scoped(secrets_backend: SecretsBackend) -> bool:
    """True when *secrets_backend* partitions secrets by organisation.

    The default Fernet backend stores ``secrets.organisation_id`` and scopes
    every read/write, so a foreign-org key is indistinguishable from a missing
    one. Externally-hosted backends (Vault, AWS) resolve a GLOBAL key namespace
    and keep the ``False`` default (see :class:`SecretsBackend`).
    """
    return bool(getattr(secrets_backend, "organisation_scoped", False))


def _multi_org_enabled() -> bool:
    """Whether this deployment is a licensed multi-org install.

    Read lazily so this low-level seam has no import-time dependency on the
    settings module, and so tests can patch this function directly. Defaults to
    ``False`` (self-hosted single-org), matching ``Settings``.
    """
    from modulo.settings import get_settings

    return bool(get_settings().modulo_multi_org_enabled)


def parse_secret_ref(value: str) -> str:
    """Parse ``secretref://<key>`` and return the bare vault key.

    Raises :class:`CredentialReferenceError` for a non-``secretref://`` value,
    an empty key, or a key containing whitespace — a value that STARTS with the
    marker must parse completely (no partial-accept path that would store the
    raw reference as a credential).
    """
    if not isinstance(value, str):
        msg = f"credential reference must be a string, got {type(value).__name__}"
        raise CredentialReferenceError("", msg)
    raw = value.strip()
    if not raw.startswith(SECRET_REF_SCHEME):
        raise CredentialReferenceError("", f"credential reference must start with {SECRET_REF_SCHEME!r}")
    key = raw[len(SECRET_REF_SCHEME) :].strip()
    if not key:
        raise CredentialReferenceError("", "credential reference is empty")
    if any(ch.isspace() for ch in key):
        raise CredentialReferenceError(key, "vault key must not contain whitespace")
    return key


async def resolve_credential_reference(
    secrets_backend: SecretsBackend,
    reference: str,
) -> str:
    """Resolve a ``secretref://<key>`` reference to its value under the org context.

    Raises :class:`CredentialReferenceError` (never KeyError, never a silent
    empty string) when the key is missing, belongs to another organisation,
    cannot be read, or resolves to an empty value.
    """
    key = parse_secret_ref(reference)
    if _multi_org_enabled() and not _backend_is_org_scoped(secrets_backend):
        # FAR-1640 (cross-org read guard): externally-hosted backends (Vault,
        # AWS Secrets Manager) resolve a GLOBAL key namespace — the same key
        # name is the same secret for every organisation. Per-org namespacing
        # cannot be added without re-keying already-stored secrets (which would
        # silently break existing reads), so in a multi-org deployment we refuse
        # the reference path outright rather than risk reading another
        # organisation's secret. Self-hosted single-org installs
        # (``modulo_multi_org_enabled=False``, the default) are unaffected.
        # Raised BEFORE any backend read — never a cross-org read.
        raise CredentialReferenceError(
            key,
            "external secrets backends are not organisation-scoped, so credential "
            "references are refused in a multi-org deployment",
        )
    try:
        value = await secrets_backend.get_secret(key)
    except asyncio.CancelledError:
        raise
    except KeyError:
        raise CredentialReferenceError(key, "vault key not found in this organisation") from None
    except Exception as exc:
        # Unreadable vault (backend outage, decrypt failure, RLS unset) fails
        # closed as a typed error naming the key — never a raw 500.
        raise CredentialReferenceError(key, f"vault key could not be read ({type(exc).__name__})") from None
    if not isinstance(value, str) or not value:
        raise CredentialReferenceError(key, "vault key resolved to an empty value")
    return value


async def resolve_credential(
    secrets_backend: SecretsBackend,
    value: str,
) -> str:
    """Return the usable credential for a write.

    A literal passes through untouched (zero cost, no vault read); a
    ``secretref://<key>`` token is resolved server-side against the org vault.
    """
    if not is_secret_ref(value):
        return value
    return await resolve_credential_reference(secrets_backend, value)


__all__ = [
    "SECRET_REF_SCHEME",
    "CredentialReferenceError",
    "is_secret_ref",
    "parse_secret_ref",
    "resolve_credential",
    "resolve_credential_reference",
]
