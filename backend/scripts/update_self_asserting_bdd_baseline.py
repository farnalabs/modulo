#!/usr/bin/env python3
"""Regenerate the self-asserting BDD step baseline (FAR-1578).

The whole-tree lens ``test_no_self_asserting_bdd_step_responses`` reports every
pytest-BDD step that fabricates the HTTP response/status it later asserts on
instead of driving the real route. The PRE-EXISTING offenders are frozen in
``backend/tests/architecture/self_asserting_bdd_baseline.txt`` so the guard
blocks NEW self-asserting steps immediately while the FAR-1578 sweep rewrites
the backlog file by file.

The lens and its canonical renderer live in the architecture test module, so
this script reuses them directly rather than duplicating the AST logic.

Run from ``backend/``::

    uv run python scripts/update_self_asserting_bdd_baseline.py

The baseline can only shrink: regenerating after a step is rewritten drops its
entry, and ``test_self_asserting_bdd_baseline_has_no_stale_entries`` fails if a
fixed entry is left behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_ARCH_DIR = _BACKEND_DIR / "tests" / "architecture"
if str(_ARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_ARCH_DIR))

import test_test_suite_quality as lens  # noqa: E402


def main() -> int:
    keys = lens._self_asserting_bdd_baseline_keys()
    lens._BDD_BASELINE_PATH.write_text(lens._render_self_asserting_bdd_baseline(keys), encoding="utf-8")
    print(f"wrote {len(keys)} self-asserting BDD step baseline entr(ies) to {lens._BDD_BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
