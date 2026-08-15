"""Unit tests for the vulture dead-code gate (scripts/run_vulture.py).

The gate wraps ``vulture backend/src/modulo --min-confidence 80`` against the
repo-root ``.vulture_whitelist.py``. The whitelist suppresses the 13 known
framework-contract findings (TYPE_CHECKING imports, FastAPI path params,
SQLAlchemy @compiles / LangChain callback params) so the gate only fires on
NEW dead code.

Coverage here:
- The gate passes on the clean tree (exit 0, zero findings).
- Newly added dead code of a form vulture reports at >= 80% confidence
  (an unused import at 90%, unreachable code at 100%) fails the gate, and
  removing it restores a clean pass. This is the prove-the-fix check: it
  exercises the real wrapper subprocess against the real tree.
- The whitelist file exists and names the framework-contract symbols that
  cover the 13 known findings.

Known limitation (documented, not asserted): vulture reports unused functions,
methods, classes and variables at 60% confidence, which is BELOW the gate's
--min-confidence 80 threshold. New dead code of those forms is therefore not
caught by the gate as configured (see FAR-252 review finding). The regression
test uses dead-code forms the gate is configured to catch so the test is
stable and meaningful.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Locate the repo root: walk up from this test file until scripts/run_vulture.py
# is found (mirrors the scripts test convention in test_backup.py / test_restore.py).
REPO_ROOT = None
for parent in Path(__file__).resolve().parents:
    if (parent / "scripts" / "run_vulture.py").exists():
        REPO_ROOT = parent
        break
if REPO_ROOT is None:
    raise RuntimeError("Could not find repo root (scripts/run_vulture.py)")

WRAPPER = REPO_ROOT / "scripts" / "run_vulture.py"
WHITELIST = REPO_ROOT / ".vulture_whitelist.py"
SRC = REPO_ROOT / "backend" / "src" / "modulo"

# Symbols named in the whitelist. There are 8 distinct names covering the
# 13 known findings (some names occur at multiple sites).
WHITELIST_SYMBOLS = (
    "CursorResult",
    "Dialect",
    "compiler",
    "element",
    "input_str",
    "inputs",
    "q_or_none",
    "version_id",
)

# A dead-code probe that vulture reports at 90% confidence (>= the gate's 80
# threshold): an import of a module used nowhere else in backend/src/modulo.
# Also includes unreachable code (100% confidence) as a second independent
# signal. `ftplib` is verified unused in the tree.
DEAD_PROBE = "import ftplib\n\n\ndef _far252_unreachable():\n    return 1\n    x = 2\n"


def _run_gate() -> subprocess.CompletedProcess[str]:
    """Run the gate wrapper exactly as pre-commit / CI invoke it."""
    return subprocess.run(  # noqa: S603 - test fixture
        [sys.executable, str(WRAPPER)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_gate_passes_on_clean_tree() -> None:
    result = _run_gate()
    assert result.returncode == 0, f"vulture gate reported findings on the clean tree:\n{result.stdout}"
    assert result.stdout.strip() == ""


def test_gate_blocks_new_dead_code() -> None:
    probe = SRC / "_far252_dead_probe.py"

    try:
        probe.write_text(DEAD_PROBE, encoding="utf-8")

        result = _run_gate()
        assert result.returncode != 0, (
            f"vulture gate exited 0 despite a dead-code probe under backend/src/modulo:\n{result.stdout}"
        )
        assert "ftplib" in result.stdout, f"gate output did not name the unused import:\n{result.stdout}"
    finally:
        probe.unlink(missing_ok=True)

    clean = _run_gate()
    assert clean.returncode == 0, f"gate did not return to a clean pass after removing the probe:\n{clean.stdout}"
    assert clean.stdout.strip() == ""


def test_whitelist_exists_and_names_framework_symbols() -> None:
    assert WHITELIST.is_file(), f"whitelist missing at {WHITELIST}"
    content = WHITELIST.read_text(encoding="utf-8")
    for symbol in WHITELIST_SYMBOLS:
        assert f'"{symbol}"' in content, f"whitelist does not name framework-contract symbol {symbol!r}"
