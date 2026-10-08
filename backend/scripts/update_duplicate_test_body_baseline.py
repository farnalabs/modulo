#!/usr/bin/env python3
"""Regenerate the duplicate test-function-body baseline.

The whole-tree lens ``test_no_duplicate_test_bodies`` reports every pair of
``test_*`` functions in the same scope whose parameters, decorators, and bodies
are byte-for-byte identical once the name and docstring are set aside. The
PRE-EXISTING offenders are frozen in
``backend/tests/architecture/duplicate_test_body_baseline.txt`` so the guard
blocks NEW duplicates immediately while the backlog is untangled file by file.

The lens and its canonical renderer live in the architecture test module, so
this script reuses them directly rather than duplicating the AST logic.

Run from ``backend/``::

    uv run python scripts/update_duplicate_test_body_baseline.py

The baseline can only shrink: regenerating after a pair is untangled drops its
entry, and ``test_duplicate_test_body_baseline_has_no_stale_entries`` fails if a
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
    keys = lens._duplicate_test_body_baseline_keys()
    lens._DUPLICATE_TEST_BODY_BASELINE_PATH.write_text(
        lens._render_duplicate_test_body_baseline(keys), encoding="utf-8"
    )
    print(f"wrote {len(keys)} duplicate-test-body baseline entr(ies) to {lens._DUPLICATE_TEST_BODY_BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
