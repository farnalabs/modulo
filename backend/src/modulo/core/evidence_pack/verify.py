"""Offline verification of an evidence pack (FAR-1697).

``verify_evidence_pack`` is given ONLY (a) the artefact — the pack dict as
published, including its file bytes — and (b) the pre-published public
verification key. With no Farnalabs service, no login and no database it:

1. verifies the Ed25519 signature over the canonical manifest (authorship);
2. re-derives the SHA-256 of every file and checks it against the signed list;
3. re-derives the audit chain from the embedded events and checks it against
   the signed chain result.

Any mismatch fails the whole pack. The verifier never trusts a value it can
recompute, and never raises on malformed input — a hostile artefact is a
``valid=False`` result, not a crash.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from modulo.core.evidence_pack.canonical import canonical_json_bytes, decode_ndjson
from modulo.core.evidence_pack.chain import recompute_chain
from modulo.core.evidence_pack.manifest import sha256_hex
from modulo.core.evidence_pack.signing import key_fingerprint
from modulo.registry.crypto import verify as _ed25519_verify

__all__ = ["OfflineVerificationResult", "verify_evidence_pack"]

_CHAIN_FIELDS = (
    "organisation_id",
    "event_count",
    "head_hash",
    "valid",
    "first_gap_index",
    "first_tampered_id",
    "truncated",
)


@dataclass(frozen=True)
class OfflineVerificationResult:
    """The outcome of an offline verification, broken out by check."""

    valid: bool
    manifest_ok: bool
    authorship_ok: bool
    files_ok: bool
    chain_ok: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "manifest_ok": self.manifest_ok,
            "authorship_ok": self.authorship_ok,
            "files_ok": self.files_ok,
            "chain_ok": self.chain_ok,
            "errors": list(self.errors),
        }


def verify_evidence_pack(pack: Mapping[str, Any], *, public_key_pem: str) -> OfflineVerificationResult:
    """Verify *pack* against the pre-published *public_key_pem*.

    Never raises: malformed or hostile input yields ``valid=False`` with the
    reason(s) in ``errors``.
    """
    errors: list[str] = []

    manifest = pack.get("manifest")
    signature = pack.get("signature")
    files = pack.get("files")
    if not isinstance(manifest, Mapping) or not isinstance(signature, Mapping) or not isinstance(files, Mapping):
        return OfflineVerificationResult(
            valid=False,
            manifest_ok=False,
            authorship_ok=False,
            files_ok=False,
            chain_ok=False,
            errors=("pack is missing a manifest, signature or files section",),
        )

    manifest_errors = _validate_manifest(manifest)
    errors.extend(manifest_errors)
    manifest_ok = not manifest_errors

    authorship_ok = _check_authorship(manifest, signature, public_key_pem, errors)
    files_ok = _check_files(manifest, files, errors)
    chain_ok = _check_chain(manifest, files, errors)
    _check_embedded_key(pack, public_key_pem, errors)

    valid = manifest_ok and authorship_ok and files_ok and chain_ok and not errors
    return OfflineVerificationResult(
        valid=valid,
        manifest_ok=manifest_ok,
        authorship_ok=authorship_ok,
        files_ok=files_ok,
        chain_ok=chain_ok,
        errors=tuple(errors),
    )


def _validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Structural validation of the signed manifest (fail closed on shape)."""
    errors: list[str] = []
    required = (
        "pack_version",
        "organisation_id",
        "exporter_identity",
        "created_at",
        "run_ids",
        "audit_events_file",
        "files",
        "audit_chain",
    )
    errors.extend(f"manifest is missing required field {key!r}" for key in required if key not in manifest)
    if "files" in manifest and not isinstance(manifest.get("files"), list):
        errors.append("manifest 'files' must be a list")
    if "audit_chain" in manifest and not isinstance(manifest.get("audit_chain"), Mapping):
        errors.append("manifest 'audit_chain' must be an object")
    chain = manifest.get("audit_chain")
    if isinstance(chain, Mapping):
        errors.extend(f"manifest audit_chain is missing {field!r}" for field in _CHAIN_FIELDS if field not in chain)
    entries = manifest.get("files")
    if isinstance(entries, list):
        errors.extend(_file_entry_errors(entries))
    return errors


def _file_entry_errors(entries: list[Any]) -> list[str]:
    """Shape-check each manifest file entry (path/sha256 strings, integer size)."""
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            errors.append("manifest file entries must be objects")
            continue
        if not all(isinstance(entry.get(field), str) for field in ("path", "sha256")):
            errors.append("manifest file entries need string 'path' and 'sha256'")
        if not isinstance(entry.get("size"), int):
            errors.append("manifest file entries need an integer 'size'")
    return errors


def _check_authorship(
    manifest: Mapping[str, Any],
    signature: Mapping[str, Any],
    public_key_pem: str,
    errors: list[str],
) -> bool:
    if signature.get("algorithm") != "ed25519":
        errors.append("signature block does not declare the ed25519 algorithm")
        return False
    signature_value = signature.get("value")
    if not isinstance(signature_value, str) or not signature_value:
        errors.append("signature block carries no signature value")
        return False
    try:
        payload = canonical_json_bytes(manifest)
    except (TypeError, ValueError) as exc:
        errors.append(f"manifest cannot be canonicalised for verification: {exc}")
        return False
    if not _ed25519_verify(public_key_pem, payload, signature_value):
        errors.append("manifest signature did not verify under the supplied public key")
        return False
    return True


def _check_files(manifest: Mapping[str, Any], files: Mapping[str, Any], errors: list[str]) -> bool:
    expected = _expected_files(manifest)
    if expected is None:
        return False
    ok = True
    for path, (digest, size) in expected.items():
        content = _file_bytes(files, path)
        if content is None:
            errors.append(f"bundle is missing file {path!r} listed in the manifest")
            ok = False
            continue
        if sha256_hex(content) != digest:
            errors.append(f"file {path!r} digest does not match the signed manifest")
            ok = False
        elif len(content) != size:
            errors.append(f"file {path!r} size does not match the signed manifest")
            ok = False
    for path in set(_string_keys(files)) - set(expected):
        errors.append(f"bundle contains unlisted file {path!r}")
        ok = False
    return ok


def _check_chain(manifest: Mapping[str, Any], files: Mapping[str, Any], errors: list[str]) -> bool:
    events_path = manifest.get("audit_events_file")
    if not isinstance(events_path, str):
        errors.append("manifest 'audit_events_file' is not a string")
        return False
    content = _file_bytes(files, events_path)
    if content is None:
        errors.append(f"bundle is missing the audit-events file {events_path!r}")
        return False
    try:
        events = decode_ndjson(content)
    except ValueError as exc:
        errors.append(f"audit-events file could not be decoded: {exc}")
        return False

    chain = manifest.get("audit_chain")
    if not isinstance(chain, Mapping):
        return False
    recomputed = recompute_chain(events, str(manifest.get("organisation_id")), truncated=bool(chain.get("truncated")))
    mismatches = [field for field in _CHAIN_FIELDS if recomputed.get(field) != chain.get(field)]
    if mismatches:
        errors.append("re-derived audit chain disagrees with the signed manifest on: " + ", ".join(mismatches))
        return False
    return True


def _check_embedded_key(pack: Mapping[str, Any], public_key_pem: str, errors: list[str]) -> None:
    """Cross-check the artefact's embedded key against the supplied key.

    Records an error when they differ, so a pack cannot be silently re-keyed.
    The embedded key is informational; verification always uses the
    caller-supplied, pre-published key.
    """
    embedded = pack.get("verification_key")
    if not isinstance(embedded, Mapping):
        return
    embedded_pem = embedded.get("public_key_pem")
    if not isinstance(embedded_pem, str) or not embedded_pem:
        return
    try:
        same = key_fingerprint(embedded_pem) == key_fingerprint(public_key_pem)
    except (TypeError, ValueError) as exc:
        errors.append(f"embedded verification key is not a usable PEM public key: {exc}")
        return
    if not same:
        errors.append("the artefact's embedded verification key is not the key supplied for verification")


def _expected_files(manifest: Mapping[str, Any]) -> dict[str, tuple[str, int]] | None:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        return None
    expected: dict[str, tuple[str, int]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        path = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int):
            return None
        expected[path] = (digest, size)
    return expected


def _file_bytes(files: Mapping[str, Any], path: str) -> bytes | None:
    blob = files.get(path)
    if not isinstance(blob, Mapping):
        return None
    content_b64 = blob.get("content_b64")
    if not isinstance(content_b64, str):
        return None
    try:
        return base64.b64decode(content_b64, validate=True)
    except ValueError:
        return None


def _string_keys(files: Mapping[str, Any]) -> list[str]:
    return [key for key in files if isinstance(key, str)]
