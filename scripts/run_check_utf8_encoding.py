#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the UTF-8 BOM scan of tracked files.

Replaces `powershell -NoProfile -File tools/check-utf8-encoding.ps1`. Scans
only git-tracked files (git ls-files) with relevant extensions for UTF-8 BOM
(blocking in .github/workflows/, non-blocking elsewhere) and UTF-16 LE/BE BOMs
(blocking). With ``--fix``, removes BOMs by rewriting the file as UTF-8 without
BOM. Exit 0 clean, 1 blocking issue.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_EXTENSIONS = {
    ".py",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".md",
    ".cfg",
    ".ini",
    ".ps1",
    ".vue",
    ".ts",
    ".js",
}

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _write_utf8_no_bom(path: str) -> None:
    # Read as UTF-8, tolerating the BOM, then rewrite without it.
    with open(path, encoding="utf-8-sig", newline="") as fh:
        content = fh.read()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def main() -> int:
    fix = "--fix" in sys.argv
    found = False

    tracked = _tracked_files()
    if not tracked:
        print("No tracked files to check", file=sys.stderr)
        return 0

    for rel in tracked:
        ext = Path(rel).suffix.lower()
        if ext not in _EXTENSIONS:
            continue
        full = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            # Skip files we can't read.
            continue

        is_workflow = "/.github/workflows/" in "/" + rel

        if data.startswith(_UTF8_BOM):
            if is_workflow:
                print(f"BLOCKING: UTF-8 BOM in workflow file: {rel}", file=sys.stderr)
                found = True
            else:
                print(f"Non-blocking BOM: {rel}", file=sys.stderr)
            if fix:
                _write_utf8_no_bom(full)
                print("  Fixed: removed UTF-8 BOM", file=sys.stderr)
            continue

        if data.startswith((_UTF16_LE_BOM, _UTF16_BE_BOM)):
            print(f"UTF-16 BOM found: {rel}", file=sys.stderr)
            found = True
            if fix:
                _write_utf8_no_bom(full)
                print("  Fixed: converted to UTF-8", file=sys.stderr)

    if found:
        print(
            "\nBlocking: workflow files with BOMs found. Use --fix to auto-remove.",
            file=sys.stderr,
        )
        return 1
    print("No blocking encoding issues found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
