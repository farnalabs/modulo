#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the workflow-file character scan.

Replaces `powershell -NoProfile -File tools/check-c1-chars.ps1`. Scans
.github/workflows/ (recursively, *.yml and *.yaml) for UTF-8 BOMs, C1 control
characters (U+0080-U+009F)
and non-ASCII characters (with an allowlist of legitimate punctuation). With
``--fix``, removes BOMs and C1 control chars. Exit 0 clean, 1 issues found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
WORKFLOWS_DIR = Path(REPO_ROOT, ".github", "workflows")

# Legitimate Unicode that sometimes appears in workflow files.
_ALLOWLIST = {
    0x2014,  # em dash
    0x2013,  # en dash
    0x2018,  # left single quote
    0x2019,  # right single quote
    0x201C,  # left double quote
    0x201D,  # right double quote
    0x2022,  # bullet
    0x2026,  # ellipsis
    0x2705,  # white heavy check mark
    0x2713,  # check mark
    0x2714,  # heavy check mark
    0x2192,  # rightwards arrow
}

_BOM = b"\xef\xbb\xbf"


def _read_bytes(path: str | Path) -> bytes:
    with Path(path).open("rb") as fh:
        return fh.read()


def _remove_c1_chars(name: str, text: str, fix: bool) -> tuple[str, bool]:
    """Report (and with *fix*, remove) C1 control chars from *text*."""
    fixed = text
    found = False
    for cp in range(0x80, 0xA0):
        ch = chr(cp)
        if ch in fixed:
            print(f"C1 control char U+{cp:04X} found in {name}", file=sys.stderr)
            found = True
            if fix:
                fixed = fixed.replace(ch, "")
                print(f"  Fixed: removed C1 char U+{cp:04X}", file=sys.stderr)
    return fixed, found


def _check_bom_and_c1(path: str | Path, fix: bool) -> bool:
    """Check (and optionally fix) a file's UTF-8 BOM and C1 control chars."""
    name = Path(path).name
    raw = _read_bytes(path)

    found = False
    had_bom = raw.startswith(_BOM)
    if had_bom:
        print(f"UTF-8 BOM found in {name}", file=sys.stderr)
        found = True
        if fix:
            raw = raw[len(_BOM) :]
            _write_bytes(path, raw)
            print("  Fixed: removed UTF-8 BOM", file=sys.stderr)

    text = raw.decode("utf-8", errors="replace")
    fixed, c1_found = _remove_c1_chars(name, text, fix)
    found |= c1_found
    if fix and fixed != text:
        _write_bytes(path, fixed.encode("utf-8"))
    return found


def _check_non_ascii_line(name: str, line_num: int, line: str) -> bool:
    """Report non-ASCII characters (outside the allowlist) on one line."""
    found = False
    for col, ch in enumerate(line, start=1):
        cp = ord(ch)
        if cp > 0x7E and cp not in _ALLOWLIST:
            print(
                f"Non-ASCII char U+{cp:04X} at {name}:{line_num}:{col}",
                file=sys.stderr,
            )
            start = max(0, col - 1 - 20)
            end = min(len(line), col - 1 + 20)
            print(f"  Context: ...{line[start:end]}...", file=sys.stderr)
            found = True
    return found


def _check_non_ascii(path: str | Path) -> bool:
    """Report non-ASCII characters (outside the allowlist) in a file."""
    name = Path(path).name
    raw = _read_bytes(path)
    raw = raw.removeprefix(_BOM)
    text = raw.decode("utf-8", errors="replace")
    lines = re.split(r"\r\n|\n", text)
    found = False
    for line_num, line in enumerate(lines, start=1):
        found |= _check_non_ascii_line(name, line_num, line)
    return found


def main() -> int:
    fix = "--fix" in sys.argv
    found = False

    files = sorted(p for ext in ("*.yml", "*.yaml") for p in WORKFLOWS_DIR.glob(f"**/{ext}"))
    if not files:
        print("No C1 control characters or BOMs found in .github/workflows/", file=sys.stderr)
        return 0

    # Check 1 + 2: UTF-8 BOM and C1 control chars
    for path in files:
        found |= _check_bom_and_c1(path, fix)

    # Check 3: Non-ASCII characters (excluding allowlist)
    for path in files:
        found |= _check_non_ascii(path)

    if found:
        print(
            "\nWorkflow files should contain only ASCII characters. "
            "Non-ASCII chars can cause GitHub Actions parser failures.",
            file=sys.stderr,
        )
        return 1
    print("No C1 control characters or BOMs found in .github/workflows/", file=sys.stderr)
    return 0


def _write_bytes(path: str | Path, data: bytes) -> None:
    with Path(path).open("wb") as fh:
        fh.write(data)


if __name__ == "__main__":
    sys.exit(main())
