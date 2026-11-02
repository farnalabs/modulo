"""``modulo doctor --report``: the native-install entry point for the v1 bundle.

Kept as a thin, backwards-compatible seam over
:mod:`modulo.support.bundle` (FAR-2305). The real builder — the fixed v1
member allowlist, the versioned ``report.json`` / ``MANIFEST.txt`` inventory,
allowlist-by-construction redaction and the fail-closed post-assembly gate —
lives in :mod:`modulo.support`.

The redaction seed map is still built from the ACTUAL generated credential
values in the 0600 ``secrets.json`` (bundled postgres/redis passwords and the
state HMAC key). Those values are only ever used as scrub patterns; they never
enter the archive, and ``secrets.json`` itself is never archived.
"""

from __future__ import annotations

from pathlib import Path

from modulo.support.bundle import build_bundle
from modulo.support.collect import (
    DEFAULT_MAX_LOG_BYTES,
    collect_sources,
    redaction_map_from_data_dir,
)
from modulo.support.redaction import REDACTED, RedactionResult
from modulo.support.redaction import redact_text as _redact_text

__all__ = [
    "DEFAULT_MAX_LOG_BYTES",
    "REDACTED",
    "build_report",
    "redact_text",
    "redaction_map_from_data_dir",
]


def redact_text(text: str, secret_values: dict[str, str]) -> str:
    """Scrub *text* through the v1 redaction layers and return only the text.

    Retained for callers of the pre-FAR-2305 API; the per-layer substitution
    counts are available from :func:`modulo.support.redaction.redact_text`.
    """
    result: RedactionResult = _redact_text(text, secret_values)
    return result.text


def build_report(
    data_dir: Path,
    out_path: Path,
    *,
    doctor_output: str,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
) -> Path:
    """Write a v1 support bundle for a native install; return its path.

    Members (all redacted before writing): ``MANIFEST.txt``, ``report.json``,
    ``version.txt``, ``doctor.txt``, ``health.json``, ``settings.txt``,
    ``deployment.txt``, ``db.txt``, ``errors.json``, ``runs.json``,
    ``licence.txt``, ``degraded.json`` (when degraded) and one
    ``logs/<component>.log.tail`` per data-dir log source.
    """
    sources = collect_sources(
        install_shape="native",
        data_dir=data_dir,
        doctor_output=doctor_output,
        max_log_bytes=max_log_bytes,
    )
    return build_bundle(out_path, sources=sources, generated_by="modulo doctor --report").path
