"""Artifact store for full sandbox stdout/stderr side-car files (FAR-582).

Provides zstd-compressed on-disk storage for sandbox node stdout and stderr
streams, with pointer dicts that travel via the ``_artifact_pointers`` key
in node output JSON.
"""

from modulo.core.artifacts.store import ArtifactStore, LocalArtifactStore, get_store
from modulo.core.artifacts.writer import ArtifactWriter

__all__ = [
    "ArtifactStore",
    "ArtifactWriter",
    "LocalArtifactStore",
    "get_store",
]
