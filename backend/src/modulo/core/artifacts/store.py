"""On-disk artifact store for sandbox stdout/stderr side-car files (FAR-582).

Stores uncompressed raw text during a node run, then atomically compresses
to zstd on finalize.  Read-back decompresses transparently.

On-disk layout::

    <root>/<org_id>/<run_id>/<node_id>/<attempt_key>.<stream>.tmp   (raw, in-progress)
    <root>/<org_id>/<run_id>/<node_id>/<attempt_key>.<stream>.zst   (compressed, finalised)

Pointer dict (one per stream) -- the persistence contract::

    {
        "stream": "stdout" | "stderr",
        "rel_path": "<org_id>/<run_id>/<node_id>/<attempt_key>.stdout.zst",
        "size_bytes": <UNCOMPRESSED byte length>,
        "sha256": "<hex sha256 of the UNCOMPRESSED content>",
        "compression": "zstd",
    }
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import zstandard

from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

# File suffixes
_SUFFIX_RAW = ".tmp"
_SUFFIX_ZST = ".zst"

# ── pointer typed-dict ──────────────────────────────────────────────────


class ArtifactPointer(dict[str, str | int]):
    """Typed alias for an artifact pointer dict.

    The frontend consumption contract is exact:
    ``{"stream", "rel_path", "size_bytes", "sha256", "compression"}``.
    """

    def __init__(
        self,
        *,
        stream: str,
        rel_path: str,
        size_bytes: int,
        sha256: str,
        compression: str = "zstd",
    ) -> None:
        super().__init__(
            stream=stream,
            rel_path=rel_path,
            size_bytes=size_bytes,
            sha256=sha256,
            compression=compression,
        )


# ── protocol / ABC ──────────────────────────────────────────────────────


@runtime_checkable
class ArtifactStore(Protocol):
    """Protocol for artifact storage backends."""

    def append(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
        text: str,
    ) -> None:
        """Append *text* to the in-progress raw file for *stream*."""
        ...

    def finalize(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
    ) -> ArtifactPointer | None:
        """Compress and atomically replace the raw file; return pointer."""
        ...

    def read_bytes(self, pointer: dict[str, Any]) -> bytes:
        """Read and decompress the artifact; return raw bytes."""
        ...

    def delete_run(self, org_id: str, run_id: str) -> int:
        """Delete all artifact files for a run; return count removed."""
        ...

    def delete_node(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
    ) -> int:
        """Delete all artifact files for a node; return count removed."""
        ...


# ── local filesystem implementation ─────────────────────────────────────


class LocalArtifactStore:
    """Filesystem-backed artifact store.

    Parameters
    ----------
    root:
        Base directory for all artifact files.  Created on first write.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # ── internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _validate_path_component(name: str, value: str) -> None:
        """Reject path-traversal sequences in a path component.

        ``../``, ``..\\``, ``/``, and ``\\`` would escape the intended
        directory tree.  An absolute path (starting with ``/`` or a drive
        letter on Windows) is also rejected.
        """
        if ".." in value or "/" in value or "\\" in value:
            raise ValueError(f"Invalid {name}: path traversal characters not allowed")
        if len(value) >= 2 and value[1] == ":":
            raise ValueError(f"Invalid {name}: absolute path not allowed")

    def _node_dir(self, org_id: str, run_id: str, node_id: str) -> Path:
        self._validate_path_component("org_id", org_id)
        self._validate_path_component("run_id", run_id)
        self._validate_path_component("node_id", node_id)
        return self._root / org_id / run_id / node_id

    def _raw_path(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
    ) -> Path:
        self._validate_path_component("attempt_key", attempt_key)
        return self._node_dir(org_id, run_id, node_id) / f"{attempt_key}.{stream}{_SUFFIX_RAW}"

    def _zst_path(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
    ) -> Path:
        self._validate_path_component("attempt_key", attempt_key)
        return self._node_dir(org_id, run_id, node_id) / f"{attempt_key}.{stream}{_SUFFIX_ZST}"

    def _rel_path(self, path: Path) -> str:
        """Return the path relative to root as a POSIX string."""
        return path.relative_to(self._root).as_posix()

    # ── public API ──────────────────────────────────────────────────────

    def append(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
        text: str,
    ) -> None:
        if not text:
            return
        path = self._raw_path(org_id, run_id, node_id, attempt_key, stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def finalize(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
    ) -> ArtifactPointer | None:
        raw = self._raw_path(org_id, run_id, node_id, attempt_key, stream)
        if not raw.exists():
            return None
        data = raw.read_bytes()
        size_bytes = len(data)
        sha256_hex = hashlib.sha256(data).hexdigest()

        # Compress with zstandard
        cctx = zstandard.ZstdCompressor()
        compressed = cctx.compress(data)

        # Atomic write via temp file + Path.replace
        zst = self._zst_path(org_id, run_id, node_id, attempt_key, stream)
        zst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(zst.parent),
            prefix=f".{attempt_key}.{stream}",
            suffix=_SUFFIX_ZST,
        )
        tmp_path = Path(tmp_path_str)
        try:
            os.close(fd)
            tmp_path.write_bytes(compressed)
            tmp_path.replace(zst)
        except BaseException:
            # Cleanup temp file on failure
            with suppress(OSError):
                tmp_path.unlink()
            raise

        # Remove the raw file after successful compression
        with suppress(OSError):
            raw.unlink()

        return ArtifactPointer(
            stream=stream,
            rel_path=self._rel_path(zst),
            size_bytes=size_bytes,
            sha256=sha256_hex,
            compression="zstd",
        )

    def read_bytes(self, pointer: dict[str, Any]) -> bytes:
        path = self._root / str(pointer["rel_path"])
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {pointer['rel_path']}")
        raw = path.read_bytes()
        if pointer.get("compression") == "zstd":
            dctx = zstandard.ZstdDecompressor()
            return bytes(dctx.decompress(raw))
        return raw

    def delete_run(self, org_id: str, run_id: str) -> int:
        run_dir = self._root / org_id / run_id
        count = 0
        if not run_dir.exists():
            return count
        for f in run_dir.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except OSError:
                    _log.warning("artifact.delete_file_failed", extra={"path": str(f)})
        # Remove empty directories bottom-up
        try:
            for d in sorted(run_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if d.is_dir():
                    with suppress(OSError):
                        d.rmdir()
            run_dir.rmdir()
        except OSError:
            pass
        return count

    def delete_node(
        self,
        org_id: str,
        run_id: str,
        node_id: str,
    ) -> int:
        node_dir = self._node_dir(org_id, run_id, node_id)
        count = 0
        if not node_dir.exists():
            return count
        for f in node_dir.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except OSError:
                    _log.warning("artifact.delete_file_failed", extra={"path": str(f)})
        with suppress(OSError):
            node_dir.rmdir()
        return count


# ── module-level factory ─────────────────────────────────────────────────

_store_instance: LocalArtifactStore | None = None


def get_store() -> LocalArtifactStore:
    """Return the singleton LocalArtifactStore, creating on first call."""
    global _store_instance
    if _store_instance is None:
        settings = get_settings()
        root = _resolve_artifacts_dir(settings)
        _store_instance = LocalArtifactStore(root)
    return _store_instance


def _resolve_artifacts_dir(settings: Settings) -> Path:
    """Resolve the artifact storage directory from settings."""
    env_val = os.environ.get("MODULO_ARTIFACTS_DIR", "")
    if env_val:
        return Path(env_val)
    # Default: <backend>/.data/artifacts
    backend_dir = Path(__file__).resolve().parents[4]  # backend/src/modulo/core/artifacts
    return backend_dir / ".data" / "artifacts"


def reset_store() -> None:
    """Reset the singleton (for testing)."""
    global _store_instance
    _store_instance = None
