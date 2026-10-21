"""Behavioural tests for the CI -> Branch Fixer webhook dispatch retry policy.

The dispatch lives inline in ``.github/workflows/ci.yml`` (job
``notify-pr-failure``, step ``Trigger Modulo Branch Fixer``) and no other suite
executes it, so a retry-policy regression is invisible until a real CI failure
needs the autonomous fix loop - exactly when it is least affordable. These tests
run the step's REAL ``run:`` block under ``bash -e`` with stubbed ``curl``,
``gh`` and ``sleep``, so they assert the shipped shell behaviour rather than a
re-implementation of it.

The defect they pin (observed 2026-10-20): a saturated Branch Fixer trigger
answers HTTP 429 ("Concurrent run limit of 8 reached"); the old policy gave up
after three attempts ~40s apart and treated the capacity refusal as a terminal
dispatch failure, so a red CI run never reached the fixer.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

#: Markers around the retry policy shared by both ci.yml notify steps.
POLICY_START = "# ==== BEGIN Branch Fixer dispatch retry policy ===="
POLICY_END = "# ==== END Branch Fixer dispatch retry policy ===="

#: The step that owns the PR-failure dispatch (its body is executed below).
PR_JOB = "notify-pr-failure"
DISPATCH_STEP_NAME = "Trigger Modulo Branch Fixer"

#: GitHub expressions the step interpolates, resolved as GitHub would resolve
#: them. An unknown expression is a deliberate failure: the test must not
#: silently run a half-rendered script when the workflow changes shape.
_EXPRESSION_VALUES = {
    "github.server_url": "https://github.com",
    "github.repository": "farnalabs/modulo",
    "github.run_id": "123456789",
    "github.sha": "deadbeefcafe",
    "github.event.pull_request.number": "42",
    "github.event.pull_request.head.ref": "fix/test-branch",
    "github.event.pull_request.head.sha": "deadbeefcafe",
    "secrets.MODULO_REVIEWBOT_TOKEN || github.token": "ghs_test_token",
    "secrets.MODULO_BRANCH_FIXER_WEBHOOK_URL": "https://app.modulo.run/api/webhooks/branch-fixer",
    "secrets.MODULO_BRANCH_FIXER_HMAC_SECRET": "test-hmac-secret",
}

_EXPRESSION = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")

CAPACITY_DETAIL = "Concurrent run limit of 8 reached"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".github" / "workflows" / "ci.yml"
        if candidate.exists():
            return parent
    raise RuntimeError("Could not locate .github/workflows/ci.yml from the test path")


def _render(value: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        expression = match.group(1)
        if expression not in _EXPRESSION_VALUES:
            raise AssertionError(
                f"Unmapped GitHub expression {expression!r} in ci.yml — add it to "
                "_EXPRESSION_VALUES so the dispatch step stays executable under test"
            )
        return _EXPRESSION_VALUES[expression]

    return _EXPRESSION.sub(substitute, value)


def _dispatch_step(job: str = PR_JOB) -> dict:
    workflow = yaml.safe_load((_repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"][job]["steps"]
    for step in steps:
        if step.get("name") == DISPATCH_STEP_NAME:
            return step
    raise AssertionError(f"ci.yml job {job!r} has no step named {DISPATCH_STEP_NAME!r}")


class Response(NamedTuple):
    """One stubbed webhook response."""

    code: str
    body: str = ""
    retry_after: str | None = None


class DispatchRun(NamedTuple):
    """The observed result of executing the real dispatch step."""

    exit_code: int
    output: str
    sleeps: list[str]
    curl_calls: int
    gh_calls: list[str]
    pr_comment_body: str
    step_summary: str


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


_CURL_STUB = """#!/usr/bin/env bash
set -u
headers_file=""
body_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -D) headers_file="$2"; shift 2; continue ;;
    -o) body_file="$2"; shift 2; continue ;;
    -w|-H|-d|-X|--connect-timeout|--max-time) shift 2; continue ;;
    *) shift ;;
  esac
done
count_file="${STUB_DIR}/curl_count"
count=$(cat "$count_file" 2>/dev/null || echo 0)
count=$((count + 1))
printf '%s' "$count" > "$count_file"
code=$(cat "${STUB_DIR}/code.${count}" 2>/dev/null || echo 000)
body=$(cat "${STUB_DIR}/body.${count}" 2>/dev/null || true)
header=$(cat "${STUB_DIR}/header.${count}" 2>/dev/null || true)
if [ -n "$headers_file" ]; then printf '%s' "$header" > "$headers_file"; fi
if [ -n "$body_file" ]; then printf '%s' "$body" > "$body_file"; fi
printf '%s' "$code"
exit 0
"""

_GH_STUB = """#!/usr/bin/env bash
set -u
{
  for arg in "$@"; do printf '%s\\n' "$arg"; done
  printf '%s\\n' "====="
} >> "${STUB_DIR}/gh_args.log"
if [ -n "${STUB_GH_FAIL:-}" ]; then exit 1; fi
exit 0
"""

_SLEEP_STUB = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$1" >> "${STUB_DIR}/sleeps.log"
exit 0
"""


def _run_dispatch(
    tmp_path: Path,
    responses: list[Response],
    env_overrides: dict[str, str] | None = None,
    gh_fails: bool = False,
    step_summary: bool = True,
    job: str = PR_JOB,
) -> DispatchRun:
    """Execute the real dispatch step against a scripted webhook."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    _write_stub(bin_dir, "curl", _CURL_STUB)
    _write_stub(bin_dir, "gh", _GH_STUB)
    _write_stub(bin_dir, "sleep", _SLEEP_STUB)

    for index, response in enumerate(responses, start=1):
        (stub_dir / f"code.{index}").write_text(response.code, encoding="utf-8")
        (stub_dir / f"body.{index}").write_text(response.body, encoding="utf-8")
        header = ""
        if response.retry_after is not None:
            header = f"HTTP/2 {response.code}\r\nRetry-After: {response.retry_after}\r\n"
        (stub_dir / f"header.{index}").write_text(header, encoding="utf-8")

    step = _dispatch_step(job)
    script = tmp_path / "dispatch.sh"
    script.write_text(_render(step["run"]), encoding="utf-8")

    env = dict(os.environ)
    env.update({key: _render(str(value)) for key, value in (step.get("env") or {}).items()})
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["STUB_DIR"] = str(stub_dir)
    env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary.md") if step_summary else ""
    if not step_summary:
        env.pop("GITHUB_STEP_SUMMARY", None)
    if gh_fails:
        env["STUB_GH_FAIL"] = "1"
    env.update(env_overrides or {})

    bash = shutil.which("bash") or "/bin/bash"
    completed = subprocess.run(  # noqa: S603 — fixed argv: an interpreter and a test-written script path
        [bash, "-e", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    sleeps_path = stub_dir / "sleeps.log"
    gh_args = stub_dir / "gh_args.log"
    summary_path = tmp_path / "summary.md"
    return DispatchRun(
        exit_code=completed.returncode,
        output=completed.stdout + completed.stderr,
        sleeps=sleeps_path.read_text(encoding="utf-8").split() if sleeps_path.exists() else [],
        curl_calls=int((stub_dir / "curl_count").read_text(encoding="utf-8") or 0)
        if (stub_dir / "curl_count").exists()
        else 0,
        gh_calls=gh_args.read_text(encoding="utf-8").split("\n=====\n") if gh_args.exists() else [],
        pr_comment_body=_pr_comment_body(gh_args) if gh_args.exists() else "",
        step_summary=summary_path.read_text(encoding="utf-8") if summary_path.exists() else "",
    )


def _pr_comment_body(gh_args_path: Path) -> str:
    """The ``--body`` value of the PR comment the step posted, if any."""
    sections = gh_args_path.read_text(encoding="utf-8").split("\n=====\n")
    for section in sections:
        args = section.splitlines()
        if len(args) >= 2 and args[0] == "pr" and args[1] == "comment" and "--body" in args:
            return args[args.index("--body") + 1]
    return ""


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------


def test_429_capacity_is_retryable_and_honours_retry_after(tmp_path: Path) -> None:
    """A saturated trigger is retired, not terminal, and the server's delay is used."""
    run = _run_dispatch(
        tmp_path,
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}', retry_after="7"), Response("202", '{"run_id": "abc"}')],
    )

    assert run.exit_code == 0, run.output
    assert run.curl_calls == 2, f"429 must be retried exactly once here, saw {run.curl_calls} attempts\n{run.output}"
    assert run.sleeps == ["7"], f"Retry-After must be honoured verbatim, slept {run.sleeps!r}"
    assert "capacity" in run.output, "the retry warning must name the capacity class"
    assert "accepted" in run.output


def test_429_without_retry_after_uses_exponential_backoff(tmp_path: Path) -> None:
    """Without a server instruction the delay doubles per attempt."""
    run = _run_dispatch(
        tmp_path,
        [
            Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}'),
            Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}'),
            Response("202", '{"run_id": "abc"}'),
        ],
        {"DISPATCH_JITTER_MAX": "0", "DISPATCH_BASE_DELAY": "10"},
    )

    assert run.exit_code == 0, run.output
    assert run.sleeps == ["10", "20"], f"backoff must double from the base delay, slept {run.sleeps!r}"


def test_jitter_keeps_each_backoff_within_the_banded_delay(tmp_path: Path) -> None:
    """Jitter desynchronises retries without unbounded growth."""
    run = _run_dispatch(
        tmp_path,
        [Response("429", "{}"), Response("202", '{"run_id": "abc"}')],
        {"DISPATCH_JITTER_MAX": "5", "DISPATCH_BASE_DELAY": "10"},
    )

    assert run.exit_code == 0, run.output
    assert len(run.sleeps) == 1, run.sleeps
    assert 10 <= int(run.sleeps[0]) <= 15, f"first delay must sit in [10, 15], slept {run.sleeps!r}"


def test_transient_gateway_errors_are_retried(tmp_path: Path) -> None:
    """502/503/504/000 are the same 'not reachable right now' class as 429."""
    run = _run_dispatch(
        tmp_path,
        [Response("503", '{"detail": "shedding load"}'), Response("502", ""), Response("202", '{"run_id": "abc"}')],
        {"DISPATCH_JITTER_MAX": "0", "DISPATCH_BASE_DELAY": "1"},
    )

    assert run.exit_code == 0, run.output
    assert run.curl_calls == 3
    assert "transient" in run.output


def test_terminal_4xx_is_not_retried(tmp_path: Path) -> None:
    """A credential/payload rejection must fail immediately, never burn the budget."""
    run = _run_dispatch(tmp_path, [Response("401", '{"detail": "invalid signature"}')])

    assert run.exit_code == 1, run.output
    assert run.curl_calls == 1, f"a terminal error must not be retried, saw {run.curl_calls} attempts"
    assert not run.sleeps, f"a terminal error must not sleep, slept {run.sleeps!r}"
    assert "non-retryable" in run.output


def test_a_bare_200_is_terminal_not_accepted(tmp_path: Path) -> None:
    """Only 202 is the webhook contract; a stray 200 must not count as delivered."""
    run = _run_dispatch(tmp_path, [Response("200", '{"detail": "not the webhook"}')])

    assert run.exit_code == 1, run.output
    assert run.curl_calls == 1, f"a non-202 success must not be retried, saw {run.curl_calls} attempts"
    assert "non-retryable" in run.output


def test_retry_after_beyond_the_budget_fails_without_waiting(tmp_path: Path) -> None:
    """Never retry earlier than the server asked; give up instead."""
    run = _run_dispatch(
        tmp_path,
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}', retry_after="300")],
        {"DISPATCH_BUDGET_SECONDS": "60"},
    )

    assert run.exit_code == 1, run.output
    assert run.curl_calls == 1, f"must not retry inside a Retry-After window it cannot honour ({run.curl_calls})"
    assert not run.sleeps, f"must not sleep a delay the budget cannot absorb, slept {run.sleeps!r}"
    assert "capacity saturated" in run.output


# ---------------------------------------------------------------------------
# Handled no-ops
# ---------------------------------------------------------------------------


def test_coalesced_202_is_success(tmp_path: Path) -> None:
    run = _run_dispatch(tmp_path, [Response("202", '{"status": "coalesced"}')])

    assert run.exit_code == 0, run.output
    assert "coalesced" in run.output
    assert run.curl_calls == 1


def test_duplicate_webhook_payload_is_a_handled_noop(tmp_path: Path) -> None:
    run = _run_dispatch(tmp_path, [Response("400", '{"detail": "Duplicate webhook payload"}')])

    assert run.exit_code == 0, run.output
    assert run.curl_calls == 1, "a duplicate delivery must not be retried"
    assert "duplicate delivery suppressed" in run.output


# ---------------------------------------------------------------------------
# Durable, loud failure signal when the budget is exhausted
# ---------------------------------------------------------------------------


def test_capacity_exhaustion_leaves_a_durable_signal(tmp_path: Path) -> None:
    """Budget exhausted on saturation: exit non-zero AND leave a durable trace.

    This is the 2026-10-20 regression: the old policy exited 1 after three
    attempts without distinguishing capacity from a broken webhook, and the
    only trace was a PR comment.
    """
    run = _run_dispatch(
        tmp_path,
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}', retry_after="30")],
        {"DISPATCH_BUDGET_SECONDS": "10", "DISPATCH_MAX_ATTEMPTS": "4"},
    )

    assert run.exit_code == 1, f"an undelivered dispatch must still fail the step\n{run.output}"
    assert run.curl_calls == 1, f"a 30s Retry-After exceeds a 10s budget — no retry ({run.curl_calls})"
    assert "capacity saturated" in run.output, run.output
    assert "not handed to the autonomous fix loop" in run.output

    assert run.step_summary, "the run summary must record that the fixer was never dispatched"
    assert "capacity saturated" in run.step_summary
    assert "max_concurrent_runs" in run.step_summary, "the summary must name the capacity remedy"

    assert run.pr_comment_body, "the PR must carry the durable dispatch-failure comment"
    assert "capacity saturated" in run.pr_comment_body
    assert "42" in run.pr_comment_body or "Branch Fixer" in run.pr_comment_body


def test_capacity_and_transient_failures_are_distinguishable(tmp_path: Path) -> None:
    """Capacity saturation and a broken webhook must not read the same."""
    capacity = _run_dispatch(
        tmp_path / "capacity",
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}')],
        {"DISPATCH_BUDGET_SECONDS": "0"},
    )
    unreachable = _run_dispatch(
        tmp_path / "unreachable",
        [Response("000", "")],
        {"DISPATCH_BUDGET_SECONDS": "0"},
    )

    assert capacity.exit_code == 1, capacity.output
    assert unreachable.exit_code == 1, unreachable.output
    assert "capacity saturated" in capacity.output
    assert "webhook unreachable" in unreachable.output
    assert "capacity saturated" in capacity.pr_comment_body
    assert "webhook unreachable" in unreachable.pr_comment_body
    assert capacity.pr_comment_body != unreachable.pr_comment_body


def test_undeliverable_failure_comment_exits_2(tmp_path: Path) -> None:
    """A dispatch failure the PR cannot be told about is a HARD error."""
    run = _run_dispatch(
        tmp_path,
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}')],
        {"DISPATCH_BUDGET_SECONDS": "0"},
        gh_fails=True,
    )

    assert run.exit_code == 2, run.output
    assert "could not be announced" in run.output


def test_failure_without_a_step_summary_target_does_not_crash(tmp_path: Path) -> None:
    """GITHUB_STEP_SUMMARY is unset outside Actions; the step must still fail cleanly."""
    run = _run_dispatch(
        tmp_path,
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}')],
        {"DISPATCH_BUDGET_SECONDS": "0"},
        step_summary=False,
    )

    assert run.exit_code == 1, run.output
    assert "capacity saturated" in run.output


# ---------------------------------------------------------------------------
# Retry-After parsing (RFC 9110 permits an HTTP-date as well as delta-seconds)
# ---------------------------------------------------------------------------


def test_http_date_retry_after_is_parsed(tmp_path: Path) -> None:
    """An HTTP-date Retry-After resolves to a positive delay, not 'unparseable'."""
    import email.utils
    import time

    target = time.time() + 45
    http_date = email.utils.formatdate(target, usegmt=True)

    run = _run_dispatch(
        tmp_path,
        [
            Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}', retry_after=http_date),
            Response("202", '{"run_id": "a"}'),
        ],
        {"DISPATCH_BUDGET_SECONDS": "300"},
    )

    assert run.exit_code == 0, run.output
    assert len(run.sleeps) == 1, run.sleeps
    # Bounds are wide on purpose: the parse runs between two `date` calls, and
    # an off-by-a-few-seconds HTTP-date parse is still correct behaviour.
    assert 35 <= int(run.sleeps[0]) <= 50, f"HTTP-date Retry-After parsed to {run.sleeps!r}"


@pytest.mark.parametrize("raw", ["", "not-a-date", "-5"])
def test_unusable_retry_after_falls_back_to_backoff(tmp_path: Path, raw: str) -> None:
    """A malformed/negative Retry-After must not be used verbatim."""
    run = _run_dispatch(
        tmp_path,
        [Response("429", f'{{"detail": "{CAPACITY_DETAIL}"}}', retry_after=raw), Response("202", '{"run_id": "a"}')],
        {"DISPATCH_JITTER_MAX": "0", "DISPATCH_BASE_DELAY": "4"},
    )

    assert run.exit_code == 0, run.output
    assert run.sleeps == ["4"], f"{raw!r} must fall back to the computed backoff, slept {run.sleeps!r}"
