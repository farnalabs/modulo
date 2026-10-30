"""FAR-675 — launcher/manifest.py: signed release-manifest verification.

Locks the ADR 031 Decision 3 trust-store contract: the ed25519 signature
verifies over the EXACT manifest bytes; a tampered manifest / wrong key /
unknown key-id refuses loudly; a missing or tampered artifact refuses; both
the current and the next trust-store keys verify (rotation); --from-file
verifies a locally provided manifest + signature against the same store.

Security posture (locked here): the SHIPPED store carries the current
production public key and leaves the next rotation slot empty; private signing
keys live in the test fixtures (conftest.py), never in the shipped module;
scripts/install.sh's embedded keys must equal _TRUST_ROWS (no stored-byte drift
between the two trust assemblies).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
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
    ReleaseManifest,
    read_signature_file,
    sign_release_bytes,
    verify_artifacts,
    verify_manifest,
    verify_release_from_files,
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


def _json_bytes(value: Any) -> bytes:
    """Helper for the byte-shaped cases in the refusal matrix below."""
    return value if isinstance(value, bytes) else json.dumps(value, indent=2, sort_keys=True).encode("utf-8")


# (case-id, tamper(bytes-or-dict) -> bytes or dict, expected message)
PAYLOAD_REFUSALS = [
    ("not-json", lambda _body: b"no json here", "not valid JSON"),
    ("non-object", lambda _body: b"[]", "must be a JSON object"),
    (
        "missing-fields",
        lambda body: {k: v for k, v in body.items() if k != "components"},
        "missing field(s): components",
    ),
    ("bad-schema-version", lambda body: {**body, "manifest_version": 99}, "unsupported manifest_version"),
    (
        "components-not-object",
        lambda body: {**body, "components": ["16.10.1"]},
        "'components' must be an object and 'artifacts' a list",
    ),
    (
        "artifacts-not-list",
        lambda body: {**body, "artifacts": {}},
        "'components' must be an object and 'artifacts' a list",
    ),
    (
        "artifact-entry-not-object",
        lambda body: {**body, "artifacts": ["x"]},
        "every artifact entry must be an object",
    ),
    (
        "artifact-entry-missing-keys",
        lambda body: {**body, "artifacts": [{"name": "a.tar.gz", "sha256": "0" * 64}]},
        "every artifact entry must be an object",
    ),
    (
        "malformed-sha256",
        lambda body: {**body, "artifacts": [{**body["artifacts"][0], "sha256": "0" * 63}, *body["artifacts"][1:]]},
        "malformed sha256",
    ),
    ("zero-artifacts", lambda body: {**body, "artifacts": []}, "covers zero artifacts"),
]


@pytest.mark.parametrize(
    ("case_id", "tamper", "pattern"),
    PAYLOAD_REFUSALS,
    ids=[case[0] for case in PAYLOAD_REFUSALS],
)
def test_manifest_payload_refusal_matrix(
    tmp_path: Path,
    test_trust_store: dict,
    test_signing_keys: dict,
    case_id: str,
    tamper: Any,
    pattern: str,
) -> None:
    """Every _parse_manifest_payload refusal branch refuses with its message.

    The tampered payload is RE-SIGNED with a trusted key so the signature
    stage passes — each refusal must come from the PAYLOAD validation, not
    from signature mistrust (the signature refusal variants are the matrix
    below).
    """
    _ = case_id
    _ = tmp_path
    body = tamper(_manifest_body(artifact_count=2))
    payload = _json_bytes(body)
    signature = sign_release_bytes(payload, key_id=_KEY_ID_CURRENT, private_key_hex=test_signing_keys[_KEY_ID_CURRENT])
    with pytest.raises(ManifestSecurityError, match=re.escape(pattern)):
        verify_manifest(payload, signature, trust_store=test_trust_store)


SIGNATURE_REFUSALS = [
    ("not-a-dict", lambda sig: ["nope"], "signature must be an object"),
    ("wrong-length", lambda sig: {**sig, "signature": "ab" * 20}, "128 hex characters"),
    ("not-hex", lambda sig: {**sig, "signature": "z" * 128}, "signature is not valid hex"),
]


@pytest.mark.parametrize(
    ("case_id", "tamper", "pattern"),
    SIGNATURE_REFUSALS,
    ids=[case[0] for case in SIGNATURE_REFUSALS],
)
def test_manifest_signature_refusal_matrix(
    tmp_path: Path,
    test_trust_store: dict,
    test_signing_keys: dict,
    case_id: str,
    tamper: Any,
    pattern: str,
) -> None:
    """The verify_signature shape checks refuse each malformed envelope."""
    _ = case_id
    _seed_artifacts(tmp_path, _manifest_body(artifact_count=2))
    body = _manifest_body(artifact_count=2)
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    sig = tamper({"key_id": _KEY_ID_CURRENT, "signature": "0" * 128})
    with pytest.raises(ManifestSecurityError, match=re.escape(pattern)):
        verify_manifest(payload, sig, trust_store=test_trust_store)


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


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "C:relative/artifact.tgz",
        r"C:\\Windows\\System32\\drivers\\etc\\hosts",
        r"\\\\server\\share\\artifact.tgz",
        "nested//artifact.tgz",
        "nested/./artifact.tgz",
        "nested/../artifact.tgz",
    ],
)
def test_signed_manifest_rejects_non_bundle_path_shapes(
    tmp_path: Path, test_trust_store: dict, test_signing_keys: dict, name: str
) -> None:
    body = _manifest_body(artifact_count=1)
    body["artifacts"][0]["name"] = name
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    manifest_path = tmp_path / manifest_module.MANIFEST_NAME
    signature_path = tmp_path / manifest_module.SIGNATURE_NAME
    manifest_path.write_bytes(payload)
    signature_path.write_text(
        json.dumps(
            sign_release_bytes(payload, key_id=_KEY_ID_CURRENT, private_key_hex=test_signing_keys[_KEY_ID_CURRENT])
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestSecurityError, match="escapes the bundle root"):
        verify_release_from_files(
            manifest_path=manifest_path,
            signature_path=signature_path,
            artifacts_root=tmp_path,
            artifact_names=[name],
            trust_store=test_trust_store,
        )


def test_signed_manifest_rejects_symlinked_parent(tmp_path: Path) -> None:
    parent = tmp_path / "nested"
    outside = tmp_path / "outside"
    outside.mkdir()
    parent.symlink_to(outside, target_is_directory=True)
    manifest = ReleaseManifest(
        release="bundle-v1.2.0",
        platform="linux-amd64",
        generated_at="2026-09-10T00:00:00Z",
        components={},
        artifact_checksums={"nested/artifact.tgz": ("0" * 64, "1.2.0")},
    )
    with pytest.raises(ManifestSecurityError, match="symlinked parent"):
        verify_artifacts(tmp_path, manifest)


def test_signed_manifest_rejects_final_symlink_and_nonregular_artifacts(tmp_path: Path) -> None:
    outside = tmp_path / "outside.tgz"
    outside.write_text("external", encoding="utf-8")
    symlink = tmp_path / "symlink.tgz"
    symlink.symlink_to(outside)
    manifest = ReleaseManifest(
        release="bundle-v1.2.0",
        platform="linux-amd64",
        generated_at="2026-09-10T00:00:00Z",
        components={},
        artifact_checksums={"symlink.tgz": ("0" * 64, "1.2.0")},
    )
    with pytest.raises(ManifestSecurityError, match="not a regular file"):
        verify_artifacts(tmp_path, manifest)

    directory = tmp_path / "directory.tgz"
    directory.mkdir()
    directory_manifest = ReleaseManifest(
        release=manifest.release,
        platform=manifest.platform,
        generated_at=manifest.generated_at,
        components={},
        artifact_checksums={"directory.tgz": ("0" * 64, "1.2.0")},
    )
    with pytest.raises(ManifestSecurityError, match="not a regular file"):
        verify_artifacts(tmp_path, directory_manifest)


# ---------------------------------------------------------------------------
# --from-file path: verify_release_from_files (offline/corporate installs)
# ---------------------------------------------------------------------------


def test_verify_release_from_files_verifies_a_named_subset(
    tmp_path: Path, test_trust_store: dict, test_signing_keys: dict
) -> None:
    """The pre-extraction tarball check: one named artifact is verified."""
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    name = body["artifacts"][0]["name"]
    verified = verify_release_from_files(
        manifest_path=manifest_path,
        signature_path=signature_path,
        artifacts_root=tmp_path,
        artifact_names=[name],
        trust_store=test_trust_store,
    )
    assert isinstance(verified, ReleaseManifest)
    assert verified.release == "bundle-v1.2.0"
    assert verified.checksum_for(name) == body["artifacts"][0]["sha256"]


def test_verify_release_from_files_tampered_named_artifact_refuses(
    tmp_path: Path, test_trust_store: dict, test_signing_keys: dict
) -> None:
    body = _manifest_body(artifact_count=1)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    name = body["artifacts"][0]["name"]
    (tmp_path / name).write_text("tampered bytes", encoding="utf-8")
    with pytest.raises(ManifestSecurityError, match="sha256 MISMATCH"):
        verify_release_from_files(
            manifest_path=manifest_path,
            signature_path=signature_path,
            artifacts_root=tmp_path,
            artifact_names=[name],
            trust_store=test_trust_store,
        )


def test_verify_release_from_files_uncovered_artifact_refuses(
    tmp_path: Path, test_trust_store: dict, test_signing_keys: dict
) -> None:
    """An artifact the manifest does not describe is refused outright."""
    body = _manifest_body(artifact_count=1)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    with pytest.raises(ManifestSecurityError, match="does not cover"):
        verify_release_from_files(
            manifest_path=manifest_path,
            signature_path=signature_path,
            artifacts_root=tmp_path,
            artifact_names=["not-in-the-manifest.tgz"],
            trust_store=test_trust_store,
        )


def test_verify_release_from_files_none_verifies_every_artifact(
    tmp_path: Path, test_trust_store: dict, test_signing_keys: dict
) -> None:
    """artifact_names=None verifies every artifact the manifest covers."""
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    verified = verify_release_from_files(
        manifest_path=manifest_path,
        signature_path=signature_path,
        artifacts_root=tmp_path,
        artifact_names=None,
        trust_store=test_trust_store,
    )
    assert verified.release == "bundle-v1.2.0"


def test_verify_release_from_files_none_refuses_missing_artifact(
    tmp_path: Path, test_trust_store: dict, test_signing_keys: dict
) -> None:
    """artifact_names=None: a listed artifact missing from disk is a hard failure."""
    body = _manifest_body(artifact_count=2)
    manifest_path, signature_path = _write_release_files(
        tmp_path,
        body,
        key_hex=test_signing_keys[_KEY_ID_CURRENT],
        key_id=_KEY_ID_CURRENT,
    )
    missing = body["artifacts"][1]["name"]
    (tmp_path / missing).unlink()
    with pytest.raises(ManifestSecurityError, match="missing from the bundle"):
        verify_release_from_files(
            manifest_path=manifest_path,
            signature_path=signature_path,
            artifacts_root=tmp_path,
            artifact_names=None,
            trust_store=test_trust_store,
        )


# ---------------------------------------------------------------------------
# Security posture: provisioned shipped store, dev-key-free shipping, and
# no drift between the two trust assemblies (module + install.sh).
# ---------------------------------------------------------------------------


def test_shipped_trust_store_has_only_the_provisioned_current_key() -> None:
    assert set(manifest_module._TRUST_ROWS) == {_KEY_ID_CURRENT}
    label, public_hex = manifest_module._TRUST_ROWS[_KEY_ID_CURRENT]
    assert label == "Modulo production release signing key (2026-A)"
    assert len(public_hex) == 64
    assert bytes.fromhex(public_hex) == bytes.fromhex(
        "73f7dd1483fff0c0df45d706ae9ce7a68151942b8e2e8bc715b5d9beb719b5b0"
    )
    assert _KEY_ID_NEXT not in manifest_module._TRUST_ROWS


def test_shipped_verification_uses_the_provisioned_current_key() -> None:
    body = _manifest_body(artifact_count=1)
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    private_hex = os.environ.get("BUNDLE_MANIFEST_SIGNING_KEY")
    if private_hex is None:
        pytest.skip("BUNDLE_MANIFEST_SIGNING_KEY is not available in the test environment")
    signature = sign_release_bytes(payload, key_id=_KEY_ID_CURRENT, private_key_hex=private_hex)
    public_hex = manifest_module._TRUST_ROWS[_KEY_ID_CURRENT][1]
    assert (
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        == public_hex
    )
    assert manifest_module.verify_signature(payload, signature) == _KEY_ID_CURRENT


def test_shipped_verify_manifest_rejects_invalid_signature() -> None:
    body = _manifest_body(artifact_count=1)
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    signature = {"key_id": _KEY_ID_CURRENT, "signature": "0" * 128}
    assert set(manifest_module._TRUST_ROWS) == {_KEY_ID_CURRENT}
    with pytest.raises(ManifestSecurityError, match="FAILED to verify"):
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

    Both assemblies must describe the SAME keypair set. The current slot is
    provisioned and the next slot remains reserved; this pins byte-equality
    between the two trust-store assemblies.
    """
    embedded = _installer_embedded_keys()
    assert set(manifest_module._TRUST_ROWS) == {_KEY_ID_CURRENT}
    assert not embedded["next"]
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


def test_signed_manifest_rejects_missing_artifact_parent(tmp_path: Path) -> None:
    """A listed name whose parent directory is absent refuses (never silently skips)."""
    manifest = ReleaseManifest(
        release="bundle-v1.2.0",
        platform="linux-amd64",
        generated_at="2026-09-10T00:00:00Z",
        components={},
        artifact_checksums={"absent/artifact.tgz": ("0" * 64, "1.2.0")},
    )
    with pytest.raises(ManifestSecurityError, match="missing from the bundle"):
        verify_artifacts(tmp_path, manifest)


def test_signed_manifest_rejects_non_directory_artifact_parent(tmp_path: Path) -> None:
    """A parent that exists but is a regular file is refused, not followed."""
    (tmp_path / "nested").write_text("not a directory", encoding="utf-8")
    manifest = ReleaseManifest(
        release="bundle-v1.2.0",
        platform="linux-amd64",
        generated_at="2026-09-10T00:00:00Z",
        components={},
        artifact_checksums={"nested/artifact.tgz": ("0" * 64, "1.2.0")},
    )
    with pytest.raises(ManifestSecurityError, match="artifact parent is not a directory"):
        verify_artifacts(tmp_path, manifest)
