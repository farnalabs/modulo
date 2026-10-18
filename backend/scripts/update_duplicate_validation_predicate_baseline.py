#!/usr/bin/env python3
"""Regenerate the duplicate-validation-predicate baseline.

The whole-tree lens ``test_no_duplicate_validation_predicates`` reports every
validation-named function that carries a non-empty validation-error ``detail``
string which a validation-named function in a DIFFERENT module carries too: the
same predicate written independently in more than one caller, so a widening in
one copy silently leaves the other stale. The PRE-EXISTING offenders are frozen
in ``backend/tests/architecture/duplicate_validation_predicate_baseline.txt`` so
the gate blocks NEW duplicates immediately while the backlog is untangled.

The lens and its canonical renderer live in the architecture test module, so
this script reuses them directly rather than duplicating the AST logic.

Run from ``backend/``::

    uv run python scripts/update_duplicate_validation_predicate_baseline.py

The baseline can only shrink: regenerating after a group is untangled drops its
entry, and ``test_duplicate_validation_predicate_baseline_has_no_stale_entries``
fails if a fixed entry is left behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_ARCH_DIR = _BACKEND_DIR / "tests" / "architecture"
if str(_ARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_ARCH_DIR))

import test_duplicate_validation_predicates as lens  # noqa: E402


def main() -> int:
    keys = lens._current_baseline_keys()
    lens.BASELINE_PATH.write_text(lens._render_baseline(keys), encoding="utf-8")
    print(f"wrote {len(keys)} duplicate-validation baseline entries to {lens.BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
