#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for sidebar nav translation key parity.

Replaces ``frontend/scripts/validate-translations.ps1``.

Checks nav label-key parity across the three files that define the sidebar:

1. **navigation.ts -> en-US.js** — every ``components.SidebarNav.item_*`` /
   ``group_*`` label key referenced in ``frontend/src/config/navigation.ts``
   has a matching entry in ``frontend/src/locales/en-US.js``.
2. **manifest.yaml -> en-US.js** — every label key the manifest's
   ``sidebar_groups`` declare resolves in the locale file. A group that
   declares no ``labelKey`` contributes the ``components.SidebarNav.group_<id>``
   fallback key navigation.ts itself falls back to.
3. **en-US.js -> manifest.yaml** (vice versa) — every manifest-shaped group nav
   key (``group_<word>``) in the locale file's SidebarNav block is live: declared as a group's
   ``labelKey`` or as the fallback of a declared group. Orphan group keys are
   stale locale entries left behind by a renamed/removed group.

Locale ``item_*`` keys that nothing references are reported as a loud WARNING
(dead keys — removal lives outside this gate's allowlist); parity for those is
enforced forward-only until they are cleaned up.

Supports ``--diff-range`` (e.g. "main...HEAD"); when given, the check is
skipped entirely if neither ``navigation.ts``, ``en-US.js`` nor
``manifest.yaml`` changed in that range.

Exit 0 clean, 1 missing keys / orphaned group keys.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# Word characters only, and only when immediately followed by a closing quote:
# a looser pattern runs past the closing backtick in navigation.ts's
# `components.SidebarNav.group_${id}` fallback template and swallows the rest
# of the file as one bogus "key" (that fallback is checked via the manifest's
# declared-group keys instead, which is what it resolves to at runtime).
_RE_NAV_LABEL = re.compile(r"components\.SidebarNav\.([A-Za-z0-9_]+)(?=['\"])")
_RE_LOCALE_KEY = re.compile(r'"((?:item|group)_[^"]+)"')
#: Manifest-shaped group nav label key (``group_build``), as opposed to the
#: UI-hint keys (``group_collapsed_hint``) SidebarNav.vue consumes directly.
_RE_GROUP_LABEL = re.compile(r"group_[a-z]+$")
#: The locale file's SidebarNav block (4-space-indented keys inside it).
#: Reverse-parity candidates are scoped to it: the flat file also carries
#: unrelated keys such as ``group_by`` / ``item_count`` that would read as
#: orphans otherwise.
_RE_SIDEBAR_BLOCK = re.compile(r'"SidebarNav":\s*\{(.*?)\n    \},', re.DOTALL)
_DIFF_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/\-]+$")

_NAV_PATH = str(Path("frontend") / "src" / "config" / "navigation.ts")
_LOCALE_PATH = str(Path("frontend") / "src" / "locales" / "en-US.js")
_MANIFEST_PATH = str(Path("frontend") / "src" / "manifest.yaml")

_NAMESPACE = "components.SidebarNav."


def _read(path: str) -> str:
    try:
        with Path(path).open(encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _files_changed(diff_range: str | None) -> bool:
    """Return True if navigation.ts, en-US.js or manifest.yaml changed."""
    if diff_range:
        if not _DIFF_RANGE_RE.match(diff_range):
            print(f"validate-translations: invalid --diff-range {diff_range!r}", file=sys.stderr)
            sys.exit(1)
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", diff_range],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
        )
    changed = set()
    for line in result.stdout.splitlines():
        changed.add(line.strip())
    return _NAV_PATH in changed or _LOCALE_PATH in changed or _MANIFEST_PATH in changed


def _manifest_group_keys(manifest_path: str) -> tuple[set[str], list[str]]:
    """Return the manifest's live group nav label keys plus any problems.

    A group's live key is its declared ``labelKey`` (within the
    ``components.SidebarNav.`` namespace) or — when it declares none — the
    ``group_<id>`` fallback navigation.ts itself uses.
    """
    content = _read(manifest_path)
    if not content:
        return set(), []
    try:
        manifest = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return set(), [f"{_MANIFEST_PATH}: invalid YAML - {exc}"]
    if not isinstance(manifest, dict):
        return set(), [f"{_MANIFEST_PATH}: manifest root must be a mapping"]
    groups = manifest.get("sidebar_groups")
    if not isinstance(groups, dict):
        return set(), [f"{_MANIFEST_PATH}: missing or non-mapping 'sidebar_groups'"]

    keys: set[str] = set()
    problems: list[str] = []
    for group_id, group in groups.items():
        if not isinstance(group, dict):
            problems.append(f"sidebar_group {group_id!r} must be a mapping")
            continue
        label_key = group.get("labelKey")
        if label_key is None:
            keys.add(f"group_{group_id}")
            continue
        if not isinstance(label_key, str) or not label_key:
            problems.append(f"sidebar_group {group_id!r} labelKey must be a non-empty string")
            continue
        if label_key.startswith(_NAMESPACE):
            keys.add(label_key.removeprefix(_NAMESPACE))
        # Keys outside the SidebarNav namespace are outside this gate's
        # domain; resolving arbitrary dotted keys is the product-map test's job.
    return keys, problems


def _sidebar_nav_keys(locale_content: str) -> set[str]:
    """item_*/group_* keys inside the locale file's SidebarNav block (else {})."""
    match = _RE_SIDEBAR_BLOCK.search(locale_content)
    if not match:
        return set()
    return set(_RE_LOCALE_KEY.findall(match.group(1)))


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

    # If a diff range is given and no relevant file changed, skip.
    if args.diff_range and not _files_changed(args.diff_range):
        print(
            f"validate-translations: no nav file changed in '{args.diff_range}' - skipping",
            file=sys.stderr,
        )
        return 0

    nav_path = str(Path(REPO_ROOT) / _NAV_PATH)
    locale_path = str(Path(REPO_ROOT) / _LOCALE_PATH)
    manifest_path = str(Path(REPO_ROOT) / _MANIFEST_PATH)

    nav_content = _read(nav_path)
    if not nav_content:
        print(f"validate-translations: {_NAV_PATH} not found - skipping", file=sys.stderr)
        return 0

    locale_content = _read(locale_path)
    if not locale_content:
        print(f"validate-translations: {_LOCALE_PATH} not found - skipping", file=sys.stderr)
        return 0

    # Extract referenced item_*/group_* keys from navigation.ts
    referenced: set[str] = set()
    for match in _RE_NAV_LABEL.finditer(nav_content):
        key = match.group(1)
        if key.startswith(("item_", "group_")):
            referenced.add(key)
    nav_referenced = set(referenced)

    # Extract defined item_*/group_* keys from en-US.js
    defined: set[str] = set()
    for match in _RE_LOCALE_KEY.finditer(locale_content):
        defined.add(match.group(1))

    # Manifest label keys: forward (must resolve in the locale file) and the
    # liveness set for the vice-versa direction below.
    manifest_keys: set[str] = set()
    manifest_problems: list[str] = []
    if Path(manifest_path).exists():
        manifest_keys, manifest_problems = _manifest_group_keys(manifest_path)
        referenced |= manifest_keys
    else:
        print(
            f"validate-translations: {_MANIFEST_PATH} not found - skipping manifest label-key parity",
            file=sys.stderr,
        )

    # 1 + 2 (forward): every referenced / declared nav label key resolves.
    missing = sorted(f"{_NAMESPACE}{k}" for k in referenced if k not in defined)
    if missing:
        for key in missing:
            print(f"FAIL: missing translation key: {key}", file=sys.stderr)
        print(
            f"\nvalidate-translations: FAILED - {len(missing)} key(s) referenced in "
            "navigation.ts / manifest.yaml but not defined in en-US.js",
            file=sys.stderr,
        )
        return 1

    if manifest_problems:
        for problem in manifest_problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        print(
            f"\nvalidate-translations: FAILED - {len(manifest_problems)} manifest label-key problem(s)",
            file=sys.stderr,
        )
        return 1

    # 3 (vice versa): every manifest-shaped group nav key in the locale's
    # SidebarNav block is live.
    sidebar_keys = _sidebar_nav_keys(locale_content)
    if Path(manifest_path).exists():
        if sidebar_keys:
            orphans = sorted(k for k in sidebar_keys if _RE_GROUP_LABEL.match(k) and k not in manifest_keys)
            if orphans:
                for key in orphans:
                    print(
                        f"FAIL: locale nav key {_NAMESPACE}{key} is not a manifest sidebar_group "
                        "labelKey or declared-group fallback (orphaned group key)",
                        file=sys.stderr,
                    )
                print(
                    f"\nvalidate-translations: FAILED - {len(orphans)} orphaned group nav key(s) in en-US.js",
                    file=sys.stderr,
                )
                return 1
        else:
            print(
                "validate-translations: no SidebarNav block in en-US.js - skipping orphan group-key check",
                file=sys.stderr,
            )

    # Loud but non-blocking: locale item_* keys nothing in navigation.ts
    # references (dead keys - cleanup tracked separately from this gate).
    stale_items = sorted(k for k in sidebar_keys if k.startswith("item_") and k not in nav_referenced)
    if stale_items:
        print(
            f"WARNING: {len(stale_items)} locale item_* nav key(s) not referenced by navigation.ts "
            f"(dead keys): {', '.join(stale_items)}",
            file=sys.stderr,
        )

    print(
        f"validate-translations: OK - all {len(referenced)} sidebar nav translation keys present",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
