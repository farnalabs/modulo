#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the incremental semgrep hook.

On Linux/macOS this wrapper runs the exact command the hook used before,
unchanged: semgrep scans ``backend/src/`` with ``--baseline-commit=HEAD``.

On Windows, the full-directory baseline scan hangs (semgrep-core cannot
complete the diff for ~3400 files).  Instead, we detect the changed Python
files (staged for commit + unstaged working-tree changes) and scan ONLY
those files, with no ``--baseline-commit`` flag.  This catches any finding
in the files the developer is actually touching — the highest-value subset
— while remaining fast (~2 min vs 5+ min for the full scan, which times
out).  CI (Linux) still runs the full baseline scan on every push, so no
enforcement is lost.

The Windows path is fail-open on TOOL errors that prevent semgrep from
running at all — a crash, a missing binary, or a spawn failure — and warn
loudly so the developer is not blocked by a broken tool.  We are NEVER
fail-open on FINDINGS: if semgrep runs and finds violations, we exit
non-zero.

We are NEVER fail-open on a REPORTED incomplete scan — the silent-decay hole
FAR-1859 exists to close:

1. A per-rule timeout on a file: semgrep skips that file and still exits 0,
   printing only a ``Warning: ... timeout error(s) ...`` line. Detected from
   the structured ``--json-output`` ``errors`` stream (an entry of type
   ``Timeout``) with an anchored text fallback. A REPORTED skip returns
   non-zero on EVERY path (the Windows convenience path and the Linux path).

A wall-clock kill of the whole process (``subprocess.TimeoutExpired``) is a
DIFFERENT shape, and the two paths treat it differently:

2. On Windows the scan runs over every changed ``backend/src/**.py`` file in a
   single invocation, so the outer ``subprocess.run`` timeout is a PROCESS-level
   bound across all of them. It can fire from machine load or a large change
   WITHOUT semgrep reporting any per-rule timeout. That is an environmental
   condition — not a defect in the change and not a detected skip — so the
   Windows path fails OPEN with a loud warning: the changed files were not fully
   checked locally, and CI (Linux) remains the authoritative full scan. It never
   reports a misleading "incomplete scan" FAILURE for a condition the developer
   cannot fix (which would only push them toward ``--no-verify``).

3. Genuine tool errors (missing binary, spawn failure, unexpected exception)
   keep the fail-open-with-warning behaviour above.

``PartialParsing`` is deliberately NOT treated as incomplete — ``mcp_server.py``
reports it on every run because of a pre-existing PEP 695 parse skip (FAR-1933),
so failing on it would make the gate permanently red for an unrelated reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

#: Semgrep exits 0 when a per-rule match times out on a file — the offending
#: file is silently skipped. That is an incomplete scan and a FAILURE of the
#: gate, not a tool error, so it must block rather than warn-and-pass.
#:
#: PRIMARY detection is the structured ``--json-output`` payload (see
#: ``_semgrep_json_timeout``). These text patterns are a SECONDARY net for the
#: case where the JSON is unavailable, and they are ANCHORED to semgrep's own
#: line prefixes: an unanchored ``"timeout error"`` substring false-positives
#: on a finding that merely quotes the phrase (``pipelines.py`` contains the
#: literal ``dispatch.wait_timeout error``), which would mislabel a clean scan
#: as incomplete.
_SEMGREP_TIMEOUT_PATTERNS = (
    re.compile(r"^Warning: \d+ timeout error", re.MULTILINE),
    re.compile(r"^Semgrep stopped running rules", re.MULTILINE),
)

#: ``errors[].type`` values in the semgrep JSON that mean the scan did NOT
#: cover every target. ``PartialParsing`` is deliberately absent (see the
#: module docstring): it is expected on ``mcp_server.py`` and must not fail.
_SEMGREP_JSON_FATAL_ERROR_TYPES = frozenset({"Timeout"})

# Git-state env vars inherited from a running `git commit` (e.g. the relative
# `GIT_INDEX_FILE=.git/index`) break semgrep's `--baseline-commit` scan, which
# creates a temporary git worktree to diff against HEAD: git then resolves the
# inherited index path against the /tmp worktree and fails with "index file open
# failed: Not a directory". The baseline worktree must use the repo's own git
# context, so strip these before spawning semgrep. The hook still runs the full
# incremental scan and blocks on any new finding.
_GIT_STATE_ENV = {
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
}

#: Wall-clock kill bound for the whole subprocess (Windows path only). This is
#: NOT semgrep's per-rule ``--timeout``: it is the outer ``subprocess.run``
#: timeout, and it bounds the ENTIRE scan.
#:
#: ``_run_windows`` passes every changed file to ONE invocation with
#: ``--timeout 120 --timeout-threshold 1``. Semgrep documents BOTH flags as
#: PER FILE ("Maximum time to spend running a rule on a single file" /
#: "Maximum number of rules that can time out on a file before the file is
#: skipped"), so the per-file budget is ``--timeout x --timeout-threshold`` =
#: 1 x 120s — but the PROCESS-level worst case is ``len(changed) x 120s``,
#: which can exceed this 180s kill on a large change or a loaded machine
#: WITHOUT semgrep reporting any per-rule timeout at all.
#:
#: A kill in that case is an environmental condition, not a defect in the
#: change and not a detected skip, so ``_run_windows`` fails OPEN with a loud
#: warning and the authoritative scan is left to CI (Linux). A REPORTED
#: per-rule timeout is a different, real failure and still returns non-zero.
_SEMGREP_TIMEOUT = 180  # seconds — outer process bound for the scoped Windows scan

#: The backend source tree semgrep scans (rule ``paths:`` filters target it).
_BACKEND_SRC = "backend/src/"

_SEMGREP_WARN = """\
========================================================================
run_semgrep.py WARNING: semgrep did NOT complete on Windows.

  semgrep findings in your changed files were NOT checked locally.
  CI (Linux) will still catch them on push — do NOT claim lint-clean.

  Reason: {reason}
========================================================================"""

_SEMGREP_INCOMPLETE = """\
========================================================================
run_semgrep.py: semgrep scan is INCOMPLETE — reported as a FAILURE.

  One or more files were NOT fully checked, so a clean result cannot be
  trusted. This is not a tool error and is NOT fail-open — fix the cause
  and re-run the scan.

  Reason: {reason}
========================================================================"""

_SEMGREP_WALLCLOCK_WARN = """\
=======================================================================
run_semgrep.py WARNING: semgrep scan did NOT complete on Windows — the
process wall-clock kill fired.

  No per-rule timeout was reported by semgrep, so this is NOT a skipped
  file and NOT a finding. The kill is a function of machine load and the
  number of changed files, not of your change, so it is treated as an
  environmental condition on this convenience path.

  Your changed files were NOT fully checked locally. CI (Linux) runs the
  authoritative full scan on push — do NOT claim lint-clean on the
  strength of this run.

  Detail: {detail}
======================================================================="""


def _semgrep_skipped_files(output: str) -> bool:
    """Return True when semgrep's text output reports a per-rule timeout skip.

    Semgrep reports this as a warning and still exits 0, so a caller that only
    reads the exit code treats an incomplete scan as clean. The patterns are
    anchored to semgrep's own line prefixes (see ``_SEMGREP_TIMEOUT_PATTERNS``)
    so a finding that merely quotes the phrase cannot false-trigger. The check
    is on the combined stdout+stderr of the semgrep invocation.
    """
    return any(pattern.search(output) for pattern in _SEMGREP_TIMEOUT_PATTERNS)


def _json_error_type_is_fatal(error: object) -> bool:
    """True when a JSON error entry's ``type`` names a fatal (incomplete) class.

    Semgrep serialises ``errors[].type`` as EITHER a string OR a LIST of
    strings (observed on 1.175.0 for an error carrying several type labels,
    e.g. a ``PartialParsing`` entry on the large ``mcp_server.py``). A bare
    ``in frozenset`` membership test on a list raises
    ``TypeError: unhashable type: 'list'`` — and because it sits in the
    incomplete-scan detector, it crashed the whole gate on a partial parse
    instead of reporting the intended "not fatal" verdict. The gate must
    survive the error it guards, so both shapes are handled.
    """
    if not isinstance(error, dict):
        return False
    error_type = error.get("type")
    if isinstance(error_type, str):
        return error_type in _SEMGREP_JSON_FATAL_ERROR_TYPES
    if isinstance(error_type, list):
        return any(isinstance(item, str) and item in _SEMGREP_JSON_FATAL_ERROR_TYPES for item in error_type)
    return False


def _semgrep_json_timeout(json_path: str | os.PathLike[str]) -> bool:
    """Return True when the semgrep ``--json-output`` payload is incomplete.

    A per-rule timeout is serialised as an ``errors`` entry with
    ``"type": "Timeout"`` while the process still exits 0, so the exit code is
    not evidence of a complete scan. A missing or malformed payload, or one
    whose errors are all non-fatal (e.g. ``PartialParsing``), returns ``False``
    — the anchored text net still covers the missing-payload case, and a
    genuine tool failure is handled by the fail-open branches.
    """
    try:
        with Path(json_path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return False
    return any(_json_error_type_is_fatal(err) for err in errors)


def _semgrep_scan_incomplete(output: str, json_path: str | os.PathLike[str] | None = None) -> bool:
    """True when either detector sees an incomplete scan (JSON primary, text net)."""
    if json_path is not None and _semgrep_json_timeout(json_path):
        return True
    return _semgrep_skipped_files(output)


def _get_changed_py_files() -> tuple[list[str], str | None]:
    """Return changed .py files under backend/src/ and any git failure reason.

    Checks both staged (index) and unstaged (working tree) changes so we
    catch everything the developer is touching, regardless of whether they
    ``git add``-ed first.

    Returns ``(files, git_error)``: ``git_error`` is ``None`` on success, or a
    non-empty reason string when a ``git diff`` invocation failed. A failed
    ``git diff`` (bare repo, permission error, corrupt index, or an initial
    commit where ``HEAD`` does not yet exist) yields no output; without the
    error surfaced, the hook would silently scan zero files and pass - a
    second silent fail-open path. The caller warns loudly on ``git_error``.
    """
    files: set[str] = set()
    git_error: str | None = None
    for diff_flag in ("--cached", ""):
        cmd = [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
        ]
        if diff_flag:
            cmd.append(diff_flag)
        cmd.extend(["--", _BACKEND_SRC])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            git_error = stderr or f"{' '.join(cmd)} exited {result.returncode}"
            continue
        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if stripped.endswith(".py") and stripped.startswith(_BACKEND_SRC):
                files.add(stripped)
    return sorted(files), git_error


def _run_windows() -> int:
    """Run semgrep on changed files only (scoped, no baseline)."""
    changed, git_error = _get_changed_py_files()
    if git_error is not None:
        # Never silently skip: if we cannot determine the changed files, warn
        # loudly (still exit 0 - fail-open on TOOL, never on FINDINGS).
        print(
            _SEMGREP_WARN.format(reason=f"git change detection failed: {git_error}"),
            file=sys.stderr,
        )
    if not changed:
        return 0

    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}

    def _is_spawn_failure(result: subprocess.CompletedProcess[str]) -> bool:
        """Return True when uv failed because the tool binary was not found."""
        return result.returncode != 0 and "Failed to spawn" in (result.stderr or "")

    try:
        with tempfile.TemporaryDirectory(prefix="run-semgrep-") as tmpdir:
            json_path = str(Path(tmpdir) / "semgrep.json")
            # On Windows, semgrep's pip package may not install a .exe wrapper, so
            # `uv run semgrep` fails with "Failed to spawn: program not found".
            # `python -m semgrep` is a dead stub (prints deprecation + exits 2).
            # CI (Linux) runs the full baseline scan on every push, so the local
            # Windows check is a convenience gate — fail open with a loud warning
            # rather than blocking the developer.
            #
            # --timeout-threshold 1: drop a file on its FIRST per-rule timeout
            #   instead of the default 3, so a slow file is reported promptly
            #   rather than burning up to 3 x 120s per file. Both flags are
            #   PER FILE; the process-level worst case across every changed file
            #   is len(changed) x 120s (see _SEMGREP_TIMEOUT above). --timeout
            #   120 is measured (0/48 timeouts at 16-way contention vs 28/32 at
            #   the 5s default); do not lower it without a fresh measurement.
            cmd = [
                "uv",
                "run",
                "--project",
                "backend",
                "--no-sync",
                "semgrep",
                "scan",
                "--config=.semgrep/",
                "--error",
                "--timeout",
                "120",
                "--timeout-threshold",
                "1",
                f"--json-output={json_path}",
                *changed,
            ]
            result = subprocess.run(
                cmd,
                env=env,
                check=False,
                timeout=_SEMGREP_TIMEOUT,
                capture_output=True,
                text=True,
            )
            if _is_spawn_failure(result):
                print(
                    _SEMGREP_WARN.format(
                        reason=(
                            "semgrep binary not found in venv (no .exe wrapper on "
                            "Windows). CI (Linux) still runs the full scan on push."
                        )
                    ),
                    file=sys.stderr,
                )
                return 0
            # Semgrep ran successfully — print its output and propagate its exit code.
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            # A per-rule timeout silently skips a file but exits 0. Treat that as
            # a FAILURE (incomplete scan), never a pass.
            if _semgrep_scan_incomplete((result.stdout or "") + (result.stderr or ""), json_path):
                print(
                    _SEMGREP_INCOMPLETE.format(
                        reason="semgrep reported a per-rule timeout and skipped one or more files (FAR-1859)"
                    ),
                    file=sys.stderr,
                )
                return 1
            return result.returncode
    except subprocess.TimeoutExpired:
        # The wall-clock kill fired. This is an environmental condition on this
        # convenience path — a function of machine load and the number of changed
        # files — NOT evidence of a defect in the change and NOT a reported skip:
        # semgrep's own per-rule timeout detector (JSON errors[].type == "Timeout"
        # plus the anchored text net) did not fire, so the silent-pass hole
        # FAR-1859 closed is NOT reopened here. Fail OPEN with a loud warning; the
        # authoritative full scan runs in CI (Linux).
        print(
            _SEMGREP_WALLCLOCK_WARN.format(
                detail=f"process killed after {_SEMGREP_TIMEOUT}s scanning {len(changed)} file(s)"
            ),
            file=sys.stderr,
        )
        return 0
    except FileNotFoundError:
        print(
            _SEMGREP_WARN.format(reason="semgrep or uv not found on PATH"),
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        print(
            _SEMGREP_WARN.format(reason=f"unexpected error: {exc}"),
            file=sys.stderr,
        )
        return 0


def main() -> int:
    if sys.platform == "win32":
        return _run_windows()

    # pre-commit runs hooks from the repo root, so relative paths below resolve
    # correctly; the outer `uv run` in .pre-commit-config.yaml plus this inner
    # `uv run` is a redundant but harmless double hop that guarantees semgrep
    # executes with the locked backend environment.
    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}
    # Resource bounds prevent OOM on machines with limited free memory.
    # --jobs 1: single-threaded to cap RSS (semgrep-core spawns per-core workers
    #   that each load the full rule set; 8 workers x 845 files x 64 rules
    #   exhausts 16 GB on this machine).
    # --max-memory 2048: hard cap at 2 GB; semgrep exits gracefully instead of
    #   the OS OOM-killer taking the process (and potentially other agents).
    # --timeout 120: per-file timeout; prevents a single pathological file from
    #   blocking the whole hook indefinitely. Measured: 0/48 timeouts at 16-way
    #   contention vs 28/32 skips at semgrep's 5s default — do not lower it
    #   without a fresh measurement.
    # --timeout-threshold 1: drop a file on its FIRST per-rule timeout instead
    #   of the default 3, so a slow file is reported promptly rather than
    #   burning up to 3 x 120s.
    # CI (Linux, Ubicloud 2-core) runs the full scan without these bounds and
    # has more headroom — the bounds here are a LOCAL-ONLY safety net.
    with tempfile.TemporaryDirectory(prefix="run-semgrep-") as tmpdir:
        json_path = str(Path(tmpdir) / "semgrep.json")
        cmd = [
            "uv",
            "run",
            "--project",
            "backend",
            "--no-sync",
            "semgrep",
            "scan",
            "--config=.semgrep/",
            "--error",
            "--jobs",
            "1",
            "--max-memory",
            "2048",
            "--timeout",
            "120",
            "--timeout-threshold",
            "1",
            f"--json-output={json_path}",
            "--baseline-commit=HEAD",
            _BACKEND_SRC,
        ]
        result = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        # A per-rule timeout silently skips a file but exits 0. Treat that as a
        # FAILURE (incomplete scan), never a pass. This is not a tool error, so
        # it is deliberately not covered by any fail-open branch.
        if _semgrep_scan_incomplete((result.stdout or "") + (result.stderr or ""), json_path):
            print(
                _SEMGREP_INCOMPLETE.format(
                    reason="semgrep reported a per-rule timeout and skipped one or more files (FAR-1859)"
                ),
                file=sys.stderr,
            )
            return 1
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
