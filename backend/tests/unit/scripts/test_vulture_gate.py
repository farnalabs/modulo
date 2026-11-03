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
- The probe is staged in a temp directory and handed to the gate as an extra
  scan target, so it NEVER appears inside ``backend/src/modulo``. Coverage's
  un-executed-source scan walks ``[tool.coverage.run] source`` on save, so a
  probe written into the measured tree can be touched into a shard's coverage
  data and then fails ``coverage combine`` on a fresh checkout with
  "No source for code" (FAR-2334). A regression test pins that invariant.
- The whitelist file exists and names the framework-contract symbols.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

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

PROBE_FILENAME = "_far252_dead_probe.py"

# Dead code probes that vulture reports at the gate's BLOCKING confidence
# levels: an unused function (60%) and an unused import (90%) plus unreachable
# code (100%). They are written to a temp directory by the tests, never into
# the measured source tree.
DEAD_FUNCTION_PROBE = "def _far252_dead_probe() -> None:\n    pass\n"
DEAD_IMPORT_AND_UNREACHABLE_PROBE = "import ftplib\n\n\ndef _far252_unreachable():\n    return 1\n    x = 2\n"


def _probe_path(directory: Path) -> Path:
    """Return the probe path inside ``directory``.

    This is the single source of truth for where the gate tests stage a probe,
    and it is deliberately a plain ``directory / PROBE_FILENAME``: callers pass
    pytest's ``tmp_path``, so the probe lives outside ``backend/src/modulo``
    (the measured coverage source tree) and outside the repo entirely. A probe
    created inside that tree can be recorded in coverage data and later break
    ``coverage combine`` (FAR-2334).
    """
    return directory / PROBE_FILENAME


def _stage_probe(directory: Path, content: str) -> Path:
    """Write ``content`` to the probe path inside ``directory`` and return it."""
    probe = _probe_path(directory)
    probe.write_text(content, encoding="utf-8")
    return probe


def _run_gate(extra_targets: list[Path] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the gate wrapper exactly as pre-commit / CI invoke it.

    ``extra_targets`` are additional scan paths — the staged probe — which lets
    a test put dead code in front of the real gate without writing it into the
    measured source tree.
    """
    args = [sys.executable, str(WRAPPER)]
    if extra_targets:
        args.extend(str(target) for target in extra_targets)
    return subprocess.run(  # noqa: S603 - test fixture
        args,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# A full-tree vulture scan is CPU-bound and measured at 23-85 s per scan on a
# loaded 16-core host; two of these tests run two scans. The suite-wide
# --timeout (120-180 s) is a budget for ordinary unit tests and is genuinely too
# tight for this work, so each scan test carries its own explicit budget. This
# is a margin on real, measured work — not a way to tolerate a hang: a scan that
# never returns still fails, just at its own bound.
_SCAN_TEST_TIMEOUT_SECONDS = 600


@pytest.mark.timeout(_SCAN_TEST_TIMEOUT_SECONDS)
def test_gate_passes_on_clean_tree() -> None:
    result = _run_gate()
    assert result.returncode == 0, f"vulture gate reported blocking findings on the clean tree:\n{result.stdout}"
    assert not result.stdout.strip()


@pytest.mark.timeout(_SCAN_TEST_TIMEOUT_SECONDS)
def test_gate_blocks_new_dead_function(tmp_path: Path) -> None:
    """The gate's primary purpose: a new dead FUNCTION blocks at 60%."""
    probe = _stage_probe(tmp_path, DEAD_FUNCTION_PROBE)
    source_before = set(SRC.rglob("*.py"))
    try:
        result = _run_gate([probe])
    finally:
        probe.unlink(missing_ok=True)

    source_after = set(SRC.rglob("*.py"))
    assert source_after == source_before, (
        "the gate test created a file inside the measured source tree: "
        f"{sorted(str(path) for path in source_after - source_before)}"
    )
    assert result.returncode != 0, (
        f"vulture gate exited 0 despite a dead function in the scanned probe:\n{result.stdout}"
    )
    assert "_far252_dead_probe" in result.stdout, f"gate output did not name the dead function:\n{result.stdout}"

    clean = _run_gate()
    assert clean.returncode == 0, f"gate did not return to a clean pass after removing the probe:\n{clean.stdout}"
    assert not clean.stdout.strip()


@pytest.mark.timeout(_SCAN_TEST_TIMEOUT_SECONDS)
def test_gate_blocks_unused_import_and_unreachable(tmp_path: Path) -> None:
    """The legacy detection levels (import at 90%, unreachable at 100%) still block."""
    probe = _stage_probe(tmp_path, DEAD_IMPORT_AND_UNREACHABLE_PROBE)
    try:
        result = _run_gate([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode != 0, (
        f"vulture gate exited 0 despite a dead import + unreachable code in the scanned probe:\n{result.stdout}"
    )
    assert "ftplib" in result.stdout, f"gate output did not name the unused import:\n{result.stdout}"


@pytest.mark.parametrize(
    "content",
    [DEAD_FUNCTION_PROBE, DEAD_IMPORT_AND_UNREACHABLE_PROBE],
    ids=["dead-function", "dead-import-unreachable"],
)
def test_probe_is_staged_outside_the_measured_source_tree(tmp_path: Path, content: str) -> None:
    """Regression guard (FAR-2334): probes must never live under ``backend/src/modulo``.

    ``[tool.coverage.run] source = ["src/modulo"]`` makes coverage's
    un-executed-source scan walk every module below ``backend/src/modulo``. A
    probe written there — even transiently — is touched into a shard's coverage
    data on save, and ``coverage combine`` then dies on a fresh checkout with
    "No source for code". This pins the gate tests' single probe-staging helper
    to a location outside the measured tree.
    """
    probe = _stage_probe(tmp_path, content)
    assert probe.read_text(encoding="utf-8") == content
    assert SRC.resolve() not in probe.resolve().parents, (
        f"vulture probe would be staged inside the measured coverage source tree: {probe}"
    )


def test_whitelist_exists_and_names_framework_symbols() -> None:
    assert WHITELIST.is_file(), f"whitelist missing at {WHITELIST}"
    content = WHITELIST.read_text(encoding="utf-8")
    for symbol in ("CursorResult", "Dialect", "compiler", "element", "version_id", "circuit_state"):
        assert f'"{symbol}"' in content, f"whitelist does not name framework-contract symbol {symbol!r}"
