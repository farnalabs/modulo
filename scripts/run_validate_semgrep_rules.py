"""Validate the shape of rule files under .semgrep/ before commit.

What it checks, per ``.semgrep/**/*.yml`` and ``*.yaml`` file:

1. The file has NO UTF-8 BOM (the first three bytes must not be EF BB BF).
2. The file declares a top-level ``rules:`` key -- a line matching
   ``^rules:\\s*$`` after optional leading blank lines and ``#`` comments.

Scope: this validates rule-FILE SHAPE ONLY. It does not scan any code, does
not parse the YAML graph, and does not judge whether a rule matches anything.
The real semgrep scan (``scripts/run_semgrep.py``) is a separate gate.

Why the check exists: semgrep >= 1.178 parses a UTF-8 BOM as part of the
first key and aborts the whole scan with ``missing 'rules' as top-level
key``. A run of nine rule files carrying a BOM landed silently because the
semgrep pre-commit hook is pattern-gated to ``^backend/src/`` (a change to
``.semgrep/**`` triggered no local validation) and the repo's ``check-yaml``
hook cannot catch it (a BOM is valid to a YAML parser). This closes that
gap with a stdlib-only, cross-platform gate.

Exit status: 1 if the .semgrep/ directory is missing, if no rule files are
found, or if any file fails; 0 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BOM = b"\xef\xbb\xbf"
_RULES_KEY = re.compile(r"^rules:\s*$")


def collect_rule_files(semgrep_dir: Path) -> list[Path]:
    """Return sorted .yml/.yaml files under semgrep_dir (recursive)."""
    files: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        files.extend(semgrep_dir.rglob(pattern))
    return sorted(set(files))


def has_bom(path: Path) -> bool:
    """True if the file starts with a UTF-8 BOM (read as bytes, not text)."""
    with path.open("rb") as handle:
        return handle.read(3) == _BOM


def has_top_level_rules_key(path: Path) -> bool:
    """True if a ``rules:`` line follows only blank lines and # comments."""
    with path.open("rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return bool(_RULES_KEY.match(line))
    return False


def check_file(path: Path) -> str | None:
    """Return a failure reason for path, or None when the file passes."""
    if has_bom(path):
        return "UTF-8 BOM present (semgrep aborts: missing 'rules' as top-level key)"
    if not has_top_level_rules_key(path):
        return "no top-level 'rules:' key"
    return None


def main() -> int:
    """Check every rule file and report per-file lines plus a summary."""
    repo_root = Path(__file__).resolve().parent.parent
    semgrep_dir = repo_root / ".semgrep"
    if not semgrep_dir.is_dir():
        print(f"FAIL: .semgrep/ directory not found at {semgrep_dir}")
        return 1

    files = collect_rule_files(semgrep_dir)
    if not files:
        print(f"FAIL: no .yml/.yaml rule files found under {semgrep_dir}")
        return 1

    failed = 0
    for path in files:
        reason = check_file(path)
        display = path.relative_to(repo_root).as_posix()
        if reason is None:
            print(f"OK {display}")
        else:
            failed += 1
            print(f"FAIL {display}: {reason}")

    print(f"checked={len(files)} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
