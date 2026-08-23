"""Documentation indexer — builds a searchable index from the product manifest.

At module load or on first call, indexes the product surface by reading
``frontend/src/manifest.yaml`` (the structured product manifest of routes/pages)
that ships in the build. Each index entry stores
``(heading_path, heading, first_paragraph, features)``, where ``features``
carries the route's ``product_map`` ``feat-*`` references so the index is
searchable by feature as well as by page text.

Search is case-insensitive keyword matching against heading + first paragraph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from modulo.core.manifest import _load_manifest_yaml, get_manifest_path

_log = logging.getLogger(__name__)


@dataclass
class DocEntry:
    heading_path: str
    heading: str
    first_paragraph: str
    features: list[str] = field(default_factory=list)


@dataclass
class DocumentationIndex:
    entries: list[DocEntry] = field(default_factory=list)

    _TOKEN_BUDGET_CHARS: ClassVar[int] = 16_000

    def search(self, query: str, section: str | None = None) -> list[DocEntry]:
        query_words = [w.lower() for w in query.split() if w]
        if not query_words:
            return []

        return [
            entry
            for entry in self.entries
            if (section is None or entry.heading_path.lower().startswith(section.lower()))
            and all(w in (entry.heading + " " + entry.first_paragraph).lower() for w in query_words)
        ]

    @staticmethod
    def format_results(results: list[DocEntry]) -> str:
        chars_remaining = DocumentationIndex._TOKEN_BUDGET_CHARS
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
    def build(cls, manifest_path: str | Path | None = None) -> DocumentationIndex:
        path = Path(manifest_path) if manifest_path else get_manifest_path()

        if not path.exists():
            _log.warning("Manifest not found at %s — returning empty index", path)
            return cls()

        try:
            data = _load_manifest_yaml(path)
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            _log.error("Failed to load manifest at %s: %s", path, exc)
            return cls()

        if not isinstance(data, dict):
            return cls()

        routes = data.get("routes")
        if not isinstance(routes, dict):
            return cls()

        entries: list[DocEntry] = []
        for route in sorted(routes.keys()):
            value = routes[route]
            if not isinstance(value, dict):
                continue
            entries.append(
                DocEntry(
                    heading_path=route,
                    heading=_heading(route, value),
                    first_paragraph=_summarise(value),
                    features=_features(value),
                )
            )

        return cls(entries=entries)


def _features(entry: dict[Any, Any]) -> list[str]:
    product_map = entry.get("product_map")
    if not product_map:
        return []
    if isinstance(product_map, (list, tuple)):
        return [str(item).strip() for item in product_map if str(item).strip()]
    return [str(product_map).strip()]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _heading(route: str, entry: dict[Any, Any]) -> str:
    breadcrumb = _as_str(entry.get("breadcrumb"))
    if breadcrumb:
        return breadcrumb
    name = _as_str(entry.get("name"))
    if name:
        return name
    return route


def _summarise(entry: dict[Any, Any]) -> str:
    name = _as_str(entry.get("name"))
    parts: list[str] = [name] if name else []

    if entry.get("type"):
        parts.append(f"type={_as_str(entry['type'])}")
    if entry.get("sidebar_group"):
        parts.append(f"sidebar_group={_as_str(entry['sidebar_group'])}")
    if entry.get("required_permissions"):
        perms = entry["required_permissions"]
        perms_str = ", ".join(_as_str(p) for p in perms) if isinstance(perms, (list, tuple)) else _as_str(perms)
        parts.append(f"required_permissions={perms_str}")
    if entry.get("visibility"):
        parts.append(f"visibility={_as_str(entry['visibility'])}")
    features = _features(entry)
    if features:
        parts.append("product_map=" + ", ".join(features))

    summary = " · ".join(parts)
    if entry.get("deprecated"):
        summary += " (deprecated)"
    if _as_str(entry.get("visibility")) in ("private_preview", "in_dev"):
        summary += " (dev-mode preview)"
    return summary
