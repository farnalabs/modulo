"""Documentation indexer — builds a searchable index from docs/prd.md.

At module load or on first call, indexes the PRD by ``##`` and ``###`` headings.
Each index entry stores ``(heading_path, heading, first_paragraph)``.

Search is case-insensitive keyword matching against heading + first paragraph.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

_log = logging.getLogger(__name__)


@dataclass
class DocEntry:
    heading_path: str
    heading: str
    first_paragraph: str


@dataclass
class DocumentationIndex:
    entries: list[DocEntry] = field(default_factory=list)

    _TOKEN_BUDGET_CHARS: ClassVar[int] = 16_000

    def search(self, query: str, section: str | None = None) -> list[DocEntry]:
        query_words = [w.lower() for w in query.split() if w]
        if not query_words:
            return []

        results: list[DocEntry] = []
        for entry in self.entries:
            if section is not None and not entry.heading_path.lower().startswith(section.lower()):
                continue
            haystack = (entry.heading + " " + entry.first_paragraph).lower()
            if all(w in haystack for w in query_words):
                results.append(entry)

        return results

    def format_results(self, results: list[DocEntry]) -> str:
        chars_remaining = self._TOKEN_BUDGET_CHARS
        parts: list[str] = []

        for entry in results:
            md = f"### {entry.heading_path}\n\n{entry.heading}\n\n{entry.first_paragraph}"
            if len(md) > chars_remaining:
                md = md[:chars_remaining] + "\n\n*(truncated — results exceed token budget)*"
                parts.append(md)
                break
            parts.append(md)
            chars_remaining -= len(md)

        return "\n\n---\n\n".join(parts)

    @classmethod
    def build(cls, prd_path: str | Path | None = None) -> DocumentationIndex:
        path = Path(prd_path) if prd_path else Path(__file__).parents[3] / "docs" / "prd.md"
        if not path.exists():
            _log.warning("PRD not found at %s — returning empty index", path)
            return cls()

        text = path.read_text(encoding="utf-8")
        return cls._parse(text)

    @classmethod
    def _parse(cls, text: str) -> DocumentationIndex:
        entries: list[DocEntry] = []
        current_h2: str | None = None
        current_h3: str | None = None

        lines = text.splitlines()
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            h2_match = re.match(r"^## (.+)$", line)
            h3_match = re.match(r"^### (.+)$", line)

            if h2_match:
                current_h2 = h2_match.group(1).strip()
                current_h3 = None
                heading_path = current_h2
                heading = current_h2
                first_para = _extract_first_paragraph(lines, i + 1)
                entries.append(DocEntry(heading_path=heading_path, heading=heading, first_paragraph=first_para))
                i += 1
            elif h3_match:
                current_h3 = h3_match.group(1).strip()
                heading_path = f"{current_h2} > {current_h3}" if current_h2 else current_h3
                heading = current_h3
                first_para = _extract_first_paragraph(lines, i + 1)
                entries.append(DocEntry(heading_path=heading_path, heading=heading, first_paragraph=first_para))
                i += 1
            else:
                i += 1

        return cls(entries=entries)


def _extract_first_paragraph(lines: list[str], start: int) -> str:
    paragraphs: list[str] = []
    current: list[str] = []

    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if re.match(r"^#", stripped):
            break

        text = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
        text = re.sub(r"_\(.+?\)_", "", text)
        text = text.strip("*_")
        current.append(text)

    if current:
        paragraphs.append(" ".join(current))

    return paragraphs[0] if paragraphs else ""
