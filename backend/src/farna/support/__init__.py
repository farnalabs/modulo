"""Support bundle (FAR-2305): one artefact a self-hoster can safely send us.

The bundle is a versioned zip with a FIXED member allowlist and a five-layer
redaction pipeline whose final layer fails closed: if any member still looks
like it carries a secret after layers 1-4, the build aborts, names the member
and writes NO zip.

Public surface (lazy — the package ``__init__`` deliberately does NOT import
the module eagerly, so ``python -m modulo.support.bundle`` does not see the
module loaded twice):

* :func:`modulo.support.bundle.build_bundle` — build a bundle (sync).
* :func:`modulo.support.bundle.build_bundle_async` — async build (DB reads).
* :func:`modulo.support.bundle.read_bundle` — read an existing bundle,
  tolerating unknown members (forward compatibility).
* :data:`modulo.support.bundle.BUNDLE_FORMAT_VERSION` — the integer format
  version, stamped in ``report.json`` and the printed reference line.
"""

from __future__ import annotations

from typing import Any

_LAZY_EXPORTS = frozenset(
    {
        "BUNDLE_FORMAT_VERSION",
        "BundleResult",
        "BundleTooLargeError",
        "RedactionGateError",
        "build_bundle",
        "build_bundle_async",
        "read_bundle",
    }
)

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        from farna.support import bundle

        return getattr(bundle, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
