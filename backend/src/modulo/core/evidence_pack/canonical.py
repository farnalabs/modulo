"""Canonical serialisation for the offline-verifiable evidence pack (FAR-1697).

Two byte contracts live here, and both must be produced identically by the
exporter and the offline verifier or the digests will never agree:

* :func:`canonical_json_bytes` — the signed manifest payload. Compact JSON
  (``separators=(',', ':')``), keys sorted, ``ensure_ascii=False`` (non-ASCII
  values stay literal UTF-8), encoded UTF-8. This mirrors the canonical form
  already used by ``modulo.core.library_sync.manifest``.
* :func:`encode_ndjson` / :func:`decode_ndjson` — the audit-events file: one
  canonical-JSON event per line. The chain re-derivation replays exactly the
  events decoded from these bytes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["canonical_json_bytes", "decode_ndjson", "encode_ndjson"]


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialise *payload* to the canonical bytes covered by the signature."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encode_ndjson(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Encode *records* as newline-delimited canonical JSON (UTF-8).

    A trailing newline terminates the final record. ``default=str`` keeps the
    encoder total for the rare non-native value in an audit payload, matching
    the tolerance of the chain hash function.
    """
    lines = [
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str) for record in records
    ]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def decode_ndjson(data: bytes) -> list[dict[str, Any]]:
    """Decode newline-delimited JSON into a list of objects.

    Blank lines are ignored. Raises :class:`ValueError` if a line is not a JSON
    object, so a malformed events file fails the chain re-derivation loudly
    rather than silently shortening the chain.
    """
    text = data.decode("utf-8")
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"audit-events file contains a non-JSON line: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("audit-events file contains a non-object record")
        records.append(parsed)
    return records
