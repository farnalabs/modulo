"""Structural gate: an undelivered Branch Fixer notification must not discard a
completed merge (merge-queue run 39633573993, 2026-10-21).

All three Branch Fixer notification steps in ``merge-queue.yml`` dispatch the
fixer webhook and, when the dispatch is undelivered, post a durable
``gh pr comment`` on every affected PR (FAR-1939). Each of those steps then
ended with an UNCONDITIONAL ``exit 1``.

On 2026-10-21 that discarded a completed merge. ``Merge PRs into merge-queue``
had already succeeded - it wrote ``squash! Merge PR #2153`` and logged
``PR #2153 merged successfully`` - then a conflict notification failed with
``HTTP 429: Concurrent run limit of 8 reached`` and every later step (combined
validation, ``gh pr merge`` to main, Linear/GitHub ticket closing, cleanup) was
skipped.

The contract pinned here:

* the step TRACKS whether every affected PR received its durable comment;
* every ``exit 1`` is GATED on the durable-comment failure condition - the
  only hard failure is a PR left with NO signal at all;
* the fail-open path emits a loud ``::warning::`` naming the durable signal and
  exits 0.

These are structural assertions. Only a live tick with a real conflict and a
real 429 exercises the path end to end, and CI never does that; the three step
bodies were additionally driven through a stubbed dispatch/``gh`` harness when
the fix landed (all PRs commented -> exit 0; one un-commented -> exit 1; no PR
identified -> exit 1).
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "merge-queue.yml"

CONFLICT_STEP = "Notify fixer of conflicts"
MIGRATION_STEP = "Notify fixer of migration collisions (PRs excluded from the batch)"
CI_FAILURE_STEP = "Notify branch-fixer on CI failure"
NOTIFICATION_STEPS = (CONFLICT_STEP, MIGRATION_STEP, CI_FAILURE_STEP)

# "Every affected PR received its durable comment" is observable in the step's
# shell as a count of failed durable comments plus a record of the PRs that did
# get one (the migration step already posts an exclusion comment, so it tracks
# the PRs that ended up with NO signal at all instead).
FAILED_COMMENT_TOKEN = "UNDELIVERED_PR_COUNT"
SIGNAL_TOKENS = ("DURABLE_COMMENT_PR_COUNT", "NO_SIGNAL_PRS")


def _load_run_blocks() -> dict[str, str]:
    """The shell ``run`` block of each notification step, keyed by step name."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    blocks: dict[str, str] = {}
    for step in workflow["jobs"]["queue"]["steps"]:
        if not isinstance(step, dict):
            continue
        if step.get("name") not in NOTIFICATION_STEPS:
            continue
        run = step.get("run")
        assert isinstance(run, str), f"{step.get('name')}: step has no shell run block"
        blocks[step["name"]] = run
    return blocks


@pytest.fixture(scope="module")
def run_blocks() -> dict[str, str]:
    return _load_run_blocks()


def _nearest_preceding_if(lines: list[str], index: int) -> str | None:
    """The text of the nearest ``if ...`` line above ``index``, or None."""
    for candidate in range(index - 1, -1, -1):
        if lines[candidate].lstrip().startswith("if "):
            return lines[candidate]
    return None


@pytest.mark.parametrize("step_name", NOTIFICATION_STEPS)
def test_notification_step_is_present(step_name: str, run_blocks: dict[str, str]) -> None:
    assert step_name in run_blocks, f"merge-queue.yml has no '{step_name}' step"


@pytest.mark.parametrize("step_name", NOTIFICATION_STEPS)
def test_step_tracks_whether_every_pr_got_a_durable_comment(step_name: str, run_blocks: dict[str, str]) -> None:
    run = run_blocks[step_name]
    assert FAILED_COMMENT_TOKEN in run, (
        f"'{step_name}' does not count durable-comment failures "
        f"({FAILED_COMMENT_TOKEN} missing), so it cannot tell a silent PR from a "
        "signalled one"
    )
    signal_token_present = any(token in run for token in SIGNAL_TOKENS)
    assert signal_token_present, (
        f"'{step_name}' does not record which PRs got a durable comment (none of {SIGNAL_TOKENS} present)"
    )


@pytest.mark.parametrize("step_name", NOTIFICATION_STEPS)
def test_exit_1_is_gated_on_the_durable_comment_failure(step_name: str, run_blocks: dict[str, str]) -> None:
    """The only hard failure is an affected PR left with NO durable signal.

    Before 2026-10-21 the step ended with an unconditional ``exit 1``: one 429
    from the Branch Fixer discarded a merge that had already completed. Every
    ``exit 1`` must now sit inside a conditional that tests the durable-comment
    failure counters.
    """
    lines = run_blocks[step_name].splitlines()
    exit_1_indices = [index for index, line in enumerate(lines) if line.strip() == "exit 1"]
    assert exit_1_indices, f"'{step_name}' no longer has a hard-failure `exit 1`"
    for index in exit_1_indices:
        gate = _nearest_preceding_if(lines, index)
        assert gate is not None, f"'{step_name}': the `exit 1` on line {index + 1} is not inside any `if`"
        gated = FAILED_COMMENT_TOKEN in gate or "NO_SIGNAL_PRS" in gate
        assert gated, (
            f"'{step_name}': the `exit 1` on line {index + 1} is not gated on the "
            f"durable-comment failure condition - its enclosing conditional is "
            f"{gate.strip()!r}. A dispatch failure with every PR durably commented "
            "must not discard the pass."
        )


@pytest.mark.parametrize("step_name", NOTIFICATION_STEPS)
def test_durable_comments_let_the_pass_continue(step_name: str, run_blocks: dict[str, str]) -> None:
    lines = run_blocks[step_name].splitlines()
    done_indices = [index for index, line in enumerate(lines) if line.strip() == "done"]
    assert done_indices, f"'{step_name}' has no durable-comment loop"
    after_loop = [line for line in lines[done_indices[-1] + 1 :] if line.strip() and not line.lstrip().startswith("#")]
    assert after_loop, f"'{step_name}' does nothing after its durable-comment loop"
    assert after_loop[0].strip() != "exit 1", (
        f"'{step_name}': the durable-comment loop is followed immediately by "
        "`exit 1`, so an undelivered notification discards the pass even when "
        "every affected PR has a durable signal (the 2026-10-21 defect)."
    )
    warnings = [line for line in lines if "::warning::" in line]
    assert warnings, f"'{step_name}' has no ::warning:: fail-open annotation"
    assert "durable" in " ".join(warnings).lower(), (
        f"'{step_name}': the fail-open warning does not name the durable signal"
    )
    exits_zero = [line for line in after_loop if line.strip() == "exit 0"]
    assert exits_zero, f"'{step_name}': the fail-open path does not exit 0"
