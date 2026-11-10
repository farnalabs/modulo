"""The signed evidence manifest (FAR-1697).

The manifest is the CANONICAL, signed description of an export pack: which
organisation exported it, under what identity, when, which run ids it covers,
the per-file SHA-256 + size of every file in the bundle, and the result of
re-deriving the embedded audit chain. Everything the offline verifier is asked
to re-derive is named here; the signature covers the manifest whole.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AUDIT_EVENTS_FILE",
    "EVIDENCE_PACK_VERSION",
    "EvidenceFile",
    "build_manifest",
    "sha256_hex",
]

#: Schema version of both the pack envelope and the signed manifest.
EVIDENCE_PACK_VERSION = "1"

#: The bundle file that carries the exported audit events (one canonical-JSON
#: event per line). Its digest is in the manifest's file list like any other
#: file, and the offline verifier reads the chain back out of it.
AUDIT_EVENTS_FILE = "audit-events.ndjson"


@dataclass(frozen=True)
class EvidenceFile:
    """One file covered by the manifest (digest + size, never the bytes)."""

    path: str
    sha256: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def build_manifest(
    *,
    organisation_id: str,
    exporter_identity: str,
    created_at: str,
    run_ids: list[str],
    files: list[EvidenceFile],
    audit_chain: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the manifest payload that gets signed.

    ``files`` is sorted by path and ``run_ids`` de-duplicated + sorted so the
    same logical pack always canonicalises to the same bytes.
    """
    return {
        "pack_version": EVIDENCE_PACK_VERSION,
        "organisation_id": organisation_id,
        "exporter_identity": exporter_identity,
        "created_at": created_at,
        "run_ids": sorted(set(run_ids)),
        "audit_events_file": AUDIT_EVENTS_FILE,
        "files": [entry.to_json() for entry in sorted(files, key=lambda entry: entry.path)],
        "audit_chain": audit_chain,
    }
