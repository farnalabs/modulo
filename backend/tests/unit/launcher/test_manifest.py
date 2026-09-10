"""FAR-675 — launcher/manifest.py: signed release-manifest verification.

Locks the ADR 031 Decision 3 trust-store contract: the ed25519 signature
verifies over the EXACT manifest bytes; a tampered manifest / wrong key /
unknown key-id refuses loudly; a missing or tampered artifact refuses; both
the current and the next trust-store keys verify (rotation); --from-file
verifies a locally provided manifest + signature against the same store.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modulo.launcher import manifest as manifest_module
from modulo.launcher.manifest import (
    _KEY_ID_CURRENT,
    _KEY_ID_NEXT,
    ManifestSecurityError,
    read_signature_file,
    sign_release_bytes,
    verify_artifacts,
    verify_manifest,
    verify_signature,
)

_SIGNING_KEY_HEX = "101acdeda0cd35fdb51f4dae6eff9838b2d07c97641e41a09deb72a2a1a2254d"
_NEXT_SIGNING_KEY_HEX = "f12a59a076a0adba5fd1962c3d99abec05b6ed9103be741b4fe7530145c342ac"


def _manifest_body(artifact_count: int = 2) -> dict[str, Any]:
    """A manifest body whose artifact digests are filled in once seeded."""
    items: list[dict[str, str]] = []
    for index in range(artifact_count):
        items.append(
            {
                "name": "modulo-1.2.0-linux-amd64.tar.gz" if index == 0 else f"runtime-checksums-{index}.txt",
                "sha256": "0" * 64,
                "version": "1.2.0",
            }
        )
    return {
        "manifest_version": 1,
        "release": "bundle-v1.2.0",
        "platform": "linux-amd64",
        "generated_at": "2026-09-10T00:00:00Z",
        "components": {"postgres": "16.10.1", "redis": "8.4.0", "python": "3.12.11"},
        "artifacts": items,
    }


def _write_release_files(artifacts_root: Path, body: dict[str, Any], *, key_hex: str, key_id: str) -> tuple[Path, Path]:
    """Seed artifacts + the manifest + a matching .sig; return (manifest, sig)."""
    for artifact in body["artifacts"]:
        name = artifact["name"]
        content = f"shipped bytes for {name}"
        artifact["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        (artifacts_root / name).write_text(content, encoding="utf-8")
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    manifest_path = artifacts_root / manifest_module.MANIFEST_NAME
    manifest_path.write_bytes(payload)
    signature = sign_release_bytes(payload, key_id=key_id, private_key_hex=key_hex)
    signature_path = artifacts_root / (manifest_module.MANIFEST_NAME + manifest_module.SIGNATURE_SUFFIX)
    signature_path.write_text(json.dumps(signature), encoding="utf-8")
    return manifest_path, signature_path


def _seed_artifacts(artifacts_root: Path, body: dict[str, Any]) -> None:
    for artifact in body["artifacts"]:
        name = artifact["name"]
        content = f"shipped bytes for {name}"
        artifact["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        (artifacts_root / name).write_text(content, encoding="utf-8")


def test_signed_manifest_verifies(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=2)
    _seed_artifacts(tmp_path, body)
    manifest_path, signature_path = _write_release_files(
        tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT
    )
    verified = verify_manifest(manifest_path.read_bytes(), read_signature_file(signature_path))
    assert verified.release == "bundle-v1.2.0"
    assert verified.components["postgres"] == "16.10.1"
    verify_artifacts(tmp_path, verified)


def test_next_key_signs_and_verifies(tmp_path: Path) -> None:
    """Rotation: the NEXT trust-store key verifies what it signs."""
    body = _manifest_body(artifact_count=1)
    _seed_artifacts(tmp_path, body)
    manifest_path, signature_path = _write_release_files(
        tmp_path, body, key_hex=_NEXT_SIGNING_KEY_HEX, key_id=_KEY_ID_NEXT
    )
    verified = verify_manifest(manifest_path.read_bytes(), read_signature_file(signature_path))
    assert verified.release == "bundle-v1.2.0"


def test_tampered_manifest_refuses(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, signature_path = _write_release_files(
        tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT
    )
    original = manifest_path.read_bytes()
    tampered = original.replace(b"bundle-v1.2.0", b"bundle-v9.9.9", 1)
    assert tampered != original
    with pytest.raises(ManifestSecurityError, match="FAILED to verify"):
        verify_signature(tampered, read_signature_file(signature_path))


def test_wrong_key_signature_refuses(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, _ = _write_release_files(tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT)
    forged = {
        "key_id": _KEY_ID_CURRENT,
        "signature": Ed25519PrivateKey.generate().sign(manifest_path.read_bytes()).hex(),
    }
    with pytest.raises(ManifestSecurityError, match="FAILED to verify"):
        verify_signature(manifest_path.read_bytes(), forged)


def test_unknown_key_id_refuses(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, _ = _write_release_files(tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT)
    payload = manifest_path.read_bytes()
    rogue = Ed25519PrivateKey.generate()
    misplaced = {"key_id": "rogue-not-in-store", "signature": rogue.sign(payload).hex()}
    with pytest.raises(ManifestSecurityError, match="rogue-not-in-store"):
        verify_signature(payload, misplaced)


def test_malformed_signature_file_refuses(tmp_path: Path) -> None:
    garbled = tmp_path / "not-a-sig"
    garbled.write_text("{ no key_id }", encoding="utf-8")
    with pytest.raises(ManifestSecurityError, match="garbled"):
        read_signature_file(garbled)


def test_missing_artifact_refuses(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT
    )
    verified = verify_manifest(manifest_path.read_bytes(), read_signature_file(signature_path))
    (tmp_path / "runtime-checksums-1.txt").unlink()
    with pytest.raises(ManifestSecurityError, match="missing"):
        verify_artifacts(tmp_path, verified)


def test_tampered_artifact_refuses(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT
    )
    verified = verify_manifest(manifest_path.read_bytes(), read_signature_file(signature_path))
    tampered_path = tmp_path / "runtime-checksums-1.txt"
    tampered_path.write_text("tampered bytes", encoding="utf-8")
    with pytest.raises(ManifestSecurityError, match="MISMATCH"):
        verify_artifacts(tmp_path, verified)


def test_manifest_artifact_name_escape_refuses(tmp_path: Path) -> None:
    body = _manifest_body(artifact_count=1)
    body["artifacts"][0]["name"] = "../../escape.tgz"
    manifest_path, signature_path = _write_release_files(
        tmp_path, body, key_hex=_SIGNING_KEY_HEX, key_id=_KEY_ID_CURRENT
    )
    verified = verify_manifest(manifest_path.read_bytes(), read_signature_file(signature_path))
    with pytest.raises(ManifestSecurityError, match="escapes the bundle root"):
        verify_artifacts(tmp_path, verified)
