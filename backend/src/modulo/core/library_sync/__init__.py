"""Client-side sync for the hosted community library (FAR-363).

The product polls ``MODULO_LIBRARY_ENDPOINT`` for a signed Ed25519 manifest,
verifies it against ``MODULO_LIBRARY_ROOT_PUBLIC_KEY``, applies revocations to
the fetched catalog, and caches the last-good state in the instance-global
``library_sync_state`` singleton table. The whole surface is fail-open: the
community library is optional and never blocks the product.

Public API:
    sync_library          - run one full sync cycle (fetch -> verify -> cache)
    get_cached_manifest   - read the last-good cached manifest
    is_revoked            - query whether a primitive id is revoked
    LibraryClient         - the outbound HTTP client (mock-friendly)
"""

from __future__ import annotations

from modulo.core.library_sync.client import LibraryClient
from modulo.core.library_sync.sync import get_cached_manifest, is_revoked, sync_library

__all__ = [
    "LibraryClient",
    "get_cached_manifest",
    "is_revoked",
    "sync_library",
]
