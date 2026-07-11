"""Ed25519 signing utilities — hex-string-based API for registry protocol v2."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_SERIALISATION_ERROR = "primitive_data contains non-serializable values"

__all__ = [
    "generate_keypair",
    "sign_primitive",
    "verify_signature",
]


def _canonical_json(obj: Mapping[str, object]) -> bytes:
    """Deterministic JSON serialisation for signing."""
    try:
        return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(_SERIALISATION_ERROR) from exc


def generate_keypair() -> dict[str, str]:
    """Generate a new Ed25519 keypair.

    Returns a dict with hex-encoded *private_key*, *public_key*, and a
    16-char hex *fingerprint* of the public key.
    """
    private = Ed25519PrivateKey.generate()
    public = private.public_key()

    private_hex: str = private.private_bytes_raw().hex()
    public_raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_hex: str = public_raw.hex()
    fingerprint: str = hashlib.sha256(public_raw).hexdigest()[:16]

    return {
        "private_key": private_hex,
        "public_key": public_hex,
        "fingerprint": fingerprint,
    }


def sign_primitive(primitive_data: Mapping[str, object], private_key_hex: str) -> str:
    """Sign *primitive_data* with an Ed25519 private key.

    Returns the hex-encoded Ed25519 signature.
    """
    try:
        private_bytes = bytes.fromhex(private_key_hex)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except ValueError:
        raise ValueError("invalid private key hex") from None
    try:
        canonical = _canonical_json(primitive_data)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    sig = private_key.sign(canonical)
    return sig.hex()


def verify_signature(primitive_data: Mapping[str, object], signature_hex: str, public_key_hex: str) -> bool:
    """Verify an Ed25519 signature against *primitive_data*.

    Returns ``True`` if the signature is valid, ``False`` otherwise.
    """
    try:
        public_bytes = bytes.fromhex(public_key_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        canonical = _canonical_json(primitive_data)
        public_key.verify(bytes.fromhex(signature_hex), canonical)
    except InvalidSignature:
        logger.warning("verify_signature: invalid signature for key %s[:8]", public_key_hex[:8])
        return False
    except ValueError:
        logger.warning("verify_signature: bad hex input (key or signature)")
        return False
    else:
        return True
