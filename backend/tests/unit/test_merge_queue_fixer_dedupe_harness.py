"""Behavioural harness for the merge-queue Branch Fixer notification dedupe (FAR-2098).

The dedupe lives in one shell step of ``.github/workflows/merge-queue.yml``
(``Notify fixer of conflicts``). Structural assertions on that step are cheap to
write and impossible to trust: they pin the shape of the code, not what it does
when a PR carries a marker, when the comment read fails, or when the clock says
the cooldown has expired. So this module runs the REAL ``run:`` block.

How it works:

* the step's ``run`` block is read out of the workflow YAML (never copied — a
  copy would drift from the thing under test);
* the GitHub expression placeholders are substituted and the step's fixed
  ``/tmp/...`` data paths are redirected into a per-test sandbox, so the run is
  hermetic and safe to parallelise;
* ``gh`` is replaced by a stub on ``PATH`` that serves fixture comments and
  records every comment it is asked to post, and ``scripts/dispatch_with_retry.sh``
  is replaced by a stub that records the dispatch and its payload and can be
  told to fail;
* the fragment is executed with ``bash --noprofile --norc -eo pipefail``, i.e.
  the same semantics GitHub Actions gives a ``shell: bash`` step.

The scenarios cover the whole decision table: unchanged head inside the cooldown
(skip, notice only), changed head (dispatch), expired cooldown (dispatch),
lookup failure (dispatch + warning), no comments (dispatch), a malformed marker
(dispatch + warning), a marker on a later page (skip), every PR deduped (no
dispatch and no duplicate comment), and — the FAR-2092 contract — a failed
dispatch still recording its marker while a dispatch whose durable comment also
fails still fails the step.

The harness skips when the box cannot run it (no ``bash``, or a ``bash`` without
``jq``/``base64``/GNU ``date -d``). The skip is loud about why, and CI's Linux
runners satisfy every one of those.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "merge-queue.yml"
CONFLICT_STEP = "Notify fixer of conflicts"

PR = 4242
OTHER_PR = 4343
HEAD_A = "a" * 40
HEAD_B = "b" * 40
COOLDOWN_HOURS = 24

_MARKER_RE = re.compile(r"<!-- merge-queue-fixer-notified head=(?P<head>[0-9a-f]+) at=(?P<at>[^ ]+) -->")


# --- environment probe -------------------------------------------------------


def _bash_probe() -> tuple[str | None, str]:
    """``(bash path, "")`` when the harness can run, else ``(None, reason)``."""
    bash = shutil.which("bash")
    if bash is None:
        return None, "bash is not installed"
    probe = (
        "command -v jq >/dev/null 2>&1 "
        "&& command -v base64 >/dev/null 2>&1 "
        "&& command -v date >/dev/null 2>&1 "
        "&& date -u -d 2026-01-01T00:00:00Z +%s >/dev/null 2>&1"
    )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv; the interpreter is what shutil.which found
            [bash, "--noprofile", "--norc", "-c", probe],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment dependent
        return None, f"bash could not be executed: {exc}"
    if completed.returncode != 0:
        return None, "the available bash lacks jq, base64, or GNU `date -d`"
    return bash, ""


_BASH, _SKIP_REASON = _bash_probe()

pytestmark = pytest.mark.skipif(_BASH is None, reason=f"FAR-2098 harness unavailable: {_SKIP_REASON}")


# --- fixtures and helpers ----------------------------------------------------


def _iso(*, hours_ago: float) -> str:
    """A marker timestamp ``hours_ago`` before now (negative = in the future)."""
    stamp = datetime.now(UTC) - timedelta(hours=hours_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker(head: str, at: str) -> str:
    return f"<!-- merge-queue-fixer-notified head={head} at={at} -->"


def _run_block() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["queue"]["steps"]:
        if isinstance(step, dict) and step.get("name") == CONFLICT_STEP:
            run = step.get("run")
            assert isinstance(run, str), f"'{CONFLICT_STEP}' has no shell run block"
            return run
    raise AssertionError(f"merge-queue.yml has no '{CONFLICT_STEP}' step")


def _bash_path(path: Path) -> str:
    """The sandbox path as the bash interpreter sees it (WSL needs /mnt/...).

    The path is embedded in the probe script rather than passed as a positional
    argument: WSL's ``bash.exe`` drops arguments after ``-c <script>``.
    """
    if os.name != "nt":
        return path.as_posix()
    quoted = shlex.quote(str(path))
    probe = subprocess.run(  # noqa: S603 - fixed argv, a single shell-quoted path
        [_BASH or "bash", "--noprofile", "--norc", "-c", f"wslpath -a {quoted} 2>/dev/null || printf %s {quoted}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    resolved = (probe.stdout or "").strip()
    return resolved or path.as_posix()


_GH_STUB = """#!/usr/bin/env bash
# FAR-2098 harness stub: `gh` receives fixture comments and records comments.
set -u
WORK="__WORK__"
printf '%s\\n' "$*" >> "$WORK/gh_calls.log"
case "${1:-}" in
  api)
    url="${2:-}"
    num=$(printf '%s' "$url" | sed -n 's#.*/issues/\\([0-9][0-9]*\\)/comments.*#\\1#p')
    page=$(printf '%s' "$url" | sed -n 's#.*[?&]page=\\([0-9][0-9]*\\).*#\\1#p')
    if [ -z "$num" ]; then
      echo "stub gh: unrecognised api url: $url" >&2
      exit 1
    fi
    if [ -z "$page" ]; then page=1; fi
    case ",${GH_API_FAIL_FOR:-}," in
      *",${num},"*)
        echo "stub gh: simulated GitHub API failure for PR $num" >&2
        exit 1
        ;;
    esac
    if [ -f "$WORK/comments_${num}_page${page}.json" ]; then
      cat "$WORK/comments_${num}_page${page}.json"
    elif [ -f "$WORK/comments_${num}.json" ]; then
      cat "$WORK/comments_${num}.json"
    else
      printf '[]\\n'
    fi
    ;;
  pr)
    if [ "${2:-}" != "comment" ]; then
      echo "stub gh: unsupported pr subcommand: ${2:-}" >&2
      exit 1
    fi
    num="${3:-}"
    body=""
    if [ "$#" -ge 3 ]; then shift 3; fi
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --body)
          body="${2:-}"
          shift 2
          ;;
        *) shift ;;
      esac
    done
    case ",${GH_COMMENT_FAIL_FOR:-}," in
      *",${num},"*)
        echo "stub gh: simulated comment-post failure for PR $num" >&2
        exit 1
        ;;
    esac
    printf '===COMMENT===\\n%s\\n' "$body" >> "$WORK/posted_${num}.log"
    echo "https://example.invalid/comment/${num}"
    ;;
  *)
    echo "stub gh: unsupported invocation: $*" >&2
    exit 1
    ;;
esac
"""

_DISPATCH_STUB = """#!/usr/bin/env bash
# FAR-2098 harness stub for scripts/dispatch_with_retry.sh.
set -u
WORK="__WORK__"
printf '%s\\n' "dispatch" >> "$WORK/dispatch_calls.log"
printf '%s' "${DISPATCH_BODY:-}" > "$WORK/dispatch_body.json"
rc="${STUB_DISPATCH_RC:-0}"
delivered="true"
if [ "$rc" -ne 0 ]; then delivered="false"; fi
{
  printf 'DISPATCH_LAST_HTTP=%s\\n' "${STUB_DISPATCH_HTTP:-200}"
  printf 'DISPATCH_LAST_DETAIL=%s\\n' "${STUB_DISPATCH_DETAIL:-ok}"
  printf 'DISPATCH_LAST_CLASS=%s\\n' "stub"
  printf 'DISPATCH_ATTEMPTS=%s\\n' "1"
  printf 'DISPATCH_DELIVERED=%s\\n' "$delivered"
} > "${DISPATCH_RESULT_FILE:-$WORK/dispatch_result.env}"
exit "$rc"
"""


class Harness:
    """A sandboxed run of the real ``Notify fixer of conflicts`` step body."""

    def __init__(self, work: Path, bash: str) -> None:
        self.work = work
        self.bash = bash
        self.work.mkdir(parents=True, exist_ok=True)
        self.posix_work = _bash_path(work)
        self._write_executable(work / "bin" / "gh", _GH_STUB)
        self._write_executable(work / "scripts" / "dispatch_with_retry.sh", _DISPATCH_STUB)

    def _write_executable(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.replace("__WORK__", self.posix_work), encoding="utf-8", newline="\n")
        path.chmod(0o755)

    # -- scenario setup --

    def set_conflicts(self, heads: dict[int, str]) -> None:
        """The conflict list plus the head SHAs the merge step recorded."""
        numbers = " ".join(str(pr) for pr in heads)
        (self.work / "conflict_prs.txt").write_text(numbers + "\n", encoding="utf-8", newline="\n")
        lines = [f"{pr} {sha}" for pr, sha in heads.items()]
        (self.work / "conflict_pr_heads.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def set_comments(self, pr: int, bodies: list[str]) -> None:
        """Comments as the API returns them with ``direction=desc`` (newest first)."""
        payload = [{"body": body} for body in bodies]
        path = self.work / f"comments_{pr}.json"
        path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    def set_paged_comments(self, pr: int, pages: dict[int, list[str]]) -> None:
        for page, bodies in pages.items():
            payload = [{"body": body} for body in bodies]
            path = self.work / f"comments_{pr}_page{page}.json"
            path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    # -- execution --

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        fragment = _run_block()
        fragment = re.sub(r"\$\{\{\s*github\.server_url\s*\}\}", "https://github.example", fragment)
        fragment = re.sub(r"\$\{\{\s*github\.repository\s*\}\}", "farnalabs/modulo", fragment)
        fragment = re.sub(r"\$\{\{\s*github\.run_id\s*\}\}", "987654321", fragment)
        assert "${{" not in fragment, "an unsubstituted GitHub expression survived into the harness fragment"
        # Hermetic run: the step's fixed /tmp data paths move into the sandbox.
        fragment = fragment.replace("/tmp/", f"{self.posix_work}/")
        (self.work / "fragment.sh").write_text(fragment, encoding="utf-8", newline="\n")

        # The step's `env:` block is normally supplied by Actions, so the harness
        # supplies it. It is written into the runner rather than passed through
        # `subprocess.run(env=...)` because a Windows env var does not reach a WSL
        # bash at all (only WSLENV-listed names cross the interop boundary).
        step_env = {
            "GH_TOKEN": "stub-token",
            "GITHUB_REPOSITORY": "farnalabs/modulo",
            "DISPATCH_URL": "https://dispatch.example.invalid/hook",
            "DISPATCH_SECRET": "stub-secret",
            "DISPATCH_LABEL": "Modulo Branch Fixer",
            "DISPATCH_BUDGET_SECONDS": "5",
        }
        step_env.update(extra_env)
        exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in step_env.items())
        runner = (
            "#!/bin/bash\n"
            f'export PATH="{self.posix_work}/bin:$PATH"\n'
            f"{exports}"
            f"cd {shlex.quote(self.posix_work)} || exit 97\n"
            "bash --noprofile --norc -eo pipefail fragment.sh\n"
        )
        (self.work / "runner.sh").write_text(runner, encoding="utf-8", newline="\n")

        return subprocess.run(  # noqa: S603 - fixed argv; the sandbox script this harness wrote
            [self.bash, "--noprofile", "--norc", f"{self.posix_work}/runner.sh"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    # -- observations --

    @property
    def dispatch_invoked(self) -> bool:
        return (self.work / "dispatch_calls.log").exists()

    @property
    def dispatch_body(self) -> dict[str, Any]:
        raw = (self.work / "dispatch_body.json").read_text(encoding="utf-8")
        return json.loads(raw)

    def comments_posted(self, pr: int) -> list[str]:
        path = self.work / f"posted_{pr}.log"
        if not path.exists():
            return []
        return [chunk.strip("\n") for chunk in path.read_text(encoding="utf-8").split("===COMMENT===") if chunk.strip()]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    assert _BASH is not None  # guaranteed by the module-level skip
    return Harness(tmp_path / "far2098", _BASH)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


# --- scenarios ---------------------------------------------------------------


def test_unchanged_head_inside_the_cooldown_skips_the_dispatch(harness: Harness) -> None:
    """The core of FAR-2098: a re-run for the same head must not dispatch."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=1))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert not harness.dispatch_invoked, "an unchanged head inside the cooldown re-dispatched the fixer"
    assert not harness.comments_posted(PR), "a skipped dispatch still posted a comment on the PR"
    output = _output(result)
    assert "::notice::" in output
    assert f"PR #{PR}" in output
    assert HEAD_A in output
    assert "The cooldown expires at " in output


def test_changed_head_always_dispatches(harness: Harness) -> None:
    """A new commit is a new conflict state: notify immediately."""
    harness.set_conflicts({PR: HEAD_B})
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=1))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "a changed head did not dispatch"
    posted = harness.comments_posted(PR)
    assert len(posted) == 1, f"expected exactly one notification comment, got {len(posted)}"
    assert f"head={HEAD_B}" in posted[0]
    assert str(PR) in harness.dispatch_body["conflictPrs"]


def test_expired_cooldown_dispatches(harness: Harness) -> None:
    """The cooldown is the recovery path for a notification that went nowhere."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=COOLDOWN_HOURS + 1))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "an expired cooldown suppressed the dispatch"


def test_comment_lookup_failure_dispatches_and_warns(harness: Harness) -> None:
    """A failed `gh api` must never suppress a needed notification."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=1))])

    result = harness.run(GH_API_FAIL_FOR=str(PR))

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "a failed comment lookup suppressed the dispatch (dedupe failed CLOSED)"
    output = _output(result)
    assert "::warning::" in output
    assert "could not read the PR's comments" in output


def test_no_comments_dispatches(harness: Harness) -> None:
    """A PR that has never been commented on is the first-notification case."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "a PR with no comments was not notified"


def test_malformed_marker_dispatches_and_warns(harness: Harness) -> None:
    """Marker text that does not parse exactly is untrusted: dispatch."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, ["merge-queue-fixer-notified but with no head or at fields"])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "a malformed marker suppressed the dispatch"
    output = _output(result)
    assert "::warning::" in output
    assert "malformed" in output


def test_future_dated_marker_is_ignored(harness: Harness) -> None:
    """A future `at` would otherwise suppress indefinitely - reject it."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=-48))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "a future-dated marker suppressed the dispatch"
    assert "in the future" in _output(result)


def test_unparseable_marker_timestamp_dispatches_and_warns(harness: Harness) -> None:
    """A marker whose `at` has the right shape but is not a real date is ignored."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [_marker(HEAD_A, "2026-13-45T99:99:99Z")])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "an unparseable marker timestamp suppressed the dispatch"
    output = _output(result)
    assert "::warning::" in output
    assert "could not be parsed" in output


def test_marker_on_a_later_page_is_found(harness: Harness) -> None:
    """The read is bounded but paged: a full first page must not hide the marker."""
    harness.set_conflicts({PR: HEAD_A})
    filler = [{"body": f"unrelated comment {index}"} for index in range(100)]
    harness.set_paged_comments(
        PR,
        {1: [entry["body"] for entry in filler], 2: [_marker(HEAD_A, _iso(hours_ago=2))]},
    )

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert not harness.dispatch_invoked, "the marker on page 2 was not found, so the dispatch was repeated"
    calls = (harness.work / "gh_calls.log").read_text(encoding="utf-8")
    assert "page=1" in calls
    assert "page=2" in calls


def test_every_conflicted_pr_deduped_means_no_dispatch_and_no_comment(harness: Harness) -> None:
    """When nothing changed, neither the webhook nor a PR comment is emitted."""
    harness.set_conflicts({PR: HEAD_A, OTHER_PR: HEAD_B})
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=3))])
    harness.set_comments(OTHER_PR, [_marker(HEAD_B, _iso(hours_ago=4))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert not harness.dispatch_invoked, "a fully deduped pass still dispatched"
    assert not harness.comments_posted(PR)
    assert not harness.comments_posted(OTHER_PR)
    assert "no Branch Fixer dispatch and no new PR comment this pass" in _output(result)


def test_only_the_changed_pr_is_dispatched(harness: Harness) -> None:
    """Dedupe narrows the payload: the unchanged PR is not re-sent either."""
    harness.set_conflicts({PR: HEAD_A, OTHER_PR: HEAD_B})
    harness.set_comments(PR, [_marker(HEAD_B, _iso(hours_ago=3))])
    harness.set_comments(OTHER_PR, [_marker(HEAD_B, _iso(hours_ago=3))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked
    sent = harness.dispatch_body["conflictPrs"]
    assert str(PR) in sent
    assert str(OTHER_PR) not in sent, "the payload still names the PR that was deduped"


def test_missing_recorded_head_dispatches_and_warns(harness: Harness) -> None:
    """No head SHA recorded means the dedupe cannot prove anything: dispatch."""
    harness.set_conflicts({PR: HEAD_A})
    (harness.work / "conflict_pr_heads.txt").write_text("", encoding="utf-8", newline="\n")
    harness.set_comments(PR, [_marker(HEAD_A, _iso(hours_ago=1))])

    result = harness.run()

    assert result.returncode == 0, _output(result)
    assert harness.dispatch_invoked, "a missing head SHA suppressed the dispatch"
    assert "no head SHA was recorded" in _output(result)


def test_undelivered_notification_records_its_marker_and_lets_the_pass_continue(harness: Harness) -> None:
    """FAR-2092 is unchanged, and the FAR-1939 comment now carries the marker."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [])

    result = harness.run(
        STUB_DISPATCH_RC="1",
        STUB_DISPATCH_HTTP="429",
        STUB_DISPATCH_DETAIL="Concurrent run limit of 8 reached",
    )

    assert result.returncode == 0, _output(result)
    posted = harness.comments_posted(PR)
    assert len(posted) == 1
    assert "could NOT deliver" in posted[0]
    marker = _MARKER_RE.search(posted[0])
    assert marker is not None, "the durable failure comment carries no dedupe marker"
    assert marker.group("head") == HEAD_A


def test_undelivered_notification_without_a_durable_comment_still_fails_hard(harness: Harness) -> None:
    """FAR-2092's hard failure survives: no durable signal at all is an error."""
    harness.set_conflicts({PR: HEAD_A})
    harness.set_comments(PR, [])

    result = harness.run(STUB_DISPATCH_RC="1", GH_COMMENT_FAIL_FOR=str(PR))

    assert result.returncode == 1, _output(result)
    assert "NO durable signal" in _output(result)
