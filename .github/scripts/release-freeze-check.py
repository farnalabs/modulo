#!/usr/bin/env python3
"""Shared release-freeze gate for merge-queue.yml (FAR-...).

Both the entry `freeze-gate` job and the pre-merge TOCTOU re-verify step ask the
same question -- is the `release-freeze` tag present right now? They previously
held two copies of the query + retry + fail-closed logic that could (and did)
diverge. This helper is the single source of truth so the two safety-critical
checks stay identical.

Policy (identical for both callers):
  * Query `gh api .../git/matching-refs/tags/release-freeze` and read the JSON
    array length via `--jq 'length'`.
  * Retry up to `FREEZE_GATE_ATTEMPTS` (default 5) times on transient failure
    (no data / non-numeric output / gh error). Backoff is `attempt * BACKOFF`
    seconds, but is SKIPPED on the final attempt so a terminal failure exits
    promptly (no pointless trailing sleep).
  * FAIL CLOSED: if every attempt fails to return a valid count we cannot tell
    whether the freeze is active, so we assume it is. The captured stderr from
    the final attempt is surfaced in the message so a persistent gate outage is
    diagnosable from the run log (house pattern: per-attempt diagnostics).
  * Stdout: a single `verdict|message` line.
  * Exit code: 0 = determined (frozen/open); 2 = fail-closed.

Unit tests: backend/tests/unit/scripts/test_release_freeze_check.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time


def _run_gh(repo: str):
    """Invoke gh to count matching release-freeze refs. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [
            "gh", "api",
            f"repos/{repo}/git/matching-refs/tags/release-freeze",
            "--jq", "length",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), (proc.stderr or "").strip()


def query_release_freeze(repo: str, attempts: int, backoff: int, run_gh=_run_gh):
    """Return (verdict, message) where verdict is 'frozen'|'open'|'fail-closed'."""
    last_stderr = ""
    for attempt in range(1, attempts + 1):
        try:
            rc, out, err = run_gh(repo)
        except FileNotFoundError as exc:
            rc, out, err = None, "", f"gh executable not found: {exc}"
        last_stderr = err or "(no error output)"
        if rc == 0 and out.isdigit():
            count = int(out)
            if count > 0:
                return "frozen", f"'release-freeze' tag present (count={count})"
            return "open", f"no 'release-freeze' tag present (count={count})"
        if attempt < attempts:
            print(
                f"::warning::release-freeze query failed (attempt {attempt}/{attempts})"
                + (f": {err}" if err else " - no data")
                + " - retrying",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(attempt * backoff)
    return (
        "fail-closed",
        f"gh api failed querying release-freeze tag after {attempts} attempts"
        + (f" - last error: {last_stderr}" if last_stderr else ""),
    )


def main(argv):
    del argv
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("fail-closed|GITHUB_REPOSITORY not set - failing CLOSED", flush=True)
        return 2
    attempts = int(os.environ.get("FREEZE_GATE_ATTEMPTS", "5"))
    backoff = int(os.environ.get("FREEZE_GATE_BACKOFF", "5"))
    verdict, message = query_release_freeze(repo, attempts, backoff)
    print(f"{verdict}|{message}", flush=True)
    return 0 if verdict in ("frozen", "open") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
