"""Unit tests for community-library manifest verification, canonical
serialisation, and parsing (FAR-363)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.unit.library_sync.conftest import signed_manifest as _signed_manifest

from modulo.core.library_sync.manifest import (
    canonical_manifest_bytes,
    parse_manifest,
    verify_manifest,
)
from modulo.registry.crypto import generate_keypair


class TestVerifyManifest:
    def test_accepts_valid_signed_manifest(self, keys: tuple[str, str]) -> None:
        private_key, public_key = keys
        manifest = _signed_manifest(private_key)
        assert verify_manifest(manifest, public_key) is True

    def test_rejects_tampered_entries(self, keys: tuple[str, str]) -> None:
        private_key, public_key = keys
        manifest = _signed_manifest(private_key, entries=[{"id": "agent-1", "type": "agent"}])
        assert verify_manifest(manifest, public_key) is True

        tampered = dict(manifest)
        tampered["entries"] = [{"id": "agent-1", "type": "agent", "evil": True}]
        assert verify_manifest(tampered, public_key) is False

    def test_rejects_signature_from_another_key(self, keys: tuple[str, str]) -> None:
        private_key, _ = keys
        _, other_public = generate_keypair()
        manifest = _signed_manifest(private_key)
        assert verify_manifest(manifest, other_public) is False

    def test_rejects_missing_signature(self, keys: tuple[str, str]) -> None:
        private_key, public_key = keys
        manifest = _signed_manifest(private_key)
        del manifest["signature"]
        assert verify_manifest(manifest, public_key) is False

    @pytest.mark.parametrize(
        "signature",
        [
            "not-a-dict",
            {"algorithm": "ed25519", "value": ""},
            {"algorithm": "ed25519", "value": "!!!not-base64!!!"},
        ],
        ids=["not-a-dict", "empty-value", "non-base64"],
    )
    def test_rejects_malformed_signature(self, keys: tuple[str, str], signature: object) -> None:
        _, public_key = keys
        manifest = {"schema_version": "1", "signature": signature}
        assert verify_manifest(manifest, public_key) is False

    def test_fail_closed_without_root_key(self) -> None:
        manifest = {"schema_version": "1"}
        assert verify_manifest(manifest, "") is False

    @pytest.mark.parametrize("kind", ["type-error", "value-error"], ids=["unserialisable-value", "circular-reference"])
    def test_returns_false_when_canonicalisation_fails(self, keys: tuple[str, str], kind: str) -> None:
        """A payload json.dumps cannot canonically serialise must fail closed.

        The guard sits around ``canonical_manifest_bytes``: without it the
        ``TypeError`` (an unserialisable value) or ``ValueError`` (a circular
        reference) escapes ``verify_manifest``'s never-raises contract.
        """
        _, public_key = keys
        manifest: dict[str, Any] = {
            "schema_version": "1",
            "entries": [],
            "revoked": [],
            "signature": {"algorithm": "ed25519", "value": "AA=="},
        }
        if kind == "type-error":
            manifest["entries"] = [object()]  # not JSON-serialisable
        else:
            manifest["self"] = manifest  # circular reference

        assert verify_manifest(manifest, public_key) is False


class TestCanonicalManifestBytes:
    def test_signature_field_is_excluded(self) -> None:
        manifest = {"entries": [], "signature": {"algorithm": "ed25519", "value": "abc"}}
        stripped = {"entries": []}
        expected = json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        assert canonical_manifest_bytes(manifest) == expected

    def test_is_deterministic(self) -> None:
        manifest = {"revoked": [{"id": "x"}], "entries": [{"id": "a"}], "schema_version": "1"}
        reordered = {"schema_version": "1", "entries": [{"id": "a"}], "revoked": [{"id": "x"}]}
        assert canonical_manifest_bytes(manifest) == canonical_manifest_bytes(reordered)


class TestParseManifest:
    def test_extracts_entries_and_revoked(self) -> None:
        data = parse_manifest(
            {
                "schema_version": "1",
                "generated_at": "2026-08-22T00:00:00Z",
                "entries": [{"id": "a"}],
                "revoked": [{"id": "b"}],
            }
        )
        assert data.schema_version == "1"
        assert data.generated_at == "2026-08-22T00:00:00Z"
        assert data.entries == [{"id": "a"}]
        assert data.revoked == [{"id": "b"}]

    def test_tolerates_missing_lists(self) -> None:
        data = parse_manifest({"schema_version": "1"})
        assert not data.entries
        assert not data.revoked
