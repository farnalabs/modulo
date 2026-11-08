"""Validate canonical graph-schema usage in frontend graph writes.

This is the repository gate for FAR-2457. It intentionally fails closed when a
frontend call constructs a graph write without importing and using the canonical
schema helper. The check is lightweight and dependency-free so it can run in
pre-commit and CI on every platform.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


CANONICAL_IMPORT = "../types/pipeline-graph"
VALIDATION_MARKERS = ("validateCanonicalGraph", "CanonicalPipelineGraphUpdate")
GRAPH_WRITE_RE = re.compile(r"api\.(?:PUT|POST)\([^\n]*graph", re.IGNORECASE)


def validate_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if not GRAPH_WRITE_RE.search(text):
        return []
    errors: list[str] = []
    if CANONICAL_IMPORT not in text:
        errors.append(f"{path}: graph write must import {CANONICAL_IMPORT}")
    for marker in VALIDATION_MARKERS:
        if marker not in text:
            errors.append(f"{path}: graph write must use {marker}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Frontend files to validate")
    args = parser.parse_args()
    paths = args.paths or [Path("frontend/src/views/PipelineEditorView.vue")]
    errors = [error for path in paths for error in validate_file(path)]
    if errors:
        print("Graph schema validation failed:")
        print("\n".join(errors))
        return 1
    print(f"Graph schema validation passed for {len(paths)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
