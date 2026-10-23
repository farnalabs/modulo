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
real 429 exercises the path end to end, and CI never does that. The three step
bodies were also driven through a stubbed dispatch/``gh`` harness out of band
when the fix landed (all PRs commented -> exit 0; one un-commented -> exit 1; no
PR identified -> exit 1), but that harness is not committed, so that particular
behavioural check is NOT reproducible from the repo for the migration-collision
and CI-failure steps. A live tick remains the only end-to-end proof for those.

Since FAR-2098 the merge-conflict step's body IS additionally driven by a
committed behavioural harness
(``test_merge_queue_fixer_dedupe_harness.py``): it runs this step's real ``run``
block against a stubbed ``gh`` and dispatch script under
``bash --noprofile --norc -eo pipefail``, and two of its scenarios pin this
module's contract from the outside (an undelivered notification records its
durable comment and lets the pass continue; a dispatch whose durable comment
also fails still fails hard).

This module owns the fail-open/fail-hard split.
``tests/architecture/test_workflow_structure.py::test_merge_queue_branch_fixer_dispatch_failures_are_attributable``
pins the attributable, durable per-PR failure arm around the same three steps.
The division is deliberate (structure here, provenance there), but both gates
read the same steps, so a change to a notify step's failure arm must update both
in the same change.
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


# --- FAR-2098: dedupe the fixer re-notification per PR head SHA at the source ---
#
# FAR-2092 (above) stopped an undelivered notification from DISCARDING a
# completed merge. This section pins the other half: the same conflicted PR was
# re-notified on every ~15-min pass (PR #2144, 2026-10-21: 8 retry attempts per
# pass against a fixer already at its 8-run cap), which saturated the Branch
# Fixer and burned up to 120s of the job's ~8 min headroom per pass.
#
# The contract pinned here:
#
# * the merge step records each conflicted PR's head SHA, in a file SEPARATE
#   from ``/tmp/conflict_prs.txt`` (whose PR-number-only shape three other
#   consumers depend on);
# * the notification step writes a hidden marker
#   ``<!-- merge-queue-fixer-notified head=<sha> at=<iso8601> -->`` into the
#   comment it posts, on BOTH a delivered and an undelivered dispatch;
# * exactly ONE path suppresses a dispatch - an exact head match inside the
#   cooldown - and every lookup/parse error dispatches with a warning;
# * a suppressed pass posts no comment, so FAR-2092's "the fixer was never
#   notified" record can never become false;
# * none of it reaches a step that merges, validates, or checks migrations.
#
# The decision table itself is exercised behaviourally by
# ``test_merge_queue_fixer_dedupe_harness.py``; this section stops the code from
# being refactored away, which is what a structural gate is for.

MERGE_STEP = "Merge PRs into merge-queue (oldest first)"
HEADS_FILE = "/tmp/conflict_pr_heads.txt"
DEDUPE_MARKER = "merge-queue-fixer-notified"
DEDUPE_FUNCTION = "fixer_notified_recently"
HEAD_LOOKUP_FUNCTION = "conflict_head_sha"


def _run_block(step_name: str) -> str:
    """The shell ``run`` block of one step, by name."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["queue"]["steps"]:
        if not isinstance(step, dict) or step.get("name") != step_name:
            continue
        run = step.get("run")
        assert isinstance(run, str), f"'{step_name}': step has no shell run block"
        return run
    raise AssertionError(f"merge-queue.yml has no '{step_name}' step")


def _shell_function(run: str, name: str) -> str:
    """The text of a shell function defined in a ``run`` block."""
    lines = run.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"{name}() {{":
            start = index
            break
    assert start is not None, f"'{name}' is not defined in the run block"
    for end in range(start + 1, len(lines)):
        if lines[end].strip() == "}":
            return "\n".join(lines[start : end + 1])
    raise AssertionError(f"'{name}' has no closing brace")


def _enclosing_conditionals(lines: list[str], index: int) -> list[str]:
    """Every ``if``-header enclosing ``lines[index]``, innermost first.

    Walks upward tracking block nesting (``fi``/``done``/``esac`` close,
    ``if``/``while``/``for``/``case`` open), so a nested ``if`` between the
    interesting statement and the gate it sits under does not hide that gate.
    """
    depth = 0
    conditions: list[str] = []
    for candidate in range(index - 1, -1, -1):
        text = lines[candidate].strip()
        if text in {"fi", "done", "esac"}:
            depth += 1
            continue
        opens = text.endswith(("; then", "; do", " in"))
        if opens and depth > 0:
            depth -= 1
            continue
        if depth > 0:
            continue
        if text.startswith("if ") and text.endswith("; then"):
            conditions.append(text)
    return conditions


def test_merge_step_records_a_head_sha_for_each_conflicted_pr() -> None:
    """The dedupe keys on the head that conflicted, recorded at conflict time."""
    merge_run = _run_block(MERGE_STEP)
    assert HEADS_FILE in merge_run, (
        "the merge step does not write the conflicted head SHAs, so the notify step cannot dedupe"
    )
    assert f": > {HEADS_FILE}" in merge_run, "the heads file is never initialised, so a stale file could be read"
    assert 'git rev-parse --verify --quiet "refs/heads/pr-$NUM"' in merge_run, (
        "the conflicted head is not taken from the ref this pass just fetched and tried to merge"
    )
    assert 'printf \'%s %s\\n\' "$NUM" "$CONFLICT_HEAD"' in merge_run, (
        "the heads file is not written as '<num> <sha>' pairs"
    )
    # The PR-number list itself must keep its shape: three other consumers read it.
    assert 'echo "$conflict_prs" > /tmp/conflict_prs.txt' in merge_run, (
        "the conflict list format changed - the Alembic-heads rebuild and both payloads read it"
    )


def test_dedupe_marker_is_written_on_both_dispatch_outcomes() -> None:
    """A record must exist wherever a notification attempt was made."""
    run = _run_block(CONFLICT_STEP)
    marker_writes = run.count("head=${PR_HEAD} at=${DISPATCH_AT}")
    assert marker_writes == 2, (
        f"expected the marker on both the delivered and the undelivered comment, found {marker_writes} write(s)"
    )
    assert DEDUPE_MARKER in run
    assert "DISPATCH_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ" in run, "the marker timestamp is not an ISO-8601 UTC stamp"


def test_dedupe_suppresses_only_an_exact_head_inside_the_cooldown() -> None:
    """One suppression path, gated on the cooldown; a changed head dispatches."""
    body = _shell_function(_run_block(CONFLICT_STEP), DEDUPE_FUNCTION)
    lines = body.splitlines()

    suppress = [index for index, line in enumerate(lines) if line.strip() == "return 0"]
    assert len(suppress) == 1, (
        f"the dedupe has {len(suppress)} suppression paths; exactly one (the cooldown) is intended"
    )
    enclosing = _enclosing_conditionals(lines, suppress[0])
    assert any("COOLDOWN_SECONDS" in condition for condition in enclosing), (
        f"the only dedupe skip is not gated on the cooldown - its enclosing conditionals are {enclosing!r}"
    )

    changed_gate = [
        index for index, line in enumerate(lines) if line.strip() == 'if [ "$marker_head" != "$head" ]; then'
    ]
    assert changed_gate, "there is no head-changed branch: a new commit would be deduped away"
    following = [line.strip() for line in lines[changed_gate[0] + 1 : changed_gate[0] + 4]]
    assert "return 1" in following, "the head-changed branch does not dispatch"


def test_dedupe_reads_comments_newest_first_within_a_bound() -> None:
    """Pagination cost is bounded, and the newest page is read first."""
    run = _run_block(CONFLICT_STEP)
    assert "direction=desc" in run, "the comment read is not newest-first, so an old marker could win"
    assert "per_page=${COMMENTS_PER_PAGE}" in run
    assert "MAX_COMMENT_PAGES=3" in run, "the comment read is unbounded"

    body = _shell_function(run, DEDUPE_FUNCTION)
    assert 'while [ "$page" -le "$MAX_COMMENT_PAGES" ]' in body
    assert "page=$((page + 1))" in body, "the read never advances to the next page"


def test_dedupe_fails_open_on_every_lookup_error() -> None:
    """Every error path - failed read, bad JSON, bad marker, bad clock - dispatches."""
    run = _run_block(CONFLICT_STEP)
    body = _shell_function(run, DEDUPE_FUNCTION)
    lines = body.splitlines()

    warnings = [index for index, line in enumerate(lines) if "::warning::" in line]
    assert len(warnings) >= 4, (
        f"only {len(warnings)} fail-open warning(s) in the dedupe: the lookup, JSON, "
        "marker and clock errors are all expected"
    )
    for index in warnings:
        following = [line.strip() for line in lines[index + 1 : index + 4]]
        assert "return 1" in following, (
            f"a dedupe error warns but does not dispatch (fail open) at line {index + 1}: {lines[index].strip()!r}"
        )

    # The caller's default must be dispatch, and the suppression must be the
    # explicit exception.
    assert f'if {DEDUPE_FUNCTION} "$NUM" "$PR_HEAD"; then' in run
    assert 'NOTIFY_PRS="$NOTIFY_PRS $NUM"' in run, "the caller does not default an undecided PR to 'dispatch'"


def test_dedupe_never_posts_a_false_never_notified_comment() -> None:
    """Dedupe reduces dispatch attempts, never the durable record's truthfulness."""
    run = _run_block(CONFLICT_STEP)

    # The failure arm covers only the PRs this pass actually tried to notify.
    failure_arm = run.rsplit('if [ "$DISPATCH_OK" != "true" ]; then', 1)[-1]
    assert "for NUM in $TARGETS; do" in failure_arm, (
        "the undelivered-notification comment loop does not iterate the dispatch set, so a deduped PR "
        "would receive a 'the fixer was never notified' comment that is now false"
    )
    assert "for NUM in $CONFLICTS; do" not in failure_arm
    assert '--arg conflictPrs "$TARGETS"' in run, "the payload does not name the dispatch set"

    # The skip path posts nothing at all: no comment between the notice and exit.
    lines = run.splitlines()
    notice = [
        index for index, line in enumerate(lines) if "no Branch Fixer dispatch and no new PR comment this pass" in line
    ]
    assert notice, "the all-deduped pass has no notice explaining why nothing was dispatched"
    skip_exit = [
        index for index, line in enumerate(lines[notice[0] + 1 :], start=notice[0] + 1) if line.strip() == "exit 0"
    ]
    assert skip_exit, "the all-deduped pass does not exit 0 before dispatching"
    skipped_region = "\n".join(lines[notice[0] : skip_exit[0]])
    assert "gh pr comment" not in skipped_region, "a deduped pass still posts a comment on the PR"
    dispatch = [index for index, line in enumerate(lines) if "bash scripts/dispatch_with_retry.sh" in line]
    assert dispatch, "the notification dispatch is gone"
    assert skip_exit[0] < dispatch[0], "the dedupe skip is evaluated after the dispatch"


def test_dedupe_is_confined_to_the_notification_step(run_blocks: dict[str, str]) -> None:
    """A dedupe bug must not be able to affect validation, migrations or merging."""
    for step_name, run in run_blocks.items():
        if step_name == CONFLICT_STEP:
            continue
        assert DEDUPE_MARKER not in run, f"the FAR-2098 dedupe leaked into '{step_name}'"
        assert HEAD_LOOKUP_FUNCTION not in run, f"the FAR-2098 head lookup leaked into '{step_name}'"

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # Exclude the steps this module owns: the conflict step's own comments NAME
    # the merge command, and the other notification steps are covered above.
    merge_path_steps = [
        step
        for step in workflow["jobs"]["queue"]["steps"]
        if isinstance(step, dict)
        and step.get("name") not in NOTIFICATION_STEPS
        and ("gh pr merge" in (step.get("run") or "") or "alembic" in (step.get("run") or ""))
    ]
    assert merge_path_steps, "no merge/validation steps found - this scan would pass vacuously"
    for step in merge_path_steps:
        run = step.get("run") or ""
        assert DEDUPE_FUNCTION not in run, f"'{step.get('name')}' (merge/validation path) reads the dedupe"
        assert DEDUPE_MARKER not in run, f"'{step.get('name')}' (merge/validation path) reads the dedupe"
