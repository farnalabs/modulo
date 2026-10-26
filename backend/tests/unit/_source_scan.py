"""Shared helper for tests that statically scan a source tree.

Some tests deliberately create a transient module inside
``backend/src/modulo`` (the vulture dead-code gate writes
``_far252_dead_probe.py`` and removes it again). A scan running in another
xdist worker in the same checkout can list that path during its directory walk
and then read it after the probe test has unlinked it, raising
``FileNotFoundError`` for a file that is not part of the committed source.

A static scan must not fail because a transient file vanished mid-scan, so
every whole-tree scan reads through :func:`iter_source_text`, which yields
``(path, text)`` pairs and skips a path that disappears between the walk and
the read.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path


def iter_source_text(root: Path, suffixes: tuple[str, ...] = (".py",)) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, text)`` for every matching file under ``root``.

    ``__pycache__`` is skipped, and a file that vanishes between the directory
    walk and the read (a concurrent test fixture's transient probe) is skipped
    rather than raising ``FileNotFoundError``.
    """
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix not in suffixes:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
