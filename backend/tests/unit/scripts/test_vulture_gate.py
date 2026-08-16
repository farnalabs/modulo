"""Unit tests for the vulture dead-code gate (scripts/run_vulture.py).

The gate wraps ``vulture backend/src/modulo --min-confidence 60`` against the
repo-root ``.vulture_whitelist.py``, with ``--ignore-decorators`` for framework
registration decorators and ``--ignore-names`` for framework-interface names
(see the wrapper docstring for the full rationale). The whitelist suppresses
framework-contract and test-referenced symbols so the gate only fires on NEW
dead functions/methods/classes.

Finding types:
- ``unused function/method/class/property/attribute`` are BLOCKING.
- ``unused variable`` findings (Pydantic/dataclass/SQLAlchemy metaclass fields,
  Alembic migration vars, class constants — arbitrary names with no name
  pattern) are suppressed as framework noise; they are reported to stderr but
  never block. Dead locals are already caught by ruff F841.

Coverage here:
- The gate passes on the clean tree (exit 0, zero blocking findings).
- Newly added dead code of a form vulture reports at >= 60% confidence — an
  unused FUNCTION (60%) or an unused import (90%) / unreachable code (100%) —
  fails the gate, and removing it restores a clean pass. The dead-function
  probe is the gate's primary purpose (FAR-252 review finding: unused functions
  were passing at --min-confidence 80); the import/unreachable probe proves the
  original import-level detection still holds.
- The whitelist file exists and names the framework-contract symbols.
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

# A dead-code probe that vulture reports at 60% confidence — the gate's primary
# purpose is to block NEW dead functions, so the probe must be a function, not
# an import. Includes a dead import (90%) and unreachable code (100%) as
# secondary signals that the legacy detection levels still block.
DEAD_PROBE = (
    "def _far252_dead_probe() -> None:\n"
    "    pass\n"
    "\n"
    "import ftplib\n"
    "\n"
    "def _far252_unreachable():\n"
    "    return 1\n"
    "    x = 2\n"
)


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
    assert result.returncode == 0, f"vulture gate reported blocking findings on the clean tree:\n{result.stdout}"
    assert result.stdout.strip() == ""


def test_gate_blocks_new_dead_function() -> None:
    """The gate's primary purpose: a new dead FUNCTION blocks at 60%."""
    probe = SRC / "_far252_dead_probe.py"

    try:
        probe.write_text("def _far252_dead_probe() -> None:\n    pass\n", encoding="utf-8")

        result = _run_gate()
        assert result.returncode != 0, (
            f"vulture gate exited 0 despite a dead function under backend/src/modulo:\n{result.stdout}"
        )
        assert "_far252_dead_probe" in result.stdout, f"gate output did not name the dead function:\n{result.stdout}"
    finally:
        probe.unlink(missing_ok=True)

    clean = _run_gate()
    assert clean.returncode == 0, f"gate did not return to a clean pass after removing the probe:\n{clean.stdout}"
    assert clean.stdout.strip() == ""


def test_gate_blocks_unused_import_and_unreachable() -> None:
    """The legacy detection levels (import at 90%, unreachable at 100%) still block."""
    probe = SRC / "_far252_dead_probe.py"

    try:
        probe.write_text(
            "import ftplib\n\n\ndef _far252_unreachable():\n    return 1\n    x = 2\n",
            encoding="utf-8",
        )

        result = _run_gate()
        assert result.returncode != 0, (
            f"vulture gate exited 0 despite a dead import + unreachable code under backend/src/modulo:\n{result.stdout}"
        )
        assert "ftplib" in result.stdout, f"gate output did not name the unused import:\n{result.stdout}"
    finally:
        probe.unlink(missing_ok=True)


def test_whitelist_exists_and_names_framework_symbols() -> None:
    assert WHITELIST.is_file(), f"whitelist missing at {WHITELIST}"
    content = WHITELIST.read_text(encoding="utf-8")
    for symbol in ("CursorResult", "Dialect", "compiler", "element", "version_id", "get_plan_for_org"):
        assert f'"{symbol}"' in content, f"whitelist does not name framework-contract symbol {symbol!r}"
