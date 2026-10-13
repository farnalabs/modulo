"""Ed25519 signing of the evidence manifest (FAR-1697).

Reuses the established Ed25519 primitives — ``modulo.registry.crypto.sign`` /
``verify`` (PEM keys, base64 signatures) — the same machinery the community
registry and the hosted-library manifest already use. No new crypto: the pack
adds a canonical payload and a published key, not a signing algorithm.

Key policy (mirrors :mod:`modulo.core.license_signing`): the signing key is
resolved from ``MODULO_EVIDENCE_SIGNING_KEY`` (a PEM-encoded Ed25519 private
key). It is never hardcoded, and signing FAILS CLOSED when no key is
configured — an instance that has not opted in cannot emit signed evidence.
The public verification key is derived from the private key and published both
as an artefact in the bundle and via the admin verification-key endpoint.
"""

from __future__ import annotations

import hashlib
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modulo.core.evidence_pack.canonical import canonical_json_bytes
from modulo.registry.crypto import sign as _ed25519_sign
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

__all__ = [
    "EvidenceSigningError",
    "configured_public_key_pem",
    "key_fingerprint",
    "public_key_pem_from_private",
    "resolve_signing_key_pem",
    "sign_manifest",
]


class EvidenceSigningError(ValueError):
    """Raised when the evidence manifest cannot be signed (e.g. no key)."""


def resolve_signing_key_pem(private_key_pem: str | None = None) -> str:
    """Resolve the PEM signing key from the argument or ``Settings``.

    Fails closed (raises :class:`EvidenceSigningError`) when neither is set.
    """
    if private_key_pem:
        return private_key_pem
    configured = get_settings().modulo_evidence_signing_key
    if configured:
        return configured
    raise EvidenceSigningError(
        "No evidence signing key configured — set MODULO_EVIDENCE_SIGNING_KEY "
        "(a PEM-encoded Ed25519 private key) to emit signed evidence packs.",
    )


def public_key_pem_from_private(private_key_pem: str) -> str:
    """Derive the PEM-encoded public key from a PEM Ed25519 private key."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise EvidenceSigningError("the evidence signing key is not an Ed25519 private key")
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def key_fingerprint(public_key_pem: str) -> str:
    """Return a short stable fingerprint of a PEM public key.

    SHA-256 over the DER SubjectPublicKeyInfo (the transport encoding), first
    16 hex characters — enough to identify the key, never a secret.
    """
    der = serialization.load_pem_public_key(public_key_pem.encode()).public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:16]


def configured_public_key_pem() -> str:
    """Return the PEM public key derived from the configured signing key."""
    return public_key_pem_from_private(resolve_signing_key_pem())


def sign_manifest(manifest: dict[str, object], private_key_pem: str) -> dict[str, str]:
    """Sign the canonical manifest bytes; return the signature block.

    The block carries ``algorithm``, the base64 ``value`` and the ``key_id``
    fingerprint, so a verifier can tell which published key is expected.
    """
    try:
        signature_b64 = _ed25519_sign(private_key_pem, canonical_json_bytes(manifest))
    except (TypeError, ValueError) as exc:
        _log.warning("evidence_pack.sign.failed")
        raise EvidenceSigningError(f"could not sign the evidence manifest: {exc}") from exc
    public_key_pem = public_key_pem_from_private(private_key_pem)
    return {
        "algorithm": "ed25519",
        "value": signature_b64,
        "key_id": key_fingerprint(public_key_pem),
    }
