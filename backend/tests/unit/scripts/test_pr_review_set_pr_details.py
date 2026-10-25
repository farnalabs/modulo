"""Behavioural tests for ``pr-review.yml``'s ``Set PR details`` step (FAR-2111).

``Set PR details`` is the single place that decides whether a review is
dispatched at all: it resolves the PR for whichever event fired and applies the
gates (draft, ``manual-delivery`` label) before every later step can act. Until
FAR-2111 it was only ever reached by a ``workflow_run`` event, so the branch that
handles a pull-request event — and every gate on it — was dead code that nobody
executed.

FAR-2111 added a ``pull_request_target`` trigger, which makes that branch live
and makes the gates load-bearing on it: a ``manual-delivery`` PR must not be
auto-reviewed, and a draft must not be reviewed early. These tests run the
step's REAL ``run:`` block under ``bash -eo pipefail`` with a stubbed ``gh``, so
a gate that is bypassed, duplicated or wedged inside one event branch fails
here rather than in production.

The structural companion
(``tests/architecture/test_workflow_structure.py``) pins the trigger and the
shape of the gates; this module pins their behaviour.
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

#: This module executes the step's real ``run:`` block under a POSIX shell with a
#: stubbed ``gh`` placed first on PATH — it needs executable-bit semantics and a
#: PATH the shell can read. The platform gate lives in ``backend/tests/conftest.py``
#: (``POSIX_ONLY_MARKER``); Linux CI executes this suite for real.
pytestmark = pytest.mark.posix_only

PR_REVIEW_WORKFLOW = Path(".github") / "workflows" / "pr-review.yml"
JOB_NAME = "Trigger Modulo PR Review"
STEP_NAME = "Set PR details"

_EXPRESSION = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")

#: Every GitHub expression this step interpolates, with the value used when a
#: case does not override it. An unmapped expression is a deliberate failure: the
#: test must never silently run a half-rendered script when the step changes.
_DEFAULT_EXPRESSIONS = {
    "github.token": "ghs_test_token",
    "github.repository": "farnalabs/modulo",
    "github.event_name": "workflow_dispatch",
    "github.event.pull_request.number": "",
    "github.event.pull_request.head.sha": "",
    "github.event.pull_request.head.ref": "",
    "github.event.pull_request.draft": "",
    "inputs.pr_number": "",
    "inputs.head_sha": "",
    "inputs.head_branch": "",
    "github.event.workflow_run.head_sha": "",
    "github.event.workflow_run.head_branch": "",
}

#: The stub answers the step's ``gh api`` reads from its environment, keyed by the
#: ``--jq`` program in the call, so one stub serves every API surface the step
#: uses (PR lookup by commit, current head, head ref, labels).
_GH_STUB = """#!/usr/bin/env bash
set -u
{
  for arg in "$@"; do printf '%s\\n' "$arg"; done
  printf '%s\\n' "====="
} >> "${STUB_DIR}/gh_args.log"
joined=" $* "
if printf '%s' "$joined" | grep -q 'labels\\[\\]\\.name'; then printf '%s' "${STUB_LABELS:-}"; exit 0; fi
if printf '%s' "$joined" | grep -q '/commits/.*/pulls'; then printf '%s' "${STUB_PR_FOR_SHA:-}"; exit 0; fi
if printf '%s' "$joined" | grep -q '\\.head\\.sha'; then printf '%s' "${STUB_CURRENT_SHA:-}"; exit 0; fi
if printf '%s' "$joined" | grep -q '\\.head\\.ref'; then printf '%s' "${STUB_HEAD_REF:-}"; exit 0; fi
printf ''
exit 0
"""


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / PR_REVIEW_WORKFLOW).exists():
            return parent
    raise RuntimeError(f"Could not locate {PR_REVIEW_WORKFLOW} from the test path")


def _render(value: str, overrides: dict[str, str]) -> str:
    values = {**_DEFAULT_EXPRESSIONS, **overrides}

    def substitute(match: re.Match[str]) -> str:
        expression = match.group(1)
        if expression not in values:
            raise AssertionError(
                f"Unmapped GitHub expression {expression!r} in pr-review.yml — add it to "
                "_DEFAULT_EXPRESSIONS so the step stays executable under test"
            )
        return values[expression]

    return _EXPRESSION.sub(substitute, value)


class StepRun(NamedTuple):
    """The observed result of executing the real step."""

    exit_code: int
    stdout: str
    stderr: str
    outputs: dict[str, str]
    gh_calls: str


def _step() -> dict:
    workflow = yaml.safe_load((_repo_root() / PR_REVIEW_WORKFLOW).read_text(encoding="utf-8"))
    for step in workflow["jobs"]["trigger-modulo-review"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step
    raise AssertionError(f"pr-review.yml job {JOB_NAME!r} has no step named {STEP_NAME!r}")


def _run_step(tmp_path: Path, **expressions: str) -> StepRun:
    """Execute the real ``Set PR details`` step.

    ``expressions`` overrides the GitHub-expression values (see
    ``_DEFAULT_EXPRESSIONS``); ``gh`` is stubbed and answers from the same map,
    so a case describes one event end-to-end.
    """
    step = _step()
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    gh_path = bin_dir / "gh"
    gh_path.write_text(_GH_STUB, encoding="utf-8")
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    script = tmp_path / "set-pr-details.sh"
    script.write_text(_render(step["run"], expressions), encoding="utf-8")
    output_file = tmp_path / "github_output"
    output_file.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update({key: _render(str(value), expressions) for key, value in (step.get("env") or {}).items()})
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GITHUB_OUTPUT"] = str(output_file)
    env["STUB_DIR"] = str(stub_dir)
    env["STUB_LABELS"] = str(expressions.get("gh_labels", ""))
    env["STUB_PR_FOR_SHA"] = str(expressions.get("gh_pr_for_sha", ""))
    env["STUB_CURRENT_SHA"] = str(expressions.get("gh_current_sha", ""))
    env["STUB_HEAD_REF"] = str(expressions.get("gh_head_ref", ""))

    bash = shutil.which("bash") or "/bin/bash"
    completed = subprocess.run(  # noqa: S603 — fixed argv: an interpreter and a test-written script path
        [bash, "-eo", "pipefail", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=_repo_root(),
        timeout=120,
    )
    outputs: dict[str, str] = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    gh_calls = (stub_dir / "gh_args.log").read_text(encoding="utf-8") if (stub_dir / "gh_args.log").exists() else ""
    return StepRun(completed.returncode, completed.stdout, completed.stderr, outputs, gh_calls)


# --- The early path: a PR opened ready is reviewed immediately ----------------


def test_early_event_resolves_the_pr_from_the_event_payload(tmp_path: Path) -> None:
    """A ready PR opened resolves its PR number / head from the event, with no API read.

    This is what lets the review start in minutes instead of after the ~50-minute
    product-CI run: ``pull_request_target`` carries the whole PR, so nothing has
    to be looked up before the webhook can fire.
    """
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "pull_request_target",
            "github.event.pull_request.number": "2246",
            "github.event.pull_request.head.sha": "a" * 40,
            "github.event.pull_request.head.ref": "deliver/far-2246",
            "github.event.pull_request.draft": "false",
            "gh_labels": "changelog: feature,agent-generated",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "true"
    assert run.outputs["pr_number"] == "2246"
    assert run.outputs["head_sha"] == "a" * 40
    assert run.outputs["head_branch"] == "deliver/far-2246"


def test_early_event_does_not_review_a_draft(tmp_path: Path) -> None:
    """``opened`` fires for a draft too; a draft is not ready, so no review is dispatched.

    The completion path still reviews a draft, so nothing is stranded — this only
    stops the early trigger reviewing work in progress (and spending a reviewer
    sandbox slot on it).
    """
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "pull_request_target",
            "github.event.pull_request.number": "2300",
            "github.event.pull_request.head.sha": "b" * 40,
            "github.event.pull_request.head.ref": "wip/thing",
            "github.event.pull_request.draft": "true",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "false"
    assert "draft" in run.stdout


def test_early_event_skips_a_manual_delivery_pr(tmp_path: Path) -> None:
    """A ``manual-delivery`` PR must not be auto-reviewed on the early path.

    This is the defect the naive "just add the trigger" change ships: the gate
    used to live inside the ``workflow_run`` branch, so the early path bypassed
    it and a PR a human was driving manually got an automatic review.
    """
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "pull_request_target",
            "github.event.pull_request.number": "2301",
            "github.event.pull_request.head.sha": "c" * 40,
            "github.event.pull_request.head.ref": "fix/manual",
            "github.event.pull_request.draft": "false",
            "gh_labels": "agent-generated,manual-delivery",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "false"
    assert "manual-delivery label" in run.stdout
    assert "labels[].name" in run.gh_calls


# --- The completion path keeps working, gates included ------------------------


def test_completion_event_resolves_the_pr_from_the_head_sha(tmp_path: Path) -> None:
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "workflow_run",
            "github.event.workflow_run.head_sha": "d" * 40,
            "github.event.workflow_run.head_branch": "deliver/far-1",
            "gh_pr_for_sha": "101",
            "gh_current_sha": "d" * 40,
            "gh_head_ref": "deliver/far-1",
            "gh_labels": "agent-generated",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "true"
    assert run.outputs["pr_number"] == "101"
    assert run.outputs["head_sha"] == "d" * 40
    assert run.outputs["head_branch"] == "deliver/far-1"


def test_completion_event_still_skips_a_manual_delivery_pr(tmp_path: Path) -> None:
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "workflow_run",
            "github.event.workflow_run.head_sha": "e" * 40,
            "gh_pr_for_sha": "102",
            "gh_current_sha": "e" * 40,
            "gh_head_ref": "fix/manual",
            "gh_labels": "manual-delivery",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "false"
    assert "manual-delivery label" in run.stdout


def test_completion_event_still_skips_a_superseded_run(tmp_path: Path) -> None:
    """The older CI run's completion must not review a head that has since moved."""
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "workflow_run",
            "github.event.workflow_run.head_sha": "f" * 40,
            "gh_pr_for_sha": "103",
            "gh_current_sha": "1" * 40,
            "gh_head_ref": "fix/moved",
            "gh_labels": "agent-generated",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "false"
    assert "superseded" in run.stdout


def test_completion_event_for_a_non_pr_ci_run_dispatches_nothing(tmp_path: Path) -> None:
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "workflow_run",
            "github.event.workflow_run.head_sha": "2" * 40,
            "github.event.workflow_run.head_branch": "main",
            "gh_pr_for_sha": "",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "false"
    assert "no open PR" in run.stdout


# --- The manual re-trigger keeps its documented bypass ------------------------


def test_manual_dispatch_bypasses_the_manual_delivery_gate(tmp_path: Path) -> None:
    """An explicit operator re-trigger must still review a labelled PR.

    ``/deploy``-style operational dispatches and the PR Housekeeper use
    ``gh workflow run pr-review.yml``; that path has always bypassed the
    ``manual-delivery`` gate, and the shared gate must not silently change that.
    """
    run = _run_step(
        tmp_path,
        **{
            "github.event_name": "workflow_dispatch",
            "inputs.pr_number": "104",
            "inputs.head_sha": "3" * 40,
            "inputs.head_branch": "fix/manual",
            "gh_labels": "manual-delivery",
        },
    )
    assert run.exit_code == 0, run.stderr
    assert run.outputs["ready"] == "true"
    assert run.outputs["pr_number"] == "104"
    assert "labels[].name" not in run.gh_calls
