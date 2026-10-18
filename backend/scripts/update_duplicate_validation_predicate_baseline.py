#!/usr/bin/env python3
"""Regenerate the duplicated-logic baseline.

Three whole-tree lenses in ``test_duplicate_validation_predicates.py`` report
pre-existing duplication, each frozen in
``backend/tests/architecture/duplicate_validation_predicate_baseline.txt``:

* a validation-named function carrying a validation-error ``detail`` string
  which a validation-named function in a DIFFERENT module carries too;
* a private module-level string constant re-declared with the same name and
  value in another module;
* a statement block that is an exact copy of a function's complete body in
  another module.

Freezing the PRE-EXISTING offenders lets each gate block NEW duplication
immediately while the backlog is untangled.

The lenses and their canonical renderer live in the architecture test module,
so this script reuses them directly rather than duplicating the AST logic.

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
    print(f"wrote {len(keys)} duplicated-logic baseline entries to {lens.BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
