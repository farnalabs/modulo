#!/usr/bin/env python3
"""TEMPORARY vocabulary-sweep guard (FAR-588, D3 of the Agent Execution Tiers
delivery, ADR 029 in Repos/devtools/adr/).

Forbids the retired user-facing terms "sandbox agent" / "external agent"
(space or hyphen forms, case-insensitive) in user-facing surfaces:

  - frontend/src/locales/*.js (message values)
  - frontend/src/views/**/*.vue (string literals)
  - frontend/src/components/**/*.vue (string literals)

The new vocabulary is "Inline Prompt" (node_type `agent`) and "Runner"
(node_type `sandbox_agent`), with the Runner packagings "Bundled Runner
(Docker)" / "External Runner (E2B)" / "Local".

FROZEN IDENTIFIERS - do not fix by rewording: the wire values `agent` and
`sandbox_agent` (node_type), the `sandbox_agent` locale KEY, and the
`sandbox_agent.*` structured-log namespaces are allowlisted. They use the
underscore form (`sandbox_agent`), which these space/hyphen patterns never
match, so the allowlist is enforced structurally rather than by line
exemptions.

Removal: delete this script and its .pre-commit-config.yaml entry at GA -
a one-time migration does not warrant a permanent policing surface.

Exit 0 clean, 1 issues found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_SURFACE: list[Path] = [
    REPO_ROOT / "frontend" / "src" / "locales",
    REPO_ROOT / "frontend" / "src" / "views",
    REPO_ROOT / "frontend" / "src" / "components",
]

# Retired vocabulary: space and hyphen compounds, case-insensitive.
FORBIDDEN = re.compile(r"sandbox[ -]agent|external[ -]agent", re.IGNORECASE)


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for base in SCAN_SURFACE:
        if not base.exists():
            continue
        if base.name == "locales":
            files.extend(sorted(base.glob("*.js")))
        else:
            files.extend(sorted(base.rglob("*.vue")))
    return files


def main() -> int:
    findings: list[str] = []
    for path in _scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_num, line in enumerate(text.splitlines(), start=1):
            match = FORBIDDEN.search(line)
            if match:
                findings.append(f"{rel}:{line_num}: retired term {match.group(0)!r}")

    if findings:
        print(
            "Retired user-facing vocabulary found (superseded by ADR 029 - use "
            "'Inline Prompt' / 'Runner' / 'Bundled Runner (Docker)' / "
            "'External Runner (E2B)' / 'Local'):",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nNote: wire-format identifiers (node_type values, the sandbox_agent "
            "locale KEY, sandbox_agent.* log namespaces) are frozen identifiers - "
            "do not fix by rewording. This hook is TEMPORARY and is removed at GA.",
            file=sys.stderr,
        )
        return 1

    print("Vocabulary sweep clean: no retired user-facing terms in locales/views/components.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
