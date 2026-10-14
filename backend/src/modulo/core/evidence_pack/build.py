"""Assemble — and sign — an offline-verifiable evidence pack (FAR-1697).

``build_evidence_pack`` is the export-side entry point: it writes the audit
events into the bundle as ``audit-events.ndjson``, re-derives the chain result,
builds the canonical manifest, signs it with the configured Ed25519 key, and
returns the self-contained pack (manifest + signature + published key + file
bytes). The offline verifier consumes exactly this object.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from modulo.core.evidence_pack.canonical import encode_ndjson
from modulo.core.evidence_pack.chain import recompute_chain
from modulo.core.evidence_pack.manifest import (
    AUDIT_EVENTS_FILE,
    EVIDENCE_PACK_VERSION,
    EvidenceFile,
    build_manifest,
    sha256_hex,
)
from modulo.core.evidence_pack.signing import (
    public_key_pem_from_private,
    resolve_signing_key_pem,
    sign_manifest,
)
from modulo.settings import get_settings

__all__ = ["build_evidence_pack"]


def _exporter_identity(override: str | None) -> str:
    """Resolve the stamped exporter identity (never a credential)."""
    if override is not None:
        return override
    configured = get_settings().modulo_evidence_exporter_identity
    return configured or "modulo"


def build_evidence_pack(
    *,
    organisation_id: str,
    events: Sequence[Mapping[str, Any]],
    extra_files: Mapping[str, bytes] | None = None,
    run_ids: Sequence[str] = (),
    exporter_identity: str | None = None,
    created_at: str | None = None,
    private_key_pem: str | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Build and sign an evidence pack over *events*.

    ``events`` are audit-event dicts (the shape ``audit_logger`` exports: id,
    event_type, actor_user_id, resource_type, resource_id, payload_json,
    request_id, previous_hash, created_at). ``extra_files`` are additional
    bundle files (path -> bytes); their digests join the manifest's file list.
    The signing key resolves from ``private_key_pem`` or
    ``MODULO_EVIDENCE_SIGNING_KEY`` and signing fails closed when neither is set.
    """
    resolved_key = resolve_signing_key_pem(private_key_pem)
    resolved_created_at = created_at or datetime.now(UTC).isoformat()

    payloads: dict[str, bytes] = {AUDIT_EVENTS_FILE: encode_ndjson(events)}
    for path, content in (extra_files or {}).items():
        if path in payloads:
            raise ValueError(f"duplicate bundle file path: {path!r}")
        if not isinstance(content, bytes):
            raise TypeError(f"bundle file {path!r} content must be bytes, got {type(content).__name__}")
        payloads[path] = content

    file_entries = [
        EvidenceFile(path=path, sha256=sha256_hex(content), size=len(content)) for path, content in payloads.items()
    ]
    audit_chain = recompute_chain(events, organisation_id, truncated=truncated)
    manifest = build_manifest(
        organisation_id=organisation_id,
        exporter_identity=_exporter_identity(exporter_identity),
        created_at=resolved_created_at,
        run_ids=list(run_ids),
        files=file_entries,
        audit_chain=audit_chain,
    )
    signature = sign_manifest(manifest, resolved_key)
    return {
        "pack_version": EVIDENCE_PACK_VERSION,
        "manifest": manifest,
        "signature": signature,
        "verification_key": {
            "algorithm": "ed25519",
            "public_key_pem": public_key_pem_from_private(resolved_key),
        },
        "files": {
            path: {"content_b64": base64.b64encode(content).decode("ascii")} for path, content in payloads.items()
        },
    }
