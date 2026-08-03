#!/usr/bin/env python3
"""Cross-platform pre-commit runner for frontend npm scripts.

Replaces `bash -c 'cd frontend && npm run <script> --if-present'`, which
breaks on Windows where `bash` resolves to WSL and cannot execute the
Windows-installed node_modules binaries.

Behaviour is identical on all platforms:
  - runs `npm run <script>` in frontend/ when <script> exists in
    frontend/package.json (the `--if-present` semantic)
  - fails when npm is missing or the npm script fails
  - exits 0 without running anything when the script is absent from
    frontend/package.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")
PACKAGE_JSON = os.path.join(FRONTEND_DIR, "package.json")


def find_npm() -> str | None:
    if sys.platform == "win32":
        # Prefer the .cmd shim: the extensionless `npm` file is a POSIX shell
        # script that CreateProcess cannot launch on Windows.
        return shutil.which("npm.cmd") or shutil.which("npm")
    return shutil.which("npm")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {os.path.basename(__file__)} <npm-script>", file=sys.stderr)
        return 2

    script = sys.argv[1]

    if not os.path.isfile(PACKAGE_JSON):
        print(f"{os.path.basename(__file__)}: {PACKAGE_JSON} not found - skipping", file=sys.stderr)
        return 0

    with open(PACKAGE_JSON, encoding="utf-8-sig") as fh:
        scripts = json.load(fh).get("scripts", {})
    if script not in scripts:
        print(
            f"{os.path.basename(__file__)}: no '{script}' script in frontend/package.json - skipping",
            file=sys.stderr,
        )
        return 0

    npm = find_npm()
    if npm is None:
        print(f"{os.path.basename(__file__)}: npm not found on PATH", file=sys.stderr)
        return 1

    result = subprocess.run([npm, "run", script], cwd=FRONTEND_DIR)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
