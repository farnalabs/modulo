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

Extra arguments (``<args>``) are forwarded to the pnpm script.  This is
used by the ESLint pre-commit hook: ``pass_filenames: true`` makes
pre-commit append the staged filenames, and this script forwards them to
``pnpm run lint`` so only the changed files are linted instead of the
entire ``src/`` directory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
FRONTEND_DIR = str(Path(REPO_ROOT) / "frontend")
PACKAGE_JSON = str(Path(FRONTEND_DIR) / "package.json")

_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./-]*$")


def find_package_manager() -> str | None:
    if sys.platform == "win32":
        # Prefer the .cmd shim: the extensionless `pnpm`/`npm` file is a POSIX
        # shell script that CreateProcess cannot launch on Windows.
        return shutil.which("pnpm.cmd") or shutil.which("pnpm") or shutil.which("npm.cmd") or shutil.which("npm")
    return shutil.which("pnpm") or shutil.which("npm")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {Path(__file__).name} <script> [extra-args...]", file=sys.stderr)
        return 2

    script = sys.argv[1]
    extra_args = sys.argv[2:]

    if not _SCRIPT_NAME_RE.match(script):
        print(f"{Path(__file__).name}: invalid script name {script!r}", file=sys.stderr)
        return 2

    if not Path(PACKAGE_JSON).is_file():
        print(f"{Path(__file__).name}: {PACKAGE_JSON} not found - skipping", file=sys.stderr)
        return 0

    with Path(PACKAGE_JSON).open(encoding="utf-8-sig") as fh:
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

    # When extra_args are present (filenames from pre-commit with
    # pass_filenames: true), we cannot use "pnpm run lint" because its
    # script hardcodes `eslint src` — extra filenames would be additive,
    # not a replacement.  Instead, run eslint directly with just the staged
    # files.  The eslint flat config (eslint.config.mjs) applies to any
    # file path, so this works identically to the full-directory lint.
    if extra_args:
        eslint_bin = FRONTEND_DIR / "node_modules" / ".bin" / "eslint"
        if not eslint_bin.is_file():
            # Fallback: use npx (slower but always available)
            eslint_cmd = ["npx", "eslint"]
        else:
            if sys.platform == "win32":
                eslint_cmd = ["cmd.exe", "/c", str(eslint_bin)]
            else:
                eslint_cmd = [str(eslint_bin)]
        cmd = [*eslint_cmd, "--cache", "--cache-location", ".cache/eslint", *extra_args]
        result = subprocess.run(cmd, cwd=FRONTEND_DIR, check=False)
        return result.returncode

    if sys.platform == "win32":
        # CreateProcess cannot execute .cmd/.bat shims directly (WinError 193);
        # they must be launched through the Windows command interpreter.
        cmd = ["cmd.exe", "/c", pm, "run", script]
    else:
        cmd = [pm, "run", script]
    result = subprocess.run(cmd, cwd=FRONTEND_DIR, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
