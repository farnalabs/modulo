"""Signed release-manifest verification (FAR-675, ADR 031 Decision 3).

Every bundle release ships a JSON release manifest covered by every shipped
artifact (the bundle tarball plus the major bundled binaries: CPython runtime,
PostgreSQL server/client, Redis server), each entry carrying a ``sha256`` and
a ``version``. The manifest is signed with ed25519; the signature file
(``RELEASE_MANIFEST.json.sig``) carries the signing key-id so the trust store
can resolve the key:

.. code-block:: json

    {"key_id": "modulo-2026-a", "signature": "<128 hex chars>"}

Verification contract (locked by ``tests/unit/launcher/test_manifest.py``):

* the SIGNATURE is verified first (Ed25519 over the exact manifest bytes);
* then every listed ARTIFACT's sha256 is verified against the files on disk
  (a missing or tampered artifact is a hard failure);
* the trust store holds the CURRENT key and the NEXT key — both verify, so
  releasing under the next key mid-rotation works before consumers update.

**Key rotation procedure** (recorded per ADR 031 in the distribution ADR):
the CURRENT and NEXT trust-store keys — both verify, so releasing under the
next key mid-rotation works before consumers update.

1. Generate the replacement keypair offline; publish its PUBLIC half by
   moving the next key's id/key into the current slot of the trust store
   (_TRUST_ROWS below). The checked-in module is the distribution point.
2. Flip release signing to the new key (CI secret ``BUNDLE_MANIFEST_SIGNING_KEY``).
3. After one full release cycle under the new key, demote the old key to the
   next slot only if unwanted, then hard-remove it — every consumer older
   than one cycle stops verifying with it by design.

**PROVISIONING:** the current production key is provisioned in this module;
the reserved NEXT slot remains empty until the next rotation. The trust store
contains no dev or experimental signing keys. See :const:`PROVISIONING_NOTE`
for the rotation procedure and the matching installer trust store.

**--from-file escape hatch:** offline/corporate installs verify against a
locally provided manifest + signature (:func:`verify_release_from_files`);
the trust store stays baked in — an offline admin never adds their own key.

The launcher-owned public surface (consumed by install.sh via the bundled
runtime; vulture's dead-code gate special-cases __all__).
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MANIFEST_NAME = "RELEASE_MANIFEST.json"
SIGNATURE_SUFFIX = ".sig"
SIGNATURE_NAME = "RELEASE_MANIFEST.json.sig"
MANIFEST_SCHEMA_VERSION = 1
_RELEASE = "modulo"

# ---------------------------------------------------------------------------
# Trust store (current production key provisioned; next slot reserved)
# ---------------------------------------------------------------------------

_KEY_ID_CURRENT = "modulo-2026-a"
_KEY_ID_NEXT = "modulo-2026-a-next"

# The current production public key is intentionally shipped here. The NEXT
# slot stays absent until the next rotation; test/CI keys are injected
# explicitly by the test suite (see backend/tests/unit/launcher/conftest.py).
# Signing never reads a private key from this module.
_TRUST_ROWS: dict[str, tuple[str, str]] = {
    _KEY_ID_CURRENT: (
        "Modulo production release signing key (2026-A)",
        "73f7dd1483fff0c0df45d706ae9ce7a68151942b8e2e8bc715b5d9beb719b5b0",
    ),
}

_NO_PROVISIONED_KEYS_MESSAGE = (
    "no provisioned production signing keys - refusing to verify (provisioning: see PROVISIONING_NOTE)"
)

PROVISIONING_NOTE = (
    "TRUST-STORE PROVISIONING: the current production key is provisioned in "
    "the shipped trust store under modulo-2026-a; the modulo-2026-a-next slot "
    "is reserved and currently empty. The two trust-store assemblies must stay "
    "byte-equal:\n"
    "  1. backend/src/modulo/launcher/manifest.py -> _TRUST_ROWS: one row per "
    "key as {key_id: (label, ed25519 public hex)} for the current + next slots.\n"
    "  2. scripts/install.sh -> TRUST_KEY_CURRENT_B64 / TRUST_KEY_NEXT_B64: "
    "the same public keys as SPKI-DER base64.\n"
    "For rotation, generate the replacement keypair offline, place its public "
    "half in the reserved next slot, release once with both keys trusted, then "
    "promote it to current and remove the retired key after the rotation window.\n"
    "The PRIVATE half is the CI secret BUNDLE_MANIFEST_SIGNING_KEY (ed25519 "
    "hex, repo-level Actions secret) - never committed. When the secret is "
    "absent the bundle-release workflow refuses to publish."
)


def _load_trust_store() -> dict[str, Ed25519PublicKey]:
    """Build the shipped trust store from the provisioned public-key rows.

    The caller (verify_signature) refuses on an empty store: verification
    must never be shipped with "no key can pass" silently bridged over.
    """
    store: dict[str, Ed25519PublicKey] = {}
    for key_id, (_label, public_hex) in _TRUST_ROWS.items():
        store[key_id] = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    return store


class ManifestSecurityError(RuntimeError):
    """Raised when a manifest or artifact fails verification."""


@dataclass(frozen=True)
class ReleaseManifest:
    """One parsed + signature-verified release manifest."""

    release: str
    platform: str
    generated_at: str
    components: dict[str, str]
    artifact_checksums: dict[str, tuple[str, str]]  # name -> (sha256, version)

    def checksum_for(self, artifact_name: str) -> str:
        entry = self.artifact_checksums.get(artifact_name)
        if entry is None:
            raise ManifestSecurityError(
                f"the manifest does not cover '{artifact_name}' — refusing an artifact it does not describe"
            )
        return entry[0]


def _digest_of(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_manifest_payload(payload: bytes) -> ReleaseManifest:
    try:
        body = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise ManifestSecurityError(f"release manifest is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ManifestSecurityError("release manifest must be a JSON object")
    required = ("manifest_version", "release", "platform", "generated_at", "components", "artifacts")
    missing = sorted(set(required) - set(body))
    if missing:
        raise ManifestSecurityError(f"release manifest is missing field(s): {', '.join(missing)}")
    if body["manifest_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestSecurityError(f"unsupported manifest_version: {body['manifest_version']!r}")
    components = body["components"]
    artifacts_raw = body["artifacts"]
    if not isinstance(components, dict) or not isinstance(artifacts_raw, list):
        raise ManifestSecurityError("manifest 'components' must be an object and 'artifacts' a list")
    checksums: dict[str, tuple[str, str]] = {}
    for entry in artifacts_raw:
        if not isinstance(entry, dict) or {"name", "sha256", "version"} - set(entry):
            raise ManifestSecurityError("every artifact entry must be an object with name, sha256 and version")
        sha = entry["sha256"]
        if not isinstance(sha, str) or len(sha) != 64:
            raise ManifestSecurityError(f"artifact '{entry['name']}' carries a malformed sha256")
        checksums[str(entry["name"])] = (sha.lower(), str(entry["version"]))
    if not checksums:
        raise ManifestSecurityError("the manifest covers zero artifacts — refusing it")
    return ReleaseManifest(
        release=str(body["release"]),
        platform=str(body["platform"]),
        generated_at=str(body["generated_at"]),
        components={str(k): str(v) for k, v in components.items()},
        artifact_checksums=checksums,
    )


def verify_signature(
    manifest_payload: bytes,
    signature: dict[str, Any],
    *,
    trust_store: dict[str, Ed25519PublicKey] | None = None,
) -> str:
    """Verify Ed25519 over *manifest_payload*; return the verified key's id.

    FAIL-CLOSED: an empty/falsy store refuses before any signature is
    touch-checked (see PROVISIONING_NOTE). Tests inject their independent test
    trust store explicitly via ``trust_store=``.
    """
    store = trust_store if trust_store is not None else _load_trust_store()
    if not store:
        raise ManifestSecurityError(_NO_PROVISIONED_KEYS_MESSAGE)
    if not isinstance(signature, dict) or {"key_id", "signature"} - set(signature):
        raise ManifestSecurityError("signature must be an object with key_id and signature")
    key_id = str(signature["key_id"])
    public_key = store.get(key_id)
    if public_key is None:
        raise ManifestSecurityError(
            f"unknown signing key-id '{key_id}' — not in the current+next trust store; refusing"
        )
    sig_text = str(signature["signature"])
    if len(sig_text) != 128:
        raise ManifestSecurityError("ed25519 signatures are 128 hex characters")
    try:
        raw = bytes.fromhex(sig_text)
    except ValueError as exc:
        raise ManifestSecurityError("ed25519 signature is not valid hex") from exc
    try:
        public_key.verify(raw, manifest_payload)
    except InvalidSignature as exc:
        raise ManifestSecurityError(
            f"release-manifest signature FAILED to verify under key '{key_id}' — the manifest may "
            "have been tampered with"
        ) from exc
    return key_id


def verify_manifest(
    manifest_payload: bytes,
    signature: dict[str, Any],
    *,
    trust_store: dict[str, Ed25519PublicKey] | None = None,
) -> ReleaseManifest:
    """Parse the payload and verify the signature; return the manifest."""
    key_id = verify_signature(manifest_payload, signature, trust_store=trust_store)
    manifest = _parse_manifest_payload(manifest_payload)
    if manifest.release == "":
        raise ManifestSecurityError("manifest release is empty")
    del key_id
    return manifest


_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


def _resolve_bundle_artifact(root: Path, name: str) -> Path:
    """Validate *name* before resolving or touching any bundle filesystem path."""
    parts = name.split("/")
    if (
        not name
        or "\\" in name
        or name.startswith(("/", "//"))
        or _WINDOWS_DRIVE_PATH.match(name)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ManifestSecurityError(f"artifact name escapes the bundle root: {name!r}")

    root_resolved = root.resolve(strict=True)
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise ManifestSecurityError(f"artifact '{name}' missing from the bundle") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise ManifestSecurityError(f"artifact name uses a symlinked parent: {name!r}")
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ManifestSecurityError(f"artifact parent is not a directory: {name!r}")

    artifact = current / parts[-1]
    try:
        artifact_stat = artifact.lstat()
    except OSError as exc:
        raise ManifestSecurityError(f"artifact '{name}' missing from the bundle") from exc
    if stat.S_ISLNK(artifact_stat.st_mode) or not stat.S_ISREG(artifact_stat.st_mode):
        raise ManifestSecurityError(f"artifact '{name}' is not a regular file")
    resolved = artifact.resolve(strict=True)
    if not resolved.is_relative_to(root_resolved):  # pragma: no cover - lstat/resolve TOCTOU guard
        raise ManifestSecurityError(f"artifact name escapes the bundle root: {name!r}")
    return resolved


def verify_artifacts(root: Path, manifest: ReleaseManifest) -> None:
    """Verify every listed artifact's sha256 against the on-disk bundle.

    Runs AFTER signature verification (callers enforce that order): a
    signature over a manifest whose files do not match proves nothing about
    the artifacts. A missing artifact file is a failure, never a skip.
    """
    for name, (expected, _version) in sorted(manifest.artifact_checksums.items()):
        artifact = _resolve_bundle_artifact(root, name)
        actual = _digest_of(artifact)
        if actual != expected:
            raise ManifestSecurityError(
                f"artifact '{name}' sha256 MISMATCH: expected {expected}, got {actual} — do not "
                f"use this download; report it at https://github.com/farnalabs/modulo/issues"
            )


def build_release_manifest(
    *,
    release: str,
    platform: str,
    generated_at: str,
    components: dict[str, str],
    artifacts: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble a canonical manifest body (CI calls it with exact args)."""
    return {
        "manifest_version": MANIFEST_SCHEMA_VERSION,
        "release": release,
        "platform": platform,
        "generated_at": generated_at,
        "components": dict(components),
        "artifacts": list(artifacts),
    }


def sign_release_manifest(manifest: dict[str, Any], *, key_id: str, private_key_hex: str) -> dict[str, str]:
    """Return the .sig object for the canonical dump of *manifest*.

    The signed payload is the exact ``json.dumps(..., indent=2, sort_keys=True)``
    encoding; whatever writes the manifest file MUST write those same bytes.
    The PRIVATE key travels ONLY as the explicit *private_key_hex* argument
    (CI passes the secret). ``key_id`` is recorded verbatim in the .sig —
    the caller MUST NOT emit a key-id the trust stores do not bless.
    """
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    signer = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    raw_sig = signer.sign(payload)
    return {"key_id": key_id, "signature": raw_sig.hex()}


def sign_release_bytes(payload: bytes, *, key_id: str, private_key_hex: str) -> dict[str, str]:
    """Sign arbitrary bytes (the manifest file's exact bytes) — the .sig writer."""
    signer = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    raw_sig = signer.sign(payload)
    return {"key_id": key_id, "signature": raw_sig.hex()}


def read_signature_file(path: Path) -> dict[str, Any]:
    """Parse + shape-check a .sig file."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestSecurityError(f"signature file {path} unreadable/garbled: {exc}") from exc
    if not isinstance(body, dict) or {"key_id", "signature"} - set(body):
        raise ManifestSecurityError(f"signature file {path} must carry key_id and signature")
    return body


def verify_release_from_files(
    *,
    manifest_path: Path,
    signature_path: Path,
    artifacts_root: Path,
    artifact_names: list[str] | None = None,
    trust_store: dict[str, Ed25519PublicKey] | None = None,
) -> ReleaseManifest:
    """The --from-file path: verify a locally provided manifest + signature.

    Verifies the manifest's signature, verifies the *artifact_names* subset
    (the pre-extraction tarball check) or every artifact the manifest
    covers, and returns the parsed manifest. The trust store stays baked
    in (the same fail-closed store re-run installs verify against) — an
    offline admin never adds their own key.
    """
    payload = manifest_path.read_bytes()
    signature = read_signature_file(signature_path)
    manifest = verify_manifest(payload, signature, trust_store=trust_store)
    if artifact_names is None:
        verify_artifacts(artifacts_root, manifest)
    else:
        for name in artifact_names:
            _verify_single_artifact(artifacts_root, manifest, name)
    return manifest


def _verify_single_artifact(root: Path, manifest: ReleaseManifest, name: str) -> None:
    expected = manifest.checksum_for(name)
    artifact = _resolve_bundle_artifact(root, name)
    actual = _digest_of(artifact)
    if actual != expected:
        raise ManifestSecurityError(f"artifact '{name}' sha256 MISMATCH: expected {expected}, got {actual}")


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "PROVISIONING_NOTE",
    "SIGNATURE_NAME",
    "SIGNATURE_SUFFIX",
    "_KEY_ID_CURRENT",
    "_KEY_ID_NEXT",
    "ManifestSecurityError",
    "ReleaseManifest",
    "build_release_manifest",
    "read_signature_file",
    "sign_release_bytes",
    "sign_release_manifest",
    "verify_artifacts",
    "verify_manifest",
    "verify_release_from_files",
]


if __name__ == "__main__":  # pragma: no cover — documented one-liner seam for CI
    print(PROVISIONING_NOTE)  # noqa: T201 - the module-owned one-liner seam for CI
