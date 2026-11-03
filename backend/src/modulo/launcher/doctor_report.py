"""``modulo doctor --report``: delegate to the support bundle (FAR-2305).

Historically (FAR-676) this module built its own narrow redacted zip (versions,
OS info, doctor output, log tails). The support bundle superseded it: a
versioned format, a fixed member allowlist, five redaction layers and a
fail-closed gate. This module is kept as the stable import surface the doctor
command and its tests use; the implementation now delegates to
:mod:`modulo.support.bundle`.

``redact_text`` / ``redaction_map_from_data_dir`` also delegate to
:mod:`modulo.support.redaction`, so there is ONE redaction implementation —
never a second copy that can drift.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from modulo.support.bundle import DEFAULT_MAX_LOG_BYTES, BundleSources, build_bundle_async
from modulo.support.redaction import (
    REDACTED,
    RedactionResult,
    Redactor,
    seed_values_from_data_dir,
)

__all__ = [
    "DEFAULT_MAX_LOG_BYTES",
    "REDACTED",
    "build_report",
    "redact_text",
    "redaction_map_from_data_dir",
]


def redaction_map_from_data_dir(data_dir: Path) -> dict[str, str]:
    """Secret VALUE -> ``<redacted>`` map (the values are the patterns).

    Seeded from the generated credentials in ``secrets.json``, including their
    percent-encoded forms. Nothing here ever enters the archive: the map keys
    (the raw values) are only ever used as scrub patterns.
    """
    return seed_values_from_data_dir(data_dir)


def redact_text(text: str, secret_values: dict[str, str]) -> str:
    """Scrub *text* through the shared redaction layers (2-4).

    Kept for callers/tests that used the FAR-676 helper; the real work lives in
    :class:`modulo.support.redaction.Redactor`.
    """
    result: RedactionResult = Redactor(secret_values).redact(text)
    return result.text


def _run_coroutine(coroutine: Any) -> Any:
    """Run *coroutine* to completion, safe from inside an already-running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def build_report(
    data_dir: Path,
    out_path: Path,
    *,
    doctor_output: str,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
) -> Path:
    """Write the redacted support bundle to *out_path*; return its path.

    Delegates to :func:`modulo.support.bundle.build_bundle_async` with the
    launcher data dir (secrets seed + log tails + degraded record) and the
    captured doctor output. The container path (``python -m
    modulo.support.bundle``) is the same module without a data dir.
    """
    coroutine = build_bundle_async(
        out_path,
        sources=BundleSources(
            data_dir=data_dir,
            doctor_output=doctor_output,
            max_log_bytes=max_log_bytes,
        ),
    )
    _run_coroutine(coroutine)
    return out_path
