#!/usr/bin/env python3
"""Regenerate the no-op pytest-BDD ``@then`` step baseline.

The whole-tree lens ``test_no_noop_bdd_then_steps`` reports every ``@then``
step whose body is only a docstring/``pass``/``...``. Such a step is the
assertion half of a Gherkin scenario but observes nothing, so the scenario
reports green no matter how broken the product is. The PRE-EXISTING offenders
are frozen in ``backend/tests/architecture/noop_bdd_then_baseline.txt`` so the
guard blocks NEW no-op ``@then`` steps immediately while the backlog is
rewritten file by file.

The lens and its canonical renderer live in the architecture test module, so
this script reuses them directly rather than duplicating the AST logic.

Run from ``backend/``::

    uv run python scripts/update_noop_bdd_then_baseline.py

The baseline can only shrink: regenerating after a step is implemented drops
its entry, and ``test_noop_bdd_then_baseline_has_no_stale_entries`` fails if an
implemented or removed entry is left behind.
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
    keys = lens._noop_bdd_then_baseline_keys()
    lens._NOOP_BDD_THEN_BASELINE_PATH.write_text(lens._render_noop_bdd_then_baseline(keys), encoding="utf-8")
    print(f"wrote {len(keys)} no-op @then baseline entr(ies) to {lens._NOOP_BDD_THEN_BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
