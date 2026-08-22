"""Shared fixtures and helpers for the library_sync unit tests (FAR-363).

These were duplicated across the individual ``test_*.py`` modules in this
directory; centralising them removes the cross-file duplication flagged by
SonarCloud's new-code duplication gate.
"""

from __future__ import annotations

import pytest

from modulo.core.library_sync.manifest import canonical_manifest_bytes
from modulo.registry.crypto import generate_keypair, sign


def signed_manifest(private_key_pem: str, **overrides: object) -> dict:
    """Build a manifest whose signature covers the canonical body (no signature key)."""
    body: dict[str, object] = {
        "schema_version": "1",
        "generated_at": "2026-08-22T00:00:00Z",
        "entries": [],
        "revoked": [],
    }
    body.update(overrides)
    canonical = canonical_manifest_bytes(body)
    return {**body, "signature": {"algorithm": "ed25519", "value": sign(private_key_pem, canonical)}}


@pytest.fixture
def keys() -> tuple[str, str]:
    """Return ``(private_key_pem, public_key_pem)``."""
    return generate_keypair()
