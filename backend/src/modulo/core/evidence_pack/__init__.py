"""Offline-verifiable evidence packs (FAR-1697).

An evidence pack is a self-contained artefact a third party can verify with
nothing but the artefact and a pre-published public key: an Ed25519 signature
over a canonical manifest (organisation, exporter identity, run ids, per-file
SHA-256 + size, and the re-derived audit-chain result), the file bytes, and the
publishing key. The offline verifier re-derives every derived value rather than
trusting it — a tampered file, a mutated event or a swapped key all fail.

Export side: :func:`build_evidence_pack`.
Verify side: :func:`verify_evidence_pack`.
"""

from __future__ import annotations

from modulo.core.evidence_pack.build import build_evidence_pack
from modulo.core.evidence_pack.canonical import canonical_json_bytes, decode_ndjson, encode_ndjson
from modulo.core.evidence_pack.chain import recompute_chain
from modulo.core.evidence_pack.manifest import (
    AUDIT_EVENTS_FILE,
    EVIDENCE_PACK_VERSION,
    EvidenceFile,
    build_manifest,
    sha256_hex,
)
from modulo.core.evidence_pack.signing import (
    EvidenceSigningError,
    configured_public_key_pem,
    key_fingerprint,
    public_key_pem_from_private,
    resolve_signing_key_pem,
    sign_manifest,
)
from modulo.core.evidence_pack.verify import OfflineVerificationResult, verify_evidence_pack

__all__ = [
    "AUDIT_EVENTS_FILE",
    "EVIDENCE_PACK_VERSION",
    "EvidenceFile",
    "EvidenceSigningError",
    "OfflineVerificationResult",
    "build_evidence_pack",
    "build_manifest",
    "canonical_json_bytes",
    "configured_public_key_pem",
    "decode_ndjson",
    "encode_ndjson",
    "key_fingerprint",
    "public_key_pem_from_private",
    "recompute_chain",
    "resolve_signing_key_pem",
    "sha256_hex",
    "sign_manifest",
    "verify_evidence_pack",
]
