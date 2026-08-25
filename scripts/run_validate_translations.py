#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for sidebar nav translation key parity.

Replaces `frontend/scripts/validate-translations.ps1`.

Checks that every ``components.SidebarNav.item_*`` key referenced in
``frontend/src/config/navigation.ts`` has a matching entry in
``frontend/src/locales/en-US.js``.

Supports ``--diff-range`` (e.g. "main...HEAD"); when given, the check is
skipped entirely if neither ``navigation.ts`` nor ``en-US.js`` changed in that
range.

Exit 0 clean, 1 missing keys.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RE_NAV_LABEL = re.compile(r"components\.SidebarNav\.([^']+)")
_RE_LOCALE_KEY = re.compile(r'"(item_[^"]+)"')
_DIFF_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/\-]+$")

_NAV_PATH = os.path.join("frontend", "src", "config", "navigation.ts")
_LOCALE_PATH = os.path.join("frontend", "src", "locales", "en-US.js")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _files_changed(diff_range: str | None) -> bool:
    """Return True if navigation.ts or en-US.js changed in the given range."""
    if diff_range:
        if not _DIFF_RANGE_RE.match(diff_range):
            print(f"validate-translations: invalid --diff-range {diff_range!r}", file=sys.stderr)
            sys.exit(1)
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", diff_range],
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
        )
    changed = set()
    for line in result.stdout.splitlines():
        changed.add(line.strip())
    return _NAV_PATH in changed or _LOCALE_PATH in changed


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check sidebar nav translation key parity.",
    )
    parser.add_argument(
        "--diff-range",
        default=None,
        help="Optional git diff range (e.g. main...HEAD) for the changed-file set.",
    )
    args = parser.parse_args(argv)

    # If a diff range is given and neither file changed, skip.
    if args.diff_range and not _files_changed(args.diff_range):
        print(
            f"validate-translations: neither {_NAV_PATH} nor {_LOCALE_PATH} changed in '{args.diff_range}' - skipping",
            file=sys.stderr,
        )
        return 0

    nav_path = os.path.join(REPO_ROOT, _NAV_PATH)
    locale_path = os.path.join(REPO_ROOT, _LOCALE_PATH)

    nav_content = _read(nav_path)
    if not nav_content:
        print(f"validate-translations: {_NAV_PATH} not found - skipping", file=sys.stderr)
        return 0

    locale_content = _read(locale_path)
    if not locale_content:
        print(f"validate-translations: {_LOCALE_PATH} not found - skipping", file=sys.stderr)
        return 0

    # Extract referenced item_* keys from navigation.ts
    referenced: set[str] = set()
    for match in _RE_NAV_LABEL.finditer(nav_content):
        key = match.group(1)
        if key.startswith("item_"):
            referenced.add(key)

    # Extract defined item_* keys from en-US.js
    defined: set[str] = set()
    for match in _RE_LOCALE_KEY.finditer(locale_content):
        defined.add(match.group(1))

    missing = sorted(f"components.SidebarNav.{k}" for k in referenced if k not in defined)

    if missing:
        for key in missing:
            print(f"FAIL: missing translation key: {key}", file=sys.stderr)
        print(
            f"\nvalidate-translations: FAILED - {len(missing)} key(s) referenced in navigation.ts but not defined in en-US.js",
            file=sys.stderr,
        )
        return 1

    print(
        f"validate-translations: OK - all {len(referenced)} sidebar nav translation keys present",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
