"""FAR-675 — launcher/manifest.py: signed release-manifest verification.

Locks the ADR 031 Decision 3 trust-store contract: the ed25519 signature
verifies over the EXACT manifest bytes; a tampered manifest / wrong key /
unknown key-id refuses loudly; a missing or tampered artifact refuses; both
the current and the next trust-store keys verify (rotation); --from-file
verifies a locally provided manifest + signature against the same store.

Security posture (locked here): the SHIPPED store is EMPTY and every
verification FAILS CLOSED until the owner provisions production keys; the
private signing keys live in the test fixtures (conftest.py), never in the
shipped module; scripts/install.sh's embedded keys must equal _TRUST_ROWS
(no stored-byte drift between the two trust assemblies).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

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

_REPO_ROOT = Path(__file__).resolve().parents[4]
_INSTALL_SH_PATH = _REPO_ROOT / "scripts" / "install.sh"


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


def _write_release_files(
    artifacts_root: Path,
    body: dict[str, Any],
    *,
    key_hex: str,
    key_id: str,
) -> tuple[Path, Path]:
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


def test_signed_manifest_verifies(tmp_path: Path, test_trust_store: dict, test_signing_keys: dict) -> None:
    body = _manifest_body(artifact_count=2)
    _seed_artifacts(tmp_path, body)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    verified = verify_manifest(
        manifest_path.read_bytes(), read_signature_file(signature_path), trust_store=test_trust_store
    )
    assert verified.release == "bundle-v1.2.0"
    assert verified.components["postgres"] == "16.10.1"
    verify_artifacts(tmp_path, verified)


def test_next_key_signs_and_verifies(tmp_path: Path, test_trust_store: dict, test_signing_keys: dict) -> None:
    """Rotation: the NEXT trust-store key verifies what it signs."""
    body = _manifest_body(artifact_count=1)
    _seed_artifacts(tmp_path, body)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_NEXT],
        key_id=_KEY_ID_NEXT,
    )
    verified = verify_manifest(
        manifest_path.read_bytes(), read_signature_file(signature_path), trust_store=test_trust_store
    )
    assert verified.release == "bundle-v1.2.0"


def test_tampered_manifest_refuses(tmp_path: Path, test_trust_store: dict, test_signing_keys: dict) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    original = manifest_path.read_bytes()
    tampered = original.replace(b"bundle-v1.2.0", b"bundle-v9.9.9", 1)
    assert tampered != original
    with pytest.raises(ManifestSecurityError, match="FAILED to verify"):
        verify_signature(tampered, read_signature_file(signature_path), trust_store=test_trust_store)


def test_wrong_key_signature_refuses(tmp_path: Path, test_trust_store: dict, test_signing_key_hex: str) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, _ = _write_release_files(tmp_path, body, key_hex=test_signing_key_hex, key_id=_KEY_ID_CURRENT)
    forged = {
        "key_id": _KEY_ID_CURRENT,
        "signature": Ed25519PrivateKey.generate().sign(manifest_path.read_bytes()).hex(),
    }
    with pytest.raises(ManifestSecurityError, match="FAILED to verify"):
        verify_signature(manifest_path.read_bytes(), forged, trust_store=test_trust_store)


def test_unknown_key_id_refuses(tmp_path: Path, test_trust_store: dict, test_signing_key_hex: str) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, _ = _write_release_files(tmp_path, body, key_hex=test_signing_key_hex, key_id=_KEY_ID_CURRENT)
    payload = manifest_path.read_bytes()
    rogue = Ed25519PrivateKey.generate()
    misplaced = {"key_id": "rogue-not-in-store", "signature": rogue.sign(payload).hex()}
    with pytest.raises(ManifestSecurityError, match="rogue-not-in-store"):
        verify_signature(payload, misplaced, trust_store=test_trust_store)


def test_malformed_signature_file_refuses(tmp_path: Path) -> None:
    garbled = tmp_path / "not-a-sig"
    garbled.write_text("{ no key_id }", encoding="utf-8")
    with pytest.raises(ManifestSecurityError, match="garbled"):
        read_signature_file(garbled)


def test_missing_artifact_refuses(tmp_path: Path, test_trust_store: dict, test_signing_keys: dict) -> None:
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    verified = verify_manifest(
        manifest_path.read_bytes(), read_signature_file(signature_path), trust_store=test_trust_store
    )
    (tmp_path / "runtime-checksums-1.txt").unlink()
    with pytest.raises(ManifestSecurityError, match="missing"):
        verify_artifacts(tmp_path, verified)


def test_tampered_artifact_refuses(tmp_path: Path, test_trust_store: dict, test_signing_keys: dict) -> None:
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    verified = verify_manifest(
        manifest_path.read_bytes(), read_signature_file(signature_path), trust_store=test_trust_store
    )
    tampered_path = tmp_path / "runtime-checksums-1.txt"
    tampered_path.write_text("tampered bytes", encoding="utf-8")
    with pytest.raises(ManifestSecurityError, match="MISMATCH"):
        verify_artifacts(tmp_path, verified)


def test_manifest_artifact_name_escape_refuses(tmp_path: Path, test_trust_store: dict, test_signing_keys: dict) -> None:
    body = _manifest_body(artifact_count=1)
    body["artifacts"][0]["name"] = "../../escape.tgz"
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    verified = verify_manifest(
        manifest_path.read_bytes(), read_signature_file(signature_path), trust_store=test_trust_store
    )
    with pytest.raises(ManifestSecurityError, match="escapes the bundle root"):
        verify_artifacts(tmp_path, verified)


# ---------------------------------------------------------------------------
# Security posture: fail-closed shipped store, dev-key-free shipping, and
# no drift between the two trust assemblies (module + install.sh).
# ---------------------------------------------------------------------------


def test_shipped_verification_fails_closed_from_this_repo_file():
    """CRITICAL: the shipped module refuses with an EMPTY trust store.

    The test runs against the REAL _TRUST_ROWS (no patched store, no
    explicit trust_store argument) — verification must refuse with the
    provisioning message, never silently produce a verdict.
    """
    body = _manifest_body(artifact_count=1)
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    signature = {"key_id": _KEY_ID_CURRENT, "signature": "0" * 128}
    with pytest.raises(ManifestSecurityError, match="no provisioned production signing keys"):
        manifest_module.verify_signature(payload, signature)


def test_shipped_verify_manifest_fails_closed() -> None:
    body = _manifest_body(artifact_count=1)
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    signature = {"key_id": _KEY_ID_CURRENT, "signature": "0" * 128}
    assert not manifest_module._TRUST_ROWS, "the wheel must ship an EMPTY trust store"
    with pytest.raises(ManifestSecurityError, match="no provisioned production signing keys"):
        manifest_module.verify_manifest(payload, signature)


def test_no_private_signing_keys_in_shipped_module() -> None:
    """CRITICAL: no private key material ships anywhere in manifest.py."""
    module_source = Path(manifest_module.__file__).read_text(encoding="utf-8")
    assert "_SIGNING_KEYS" not in module_source, "private signing keys must not ship in the module"
    assert "private_key_hex: str" in module_source, "signing takes the key as an explicit argument only"


def _installer_embedded_keys() -> dict[str, str]:
    """Read install.sh's TRUST_KEY_*_B64 embeds (slot -> b64 or '')."""
    install_sh = (_REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    embedded: dict[str, str] = {}
    for slot in ("current", "next"):
        match = re.search(rf'TRUST_KEY_{slot.upper()}_B64="([^"]*)"', install_sh)
        if match is None:
            raise AssertionError(f"install.sh no longer embeds TRUST_KEY_{slot.upper()}_B64")
        embedded[slot] = match.group(1)
    return embedded


def _public_b64(public_hex: str) -> str:
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    return base64.b64encode(key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode("ascii")


def test_installer_embedded_store_matches_trust_rows() -> None:
    """CRITICAL: install.sh's embedded keys and _TRUST_ROWS cannot drift.

    Both assemblies must describe the SAME keypair set. At ship time BOTH
    are empty (fail-closed); when the owner provisions production keys this
    loop pins byte-equality between the two fill-in targets inwards.
    """
    embedded = _installer_embedded_keys()
    if not manifest_module._TRUST_ROWS:
        assert not embedded["current"]
        assert not embedded["next"]
        return
    for key_id, (_label, public_hex) in manifest_module._TRUST_ROWS.items():
        slot = "current" if key_id == _KEY_ID_CURRENT else "next"
        expected_b64 = _public_b64(public_hex)
        assert embedded[slot], f"install.sh slot '{slot}' ({key_id}) must be provisioned"
        assert embedded[slot] == expected_b64, (
            f"install.sh's embedded '{slot}' key differs from _TRUST_ROWS[{key_id}] — the two "
            "trust-store assemblies MUST carry the SAME public keys (both from one provisioning)"
        )


def test_sign_roundtrips_through_installersh_sig_extraction(
    tmp_path: Path, test_trust_store: dict, sig_factory: Any
) -> None:
    """Cross-artifact: install.sh's .sig extraction patterns (the same sed
    regexes it runs at install time) capture exactly what verify_signature
    accepts, and the signed bytes round-trip through the injected store."""
    payload = json.dumps(_manifest_body(1), indent=2, sort_keys=True).encode("utf-8")
    signature = sig_factory(payload)
    sig_path = tmp_path / "RELEASE_MANIFEST.json.sig"
    sig_path.write_text(json.dumps(signature), encoding="utf-8")
    sig_text = sig_path.read_text(encoding="utf-8")
    # The EXACT patterns install.sh extracts, verbatim from its sed/grep
    # (POSIX [[:space:]] -> Python \s; same capture semantics).
    key_id_match = re.search(r'"key_id"\s*:\s*"([^"]+)"', sig_text)
    sig_hex_match = re.search(r'"signature"\s*:\s*"([0-9a-fA-F]{128})"', sig_text)
    assert key_id_match is not None
    assert sig_hex_match is not None
    extracted = {"key_id": key_id_match.group(1), "signature": sig_hex_match.group(1)}
    assert verify_signature(payload, extracted, trust_store=test_trust_store) == _KEY_ID_CURRENT
