#!/usr/bin/env python3
"""Cross-platform pre-commit/CI wrapper for the uv lockfile freshness check.

Runs `uv lock --project backend --check` from the repo root. Exit 0 when the
lockfile is fresh, 1 when it is stale or has dependency conflicts.

FAR-2308: this gate is FAIL-CLOSED. A `backend/uv.lock` that has drifted from
`backend/pyproject.toml` makes every `uv run` in a commit gate rewrite the
lockfile mid-run, pre-commit's stash/restore collides, and pushes from any
branch based on the stale tree abort. `uv sync --frozen` (used throughout CI)
installs from the lock as-is and cannot detect the drift, so this check is the
only gate that can. If the check cannot be executed at all (uv missing,
timeout, unexpected failure) the gate exits 1 rather than passing silently.

`uv lock --check` never writes the lockfile, so running this gate cannot
"fix" the drift it is reporting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_TIMEOUT_SECONDS = 180

_FIX_HINT = (
    "backend/uv.lock is stale: it records the project version and the resolved "
    "dependency set, and it no longer matches backend/pyproject.toml. "
    "Run `uv lock` in backend/ and commit backend/uv.lock."
)


def _fail(message: str) -> int:
    print(f"FAILED: {message}", file=sys.stderr)
    return 1


def main() -> int:
    print("Checking uv lockfile freshness...", file=sys.stderr)
    try:
        result = subprocess.run(
            ["uv", "lock", "--project", "backend", "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _fail("`uv` is not installed or not on PATH - cannot verify backend/uv.lock (failing closed).")
    except subprocess.TimeoutExpired:
        return _fail(f"`uv lock --check` did not finish within {_TIMEOUT_SECONDS}s (failing closed).")
    except OSError as exc:
        return _fail(f"could not run `uv lock --check`: {exc} (failing closed).")
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        return _fail(f"{_FIX_HINT} (`uv lock --check` exited {result.returncode})")
    print("uv lockfile is fresh with no dependency conflicts", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
