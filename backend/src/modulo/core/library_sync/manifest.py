"""Manifest verification for the hosted community library (FAR-363).

The vendor (library.modulo.run) publishes a signed Ed25519 manifest:

    {
      "schema_version": "...",
      "generated_at": "...",
      "entries": [{id, type, slug, author, version, content_sha256, license, status, published_at}],
      "revoked":   [{id, type, slug, version, reason, revoked_at}],
      "signature": {"algorithm": "ed25519", "value": "<base64>"}
    }

The signature covers the canonical JSON of the manifest with the ``signature``
field removed (``sort_keys=True``, compact separators). The product bundles the
root public key (``MODULO_LIBRARY_ROOT_PUBLIC_KEY``) and verifies the manifest
before trusting any of its contents.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from modulo.registry.crypto import verify as _verify_ed25519

__all__ = [
    "ManifestData",
    "canonical_manifest_bytes",
    "parse_manifest",
    "verify_manifest",
]

_log = logging.getLogger(__name__)

_SIGNATURE_KEY = "signature"


@dataclass
class ManifestData:
    """Structured view of a verified community-library manifest."""

    schema_version: str | None = None
    generated_at: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    revoked: list[dict[str, Any]] = field(default_factory=list)


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Serialise a manifest to the canonical bytes covered by its signature.

    The ``signature`` field is excluded (it cannot sign itself); keys are
    sorted and separators are compact so both the signer (vendor) and verifier
    (product) produce byte-identical output.
    """
    stripped = {k: v for k, v in manifest.items() if k != _SIGNATURE_KEY}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_manifest(manifest: dict[str, Any], root_public_key_pem: str) -> bool:
    """Verify the Ed25519 signature over the canonical manifest bytes.

    Fail-closed: returns False on any malformed signature block, missing
    signature, or invalid signature — never raises.
    """
    if not root_public_key_pem:
        _log.warning("library_sync.verify.no_root_key")
        return False
    signature_block = manifest.get(_SIGNATURE_KEY)
    if not isinstance(signature_block, dict):
        _log.warning("library_sync.verify.missing_signature")
        return False
    signature_value = signature_block.get("value")
    if not isinstance(signature_value, str) or not signature_value:
        _log.warning("library_sync.verify.empty_signature")
        return False
    try:
        canonical = canonical_manifest_bytes(manifest)
    except (TypeError, ValueError) as exc:
        _log.warning("library_sync.verify.canonicalisation_failed", extra={"reason": str(exc)})
        return False
    return _verify_ed25519(root_public_key_pem, canonical, signature_value)


def parse_manifest(manifest: dict[str, Any]) -> ManifestData:
    """Extract the structured entries/revoked lists from a verified manifest.

    Tolerant of missing or malformed lists (returns empty lists rather than
    raising) so a verified-but-incomplete manifest degrades gracefully.
    """
    entries = manifest.get("entries")
    revoked = manifest.get("revoked")
    return ManifestData(
        schema_version=_optional_str(manifest.get("schema_version")),
        generated_at=_optional_str(manifest.get("generated_at")),
        entries=list(entries) if isinstance(entries, list) else [],
        revoked=list(revoked) if isinstance(revoked, list) else [],
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
