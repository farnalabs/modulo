"""Ed25519 signing utilities — hex-string-based API for registry protocol v2."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature

if TYPE_CHECKING:
    from collections.abc import Mapping
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "generate_keypair",
    "sign_primitive",
    "verify_signature",
]


def _canonical_json(obj: Mapping[str, object]) -> bytes:
    """Deterministic JSON serialisation for signing."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


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
    private_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    canonical = _canonical_json(primitive_data)
    sig = private_key.sign(canonical)
    return sig.hex()


def verify_signature(primitive_data: Mapping[str, object], signature_hex: str, public_key_hex: str) -> bool:
    """Verify an Ed25519 signature against *primitive_data*.

    Returns ``True`` if the signature is valid, ``False`` otherwise.
    """
    public_bytes = bytes.fromhex(public_key_hex)
    public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
    canonical = _canonical_json(primitive_data)
    try:
        public_key.verify(bytes.fromhex(signature_hex), canonical)
    except InvalidSignature:
        return False
    else:
        return True
