"""Shared line-diff iteration for the prompt-versions and node-output diff endpoints.

Both endpoints render a side-by-side line diff from two whitespace-preserving line
listings using ``difflib.SequenceMatcher``. This module keeps that walk in one place
so each endpoint only maps the generic rows onto its own response model.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from difflib import SequenceMatcher
from typing import Literal

LineDiffKind = Literal["unchanged", "removed", "added"]

# A single diff row: (kind, content, source line number, other line number).
# Line numbers are 1-based; ``None`` marks a line with no counterpart in the
# other listing.
LineDiffRow = tuple[LineDiffKind, str, int | None, int | None]


def _yield_common_or_removed(
    op: str,
    i1: int,
    i2: int,
    lines_a: Sequence[str],
    line_a: int,
    line_b: int,
) -> Generator[LineDiffRow, None, tuple[int, int]]:
    """Yield the ``unchanged`` (equal) or ``removed`` (delete) rows for one opcode.

    Returns the updated ``(line_a, line_b)`` counters to the ``yield from`` caller.
    """
    kind: LineDiffKind = "unchanged" if op == "equal" else "removed"
    for idx in range(i1, i2):
        n_b = line_b if kind == "unchanged" else None
        yield kind, lines_a[idx].rstrip("\n"), line_a, n_b
        line_a += 1
        if kind == "unchanged":
            line_b += 1
    return line_a, line_b


def _yield_replaced_and_added(
    op: str,
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    lines_a: Sequence[str],
    lines_b: Sequence[str],
    line_a: int,
    line_b: int,
) -> Generator[LineDiffRow, None, tuple[int, int]]:
    """Yield the ``removed`` (replace) and ``added`` rows for one opcode.

    Returns the updated ``(line_a, line_b)`` counters to the ``yield from`` caller.
    """
    if op == "replace":
        for idx in range(i1, i2):
            yield "removed", lines_a[idx].rstrip("\n"), line_a, None
            line_a += 1
    for idx in range(j1, j2):
        yield "added", lines_b[idx].rstrip("\n"), None, line_b
        line_b += 1
    return line_a, line_b


def iter_line_diffs(lines_a: Sequence[str], lines_b: Sequence[str]) -> Iterator[LineDiffRow]:
    """Yield ``(kind, content, line_a, line_b)`` rows describing a line-wise diff.

    ``lines_a`` and ``lines_b`` may keep trailing newlines; each yielded ``content``
    is stripped of its trailing newline to mirror the existing endpoint behaviour.
    """
    line_a = 1
    line_b = 1
    matcher = SequenceMatcher(None, lines_a, lines_b)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("equal", "delete"):
            line_a, line_b = yield from _yield_common_or_removed(op, i1, i2, lines_a, line_a, line_b)
        else:
            line_a, line_b = yield from _yield_replaced_and_added(op, i1, i2, j1, j2, lines_a, lines_b, line_a, line_b)
