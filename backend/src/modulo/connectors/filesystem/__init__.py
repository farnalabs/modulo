"""FilesystemConnector — local filesystem access with base_path chroot enforcement."""

import asyncio
import itertools
from pathlib import Path
from typing import Any

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)


class PathTraversalError(ValueError):
    """Raised when a path escapes the configured base_path."""

    def __init__(self, path: str, base_path: Path) -> None:
        super().__init__(
            f"Path {path!r} resolves outside base_path {str(base_path)!r}. Path traversal is not permitted."
        )


class FilesystemConnector(ConnectorBase):
    """Read/write local files within a chrooted base directory.

    Supported query resources:
      "file"      — read a file; requires filter {"path": "relative/path"}
      "directory" — list a directory; requires filter {"path": "relative/dir"}

    Supported write resources:
      "file" — write text; data must contain {"path": "...", "content": "..."}
    """

    def __init__(self, base_path: str) -> None:
        if not base_path:
            raise ValueError("base_path must be a non-empty directory path")
        self._base_path = Path(base_path).resolve()

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.FILESYSTEM

    def _safe_path(self, relative: str) -> Path:
        """Resolve relative path within base_path; raise on traversal attempt."""
        candidate = (self._base_path / relative).resolve()
        if not candidate.is_relative_to(self._base_path):
            raise PathTraversalError(relative, self._base_path)
        return candidate

    async def health_check(self) -> HealthResult:
        if self._base_path.is_dir():
            return HealthResult(ok=True)
        return HealthResult(
            ok=False,
            detail=f"base_path {self._base_path} does not exist or is not a directory",
        )

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "file":
                rel_path = q.filters.get("path")
                if not rel_path:
                    raise ValueError("Filesystem file query requires 'path' filter")
                path = self._safe_path(rel_path)
                try:
                    content = await asyncio.to_thread(path.read_text, encoding="utf-8")
                except FileNotFoundError:
                    raise ValueError(f"File not found: {rel_path!r}") from None
                except PermissionError:
                    raise ValueError(f"Permission denied reading file: {rel_path!r}") from None
                return ConnectorResult(records=[{"path": str(path), "content": content}])
            case "directory":
                dir_path = self._safe_path(q.filters.get("path", "."))

                def _list_dir() -> list[dict[str, Any]]:
                    return [
                        {"name": p.name, "type": "dir" if p.is_dir() else "file", "path": str(p)}
                        for p in itertools.islice(sorted(dir_path.iterdir()), q.limit)
                    ]

                entries = await asyncio.to_thread(_list_dir)
                return ConnectorResult(records=entries, total=len(entries))
            case _:
                raise ValueError(f"Unsupported filesystem resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "file":
                rel_path = payload.data.get("path")
                if not rel_path:
                    raise ValueError("Filesystem file write requires 'path' in data")
                content = payload.data.get("content")
                if content is None:
                    raise ValueError("Filesystem file write requires 'content' in data")
                path = self._safe_path(rel_path)

                def _write() -> None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")

                await asyncio.to_thread(_write)
                return {"path": str(path), "bytes_written": len(content)}
            case _:
                raise ValueError(f"Unsupported filesystem write resource: {payload.resource!r}")
