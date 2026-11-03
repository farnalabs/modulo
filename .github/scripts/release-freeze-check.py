#!/usr/bin/env python3
"""Shared release-freeze gate for merge-queue.yml (FAR-1365, FAR-2326).

Both the entry `freeze-gate` job and the pre-merge TOCTOU re-verify step ask the
same question -- is the `release-freeze` branch present right now? They previously
held two copies of the query + retry + fail-closed logic that could (and did)
diverge. This helper is the single source of truth so the two safety-critical
checks stay identical.

Policy (identical for both callers):
  * Query `gh api .../git/matching-refs/heads/release-freeze` and read the JSON
    array length via `--jq 'length'`. The marker is a BRANCH under
    `refs/heads/`, never a tag -- the tag namespace is not consulted at all.
  * Retry up to `FREEZE_GATE_ATTEMPTS` (default 5) times on transient failure
    (no data / non-numeric output / gh error). Backoff is `attempt * BACKOFF`
    seconds, but is SKIPPED on the final attempt so a terminal failure exits
    promptly (no pointless trailing sleep).
  * FAIL CLOSED: if every attempt fails to return a valid count we cannot tell
    whether the freeze is active, so we assume it is. The captured stderr from
    the final attempt is surfaced in the message so a persistent gate outage is
    diagnosable from the run log (house pattern: per-attempt diagnostics).
  * Stdout: the verdict line `verdict|message` FIRST, then -- only when the
    verdict is `frozen` -- best-effort detail lines describing the marker
    (`sha=`, `age_seconds=`, `stale=`, `max_freeze_hours=`). Callers parse the
    verdict from the FIRST line; the detail lines are decoration and are
    omitted (with a `::warning::` on stderr) when they cannot be read.
  * Exit code: 0 = determined (frozen/open); 2 = fail-closed.

FAR-2326 - why the detail and the `stale` flag exist:
  A `cut`/`recut` creates the marker and only the final GA `promote` removes
  it, so a failed release can leave the marker in place indefinitely and the
  merge queue silently stops merging. On 2026-11-02/03 the marker survived a
  release that failed 4x on an unrelated CI flag: ~12 hours with 20 approved
  green PRs waiting, while every merge-queue run still concluded `success`
  (the queue job was skipped). A halted queue must not be indistinguishable
  from a healthy idle one, so the age travels with the verdict.

  The age is measured from the marker's COMMIT stamp
  (`commit.commit.committer.date`), not from the moment the branch was created
  (GitHub exposes no ref-creation time). A cut re-points the branch at a fresh
  `main` commit, so the measured age is a LOWER BOUND on the freeze's true age
  -- it never invents staleness, it can only report it late. An unparseable
  stamp is unmeasurable (`age_seconds` omitted) and therefore never reads as
  stale; a future-dated stamp clamps to zero for the same reason.

  `stale` is advisory. It NEVER changes the verdict and it NEVER fails a gate:
  a release legitimately holds the freeze for hours, so staleness raises an
  operator alert (merge-queue-watchdog.yml) instead of opening the gate.
  Threshold: `FREEZE_MAX_HOURS` (default 4), overridable per call site.

Unit tests: backend/tests/unit/scripts/test_release_freeze_check.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime

#: How long the marker may exist before it is reported as stale. A release
#: legitimately holds the freeze while the RC is soaked and validated, so this
#: is a "somebody should look" threshold, never an automatic release.
DEFAULT_MAX_FREEZE_HOURS = "4"


def _run_gh(repo: str):
    """Invoke gh to count matching release-freeze refs. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/git/matching-refs/heads/release-freeze",
            "--jq",
            "length",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), (proc.stderr or "").strip()


def _run_gh_marker(repo: str):
    """Read the marker branch's head: '<sha>\t<iso committer date>'.

    Separate from the count query on purpose: the count query decides the
    verdict (safety-critical, fail-closed) and must stay untouched, while this
    one is decoration that may fail without affecting the verdict.
    """
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/branches/release-freeze",
            "--jq",
            "[.commit.sha, .commit.commit.committer.date] | @tsv",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), (proc.stderr or "").strip()


def _run_query(run_gh, repo: str) -> tuple[int | None, str, str]:
    """Run a gh query once, mapping a missing executable to an error string."""
    try:
        return run_gh(repo)
    except FileNotFoundError as exc:
        return None, "", f"gh executable not found: {exc}"


def _interpret_query_result(rc: int | None, out: str) -> tuple[str, str] | None:
    """Return the verdict for a successful branch-count response, else ``None``."""
    if rc != 0 or not out.isdigit():
        return None
    count = int(out)
    if count > 0:
        return "frozen", f"'release-freeze' branch present (count={count})"
    return "open", f"no 'release-freeze' branch present (count={count})"


def _warn_retry(attempt: int, attempts: int, err: str) -> None:
    """Emit the retry warning for a failed branch query."""
    print(
        f"::warning::release-freeze query failed (attempt {attempt}/{attempts})"
        + (f": {err}" if err else " - no data")
        + " - retrying",
        file=sys.stderr,
        flush=True,
    )


def _final_failure(attempts: int, last_stderr: str) -> tuple[str, str]:
    """Return the fail-closed verdict after every attempt failed."""
    return (
        "fail-closed",
        f"gh api failed querying release-freeze branch after {attempts} attempts"
        + (f" - last error: {last_stderr}" if last_stderr else ""),
    )


def query_release_freeze(repo: str, attempts: int, backoff: int, run_gh=_run_gh):
    """Return (verdict, message) where verdict is 'frozen'|'open'|'fail-closed'."""
    last_stderr = ""
    for attempt in range(1, attempts + 1):
        rc, out, err = _run_query(run_gh, repo)
        last_stderr = err or "(no error output)"
        verdict = _interpret_query_result(rc, out)
        if verdict is not None:
            return verdict
        if attempt < attempts:
            _warn_retry(attempt, attempts, err)
            time.sleep(attempt * backoff)
    return _final_failure(attempts, last_stderr)


def parse_marker(out: str) -> tuple[str, str] | None:
    """Split a '<sha>\\t<iso date>' marker payload, or ``None`` when malformed."""
    parts = out.split("\t")
    if len(parts) != 2:
        return None
    sha = parts[0].strip()
    stamp = parts[1].strip()
    if not sha or not stamp:
        return None
    return sha, stamp


def commit_age_seconds(stamp: str, now: datetime | None = None) -> int | None:
    """Seconds between an ISO-8601 commit stamp and now.

    Returns ``None`` when the stamp cannot be parsed (unmeasurable, so it never
    reads as stale) and clamps a future-dated stamp to zero (clock skew is not
    evidence of a long freeze).
    """
    # gh emits RFC3339 with a trailing "Z". Python 3.11+ parses that natively,
    # but normalise it explicitly so this script also works on an older python3
    # -- otherwise every stamp would read as unmeasurable and staleness would
    # silently never fire when the helper is run by hand.
    normalised = f"{stamp[:-1]}+00:00" if stamp.endswith("Z") else stamp
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0, int((reference - parsed).total_seconds()))


def is_stale(age_seconds: int, max_hours: float) -> bool:
    """True when the marker has existed for at least ``max_hours`` hours."""
    return age_seconds >= max_hours * 3600


def query_freeze_marker(repo: str, run_gh=_run_gh_marker) -> tuple[str, int | None] | None:
    """Best-effort (sha, age_seconds) for the marker branch head.

    ``None`` when the detail cannot be read at all: the caller then reports the
    freeze without an age rather than guessing one. This never affects the
    verdict.
    """
    rc, out, err = _run_query(run_gh, repo)
    if rc != 0:
        print(
            "::warning::release-freeze marker detail unavailable"
            + (f": {err}" if err else " - no output")
            + " - reporting the freeze without an age/staleness figure",
            file=sys.stderr,
            flush=True,
        )
        return None
    parsed = parse_marker(out)
    if parsed is None:
        print(
            f"::warning::release-freeze marker detail unparseable ({out!r})"
            " - reporting the freeze without an age/staleness figure",
            file=sys.stderr,
            flush=True,
        )
        return None
    sha, stamp = parsed
    age = commit_age_seconds(stamp)
    if age is None:
        print(
            f"::warning::release-freeze marker stamp {stamp!r} is not ISO-8601"
            " - reporting the freeze without an age/staleness figure",
            file=sys.stderr,
            flush=True,
        )
    return sha, age


def max_freeze_hours() -> float:
    """Read FREEZE_MAX_HOURS, falling back to the default when it is unusable."""
    raw = os.environ.get("FREEZE_MAX_HOURS", DEFAULT_MAX_FREEZE_HOURS)
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        print(
            f"::warning::FREEZE_MAX_HOURS={raw!r} is not a positive number -"
            f" using the default {DEFAULT_MAX_FREEZE_HOURS}h",
            file=sys.stderr,
            flush=True,
        )
        return float(DEFAULT_MAX_FREEZE_HOURS)
    return value


def detail_lines(repo: str, freeze_max_hours: float, run_gh=_run_gh_marker) -> list[str]:
    """Key=value lines describing the marker, printed after a `frozen` verdict."""
    marker = query_freeze_marker(repo, run_gh)
    if marker is None:
        return []
    sha, age = marker
    lines = [f"sha={sha}"]
    if age is None:
        return lines
    stale = "true" if is_stale(age, freeze_max_hours) else "false"
    lines.extend(
        [
            f"age_seconds={age}",
            f"stale={stale}",
            f"max_freeze_hours={freeze_max_hours:g}",
        ]
    )
    return lines


def main(argv):
    del argv
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("fail-closed|GITHUB_REPOSITORY not set - failing CLOSED", flush=True)
        return 2
    attempts = int(os.environ.get("FREEZE_GATE_ATTEMPTS", "5"))
    backoff = int(os.environ.get("FREEZE_GATE_BACKOFF", "5"))
    verdict, message = query_release_freeze(repo, attempts, backoff)
    lines = [f"{verdict}|{message}"]
    if verdict == "frozen":
        lines.extend(detail_lines(repo, max_freeze_hours()))
    print("\n".join(lines), flush=True)
    return 0 if verdict in ("frozen", "open") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
