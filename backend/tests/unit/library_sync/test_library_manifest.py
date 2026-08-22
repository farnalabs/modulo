"""Unit tests for community-library manifest verification (FAR-363)."""

from __future__ import annotations

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

    def test_rejects_malformed_signature_block(self, keys: tuple[str, str]) -> None:
        _, public_key = keys
        manifest = {"schema_version": "1", "signature": "not-a-dict"}
        assert verify_manifest(manifest, public_key) is False

    def test_rejects_empty_signature_value(self, keys: tuple[str, str]) -> None:
        _, public_key = keys
        manifest = {"schema_version": "1", "signature": {"algorithm": "ed25519", "value": ""}}
        assert verify_manifest(manifest, public_key) is False

    def test_rejects_non_base64_signature(self, keys: tuple[str, str]) -> None:
        _, public_key = keys
        manifest = {"schema_version": "1", "signature": {"algorithm": "ed25519", "value": "!!!not-base64!!!"}}
        assert verify_manifest(manifest, public_key) is False

    def test_fail_closed_without_root_key(self) -> None:
        manifest = {"schema_version": "1"}
        assert verify_manifest(manifest, "") is False


class TestCanonicalManifestBytes:
    def test_signature_field_is_excluded(self) -> None:
        manifest = {"entries": [], "signature": {"algorithm": "ed25519", "value": "abc"}}
        canonical = canonical_manifest_bytes(manifest)
        assert b"signature" not in canonical

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
