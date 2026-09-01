#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the workflow-file character scan.

Replaces `powershell -NoProfile -File tools/check-c1-chars.ps1`. Scans
.github/workflows/*.yml for UTF-8 BOMs, C1 control characters (U+0080-U+009F)
and non-ASCII characters (with an allowlist of legitimate punctuation). With
``--fix``, removes BOMs and C1 control chars. Exit 0 clean, 1 issues found.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_GLOB = os.path.join(REPO_ROOT, ".github", "workflows", "*.yml")

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


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main() -> int:
    fix = "--fix" in sys.argv
    found = False

    files = sorted(glob.glob(WORKFLOWS_GLOB))
    if not files:
        print("No C1 control characters or BOMs found in .github/workflows/", file=sys.stderr)
        return 0

    # Check 1 + 2: UTF-8 BOM and C1 control chars
    for path in files:
        name = Path(path).name
        raw = _read_bytes(path)

        had_bom = raw.startswith(_BOM)
        if had_bom:
            print(f"UTF-8 BOM found in {name}", file=sys.stderr)
            found = True
            if fix:
                raw = raw[len(_BOM) :]
                _write_bytes(path, raw)
                print("  Fixed: removed UTF-8 BOM", file=sys.stderr)

        text = raw.decode("utf-8", errors="replace")
        fixed = text
        for cp in range(0x80, 0xA0):
            ch = chr(cp)
            if ch in fixed:
                print(f"C1 control char U+{cp:04X} found in {name}", file=sys.stderr)
                found = True
                if fix:
                    fixed = fixed.replace(ch, "")
                    print(f"  Fixed: removed C1 char U+{cp:04X}", file=sys.stderr)
        if fix and fixed != text:
            _write_bytes(path, fixed.encode("utf-8"))

    # Check 3: Non-ASCII characters (excluding allowlist)
    for path in files:
        name = Path(path).name
        raw = _read_bytes(path)
        raw = raw.removeprefix(_BOM)
        text = raw.decode("utf-8", errors="replace")
        lines = re.split(r"\r\n|\n", text)
        for line_num, line in enumerate(lines, start=1):
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

    if found:
        print(
            "\nWorkflow files should contain only ASCII characters. "
            "Non-ASCII chars can cause GitHub Actions parser failures.",
            file=sys.stderr,
        )
        return 1
    print("No C1 control characters or BOMs found in .github/workflows/", file=sys.stderr)
    return 0


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


if __name__ == "__main__":
    sys.exit(main())
