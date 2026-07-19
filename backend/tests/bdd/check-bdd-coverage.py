"""Check that every .feature file has at least one scenarios() call in a step file.

Exit code 0 = all covered. Exit code 1 = some feature files lack coverage.

Usage:
    uv run python check-bdd-coverage.py
"""

import re
import sys
from pathlib import Path

FEATURES_DIR = Path(__file__).parent / "features"
STEPS_DIR = Path(__file__).parent / "steps"


def collect_covered_features() -> set[str]:
    """Return set of feature file paths (relative to FEATURES_DIR) that are
    referenced by scenarios('...') in any step file."""
    pattern = re.compile(r'scenarios\s*\(\s*["\']([^"\']+)["\']\s*\)')
    covered: set[str] = set()
    for step_file in STEPS_DIR.rglob("test_*.py"):
        text = step_file.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            raw = match.group(1)
            # Resolve the path relative to the step file's directory.
            step_dir = step_file.parent
            resolved = (step_dir / raw).resolve()
            try:
                rel = resolved.relative_to(FEATURES_DIR.resolve())
                covered.add(str(rel.as_posix()))
            except ValueError:
                pass
    return covered


def main() -> int:
    covered = collect_covered_features()
    missing: list[str] = []
    for feat_file in sorted(FEATURES_DIR.rglob("*.feature")):
        rel = str(feat_file.relative_to(FEATURES_DIR).as_posix())
        if rel not in covered:
            missing.append(rel)

    if missing:
        for m in missing:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
