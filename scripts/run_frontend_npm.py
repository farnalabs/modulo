#!/usr/bin/env python3
"""Cross-platform pre-commit runner for frontend pnpm scripts.

Replaces `bash -c 'cd frontend && pnpm run <script> --if-present'`, which
breaks on Windows where `bash` resolves to WSL and cannot execute the
Windows-installed node_modules binaries.

Behaviour is identical on all platforms:
  - runs `pnpm run <script>` in frontend/ when <script> exists in
    frontend/package.json (the `--if-present` semantic), falling back to
    `npm run <script>` only when pnpm is not installed
  - fails when both pnpm and npm are missing or the script fails
  - exits 0 without running anything when the script is absent from
    frontend/package.json
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")
PACKAGE_JSON = os.path.join(FRONTEND_DIR, "package.json")

_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./-]*$")


def find_package_manager() -> str | None:
    if sys.platform == "win32":
        # Prefer the .cmd shim: the extensionless `pnpm`/`npm` file is a POSIX
        # shell script that CreateProcess cannot launch on Windows.
        return shutil.which("pnpm.cmd") or shutil.which("pnpm") or shutil.which("npm.cmd") or shutil.which("npm")
    return shutil.which("pnpm") or shutil.which("npm")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(__file__).name} <script>", file=sys.stderr)
        return 2

    script = sys.argv[1]

    if not _SCRIPT_NAME_RE.match(script):
        print(f"{Path(__file__).name}: invalid script name {script!r}", file=sys.stderr)
        return 2

    if not Path(PACKAGE_JSON).is_file():
        print(f"{Path(__file__).name}: {PACKAGE_JSON} not found - skipping", file=sys.stderr)
        return 0

    with open(PACKAGE_JSON, encoding="utf-8-sig") as fh:
        scripts = json.load(fh).get("scripts", {})
    if script not in scripts:
        print(
            f"{Path(__file__).name}: no '{script}' script in frontend/package.json - skipping",
            file=sys.stderr,
        )
        return 0

    pm = find_package_manager()
    if pm is None:
        print(f"{Path(__file__).name}: neither pnpm nor npm found on PATH", file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # CreateProcess cannot execute .cmd/.bat shims directly (WinError 193);
        # they must be launched through the Windows command interpreter.
        cmd = ["cmd.exe", "/c", pm, "run", script]
    else:
        cmd = [pm, "run", script]
    result = subprocess.run(cmd, cwd=FRONTEND_DIR)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
