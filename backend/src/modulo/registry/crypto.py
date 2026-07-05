"""Ed25519 signing utilities for the community registry — PEM/base64 API.

Provides key generation, signing, verification, and trust anchor support
for the community library registry. Uses PEM-encoded keys and base64
signatures.
"""

from __future__ import annotations

import base64
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "generate_keypair",
    "get_trust_anchor_public_key_pem",
    "sign",
    "sign_with_trust_anchor",
    "verify",
    "verify_trust_anchor",
]

logger = logging.getLogger(__name__)


def _public_key_to_pem(key: Ed25519PublicKey) -> str:
    """Serialize an Ed25519 public key to PEM string."""
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair.

    Returns:
        Tuple of (private_key_pem, public_key_pem).
    """
    private = Ed25519PrivateKey.generate()
    public = private.public_key()

    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_pem = _public_key_to_pem(public)

    return (private_pem, public_pem)


def _load_private_key(private_key_pem: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM string."""
    key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None,
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Key is not an Ed25519 private key")
    return key


def _load_public_key(public_key_pem: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key from PEM string."""
    key = serialization.load_pem_public_key(public_key_pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("Key is not an Ed25519 public key")
    return key


def sign(private_key_pem: str, data: bytes) -> str:
    """Sign *data* with an Ed25519 private key (PEM).

    Returns:
        Base64-encoded signature.
    """
    private_key = _load_private_key(private_key_pem)
    sig = private_key.sign(data)
    return base64.b64encode(sig).decode()


def _safe_base64_decode(signature: str) -> bytes | None:
    """Decode a base64 signature string, returning None on invalid input."""
    try:
        return base64.b64decode(signature)
    except (TypeError, ValueError) as exc:
        logger.debug("Invalid base64 signature: %s", exc)
        return None


def verify(public_key_pem: str, data: bytes, signature: str) -> bool:
    """Verify an Ed25519 signature.

    Returns:
        True if the signature is valid, False otherwise.
    """
    sig_bytes = _safe_base64_decode(signature)
    if sig_bytes is None:
        return False

    try:
        public_key = _load_public_key(public_key_pem)
        public_key.verify(sig_bytes, data)
        return True
    except InvalidSignature:
        return False
    except (TypeError, ValueError) as exc:
        logger.debug("Signature verification failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Trust anchor — root of trust for the registry
# ---------------------------------------------------------------------------

_trust_anchor: Ed25519PrivateKey | None = None


def _get_trust_anchor() -> Ed25519PrivateKey:
    """Return the module-level trust anchor, creating it if needed."""
    global _trust_anchor
    if _trust_anchor is None:
        _trust_anchor = Ed25519PrivateKey.generate()
    return _trust_anchor


def get_trust_anchor_public_key_pem() -> str:
    """Return the trust anchor's PEM-encoded public key."""
    anchor = _get_trust_anchor()
    return _public_key_to_pem(anchor.public_key())


def sign_with_trust_anchor(public_key_pem: str) -> str:
    """Sign a PEM-encoded public key with the trust anchor's private key.

    Returns:
        Base64-encoded signature of the public key.
    """
    anchor = _get_trust_anchor()
    sig = anchor.sign(public_key_pem.encode())
    return base64.b64encode(sig).decode()


def verify_trust_anchor(
    public_key_pem: str,
    signature: str,
    trust_anchor_public_key_pem: str | None = None,
) -> bool:
    """Verify that a public key is signed by the registry's trust anchor.

    Args:
        public_key_pem: The PEM-encoded public key to verify.
        signature: Base64-encoded signature from the trust anchor.
        trust_anchor_public_key_pem: Trust anchor's public key. If
            None, uses the built-in development trust anchor.

    Returns:
        True if the signature is valid, False otherwise.
    """
    anchor_pem = trust_anchor_public_key_pem or get_trust_anchor_public_key_pem()
    return verify(anchor_pem, public_key_pem.encode(), signature)
