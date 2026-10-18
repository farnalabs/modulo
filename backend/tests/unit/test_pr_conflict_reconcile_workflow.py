"""Structural gate: pr-conflict-reconcile.yml must wire the Branch Fixer
dispatch for conflicted PRs (FAR-805).

CI does not exercise ``.github/workflows/pr-conflict-reconcile.yml`` itself
(it only runs on schedule/manual dispatch), so this test is the only gate
that keeps the detection, dispatch, and notify wiring intact — the same
reasoning as ``test_deploy_workflow_rehearsal.py`` for deploy.yml.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-conflict-reconcile.yml"


def _raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_raw())


# --- existence and parse ---


def test_workflow_file_exists_and_parses() -> None:
    assert WORKFLOW.exists(), f"Workflow file missing: {WORKFLOW}"
    wf = _workflow()
    assert isinstance(wf, dict), "YAML root is not a mapping"
    # PyYAML parses `on` as the boolean True — both are valid keys
    assert "on" in wf or True in wf, "Top-level 'on' key missing"


# --- trigger wiring ---


def test_has_cron_schedule_and_workflow_dispatch() -> None:
    wf = _workflow()
    # PyYAML parses `on` as the boolean True
    on = wf.get("on", wf.get(True, {}))
    assert "schedule" in on, "Missing cron schedule trigger"
    schedules = on["schedule"]
    assert isinstance(schedules, list), "schedule must be a list"
    crons = [s.get("cron") for s in schedules]
    assert any("*/15" in c for c in crons if c), "Missing */15 cron schedule"
    assert "workflow_dispatch" in on, "Missing workflow_dispatch trigger"


# --- permissions ---


def test_has_actions_write_permission() -> None:
    raw = _raw()
    assert "actions: write" in raw, "actions: write permission missing"


# --- CONFLICTING filter ---


def test_filters_on_conflicting() -> None:
    raw = _raw()
    assert "CONFLICTING" in raw, "Does not filter on mergeable=CONFLICTING"


# --- Branch Fixer dispatch wiring ---


def test_dispatches_branch_fixer_by_name() -> None:
    """After FAR-807, the exact workflow name 'Fix: Modulo Branch Fixer' is no
    longer referenced (the old gh run list dedup that cited it was removed).
    The dispatch step must still exist and reference 'Branch Fixer'."""
    raw = _raw()
    assert "Branch Fixer" in raw, "Does not reference Branch Fixer anywhere"


# --- Dispatch step must carry GH_TOKEN so the in-flight dedup `gh run list` auths ---
# (raw-string containment checks alone would not catch the dead-dedup runtime bug
#  flagged in review: a dispatch step without GH_TOKEN makes the dedup dead code)


def test_dispatch_step_sets_github_token() -> None:
    wf = _workflow()
    jobs = wf.get("jobs", {})
    dispatch_step = None
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("name") == "Dispatch the Branch Fixer":
                dispatch_step = step
                break
        if dispatch_step is not None:
            break
    assert dispatch_step is not None, "Dispatch the Branch Fixer step missing"
    env = dispatch_step.get("env", {})
    assert "GH_TOKEN" in env, (
        "Dispatch step env must set GH_TOKEN so the in-flight dedup `gh run list` can auth (FAR-805 review)"
    )


# --- manual-delivery label honour ---


def test_mentions_manual_delivery_label() -> None:
    raw = _raw()
    assert "manual-delivery" in raw, "Does not reference manual-delivery label"


# --- modulo-cannot-fix label honour ---


def test_mentions_modulo_cannot_fix_label() -> None:
    raw = _raw()
    assert "modulo-cannot-fix" in raw, "Does not reference modulo-cannot-fix label"


# --- fail-open ::warning:: path ---


def test_fail_open_warning_path_exists() -> None:
    raw = _raw()
    assert "::warning::" in raw, "No fail-open ::warning:: path found"


# --- FAR-807: inert dedup replaced with fixer-commit recency signal ---


def test_no_inert_gh_run_list_dedup() -> None:
    """The old dedup used `gh run list --workflow "Fix: Modulo Branch Fixer"` which
    never matched because the Branch Fixer is triggered via webhook, not GitHub
    Actions. This guard must be absent after FAR-807."""
    raw = _raw()
    assert "gh run list --workflow" not in raw, "Inert gh run list --workflow dedup still present (FAR-807)"


def test_references_fixer_commit_signal() -> None:
    """The fixer commits as 'modulo-branch-fixer' — the workflow must use this
    committer name as the dedup signal."""
    raw = _raw()
    assert "modulo-branch-fixer" in raw, "Workflow must reference 'modulo-branch-fixer' committer name for dedup"


def test_recency_window_constant_present() -> None:
    """The 30-minute recency window must be documented as a constant or comment
    so the guard's limits are honest."""
    raw = _raw()
    assert "30" in raw, "30-minute recency window constant/comment missing"


# --- FAR-1935: deterministic conflict fingerprint so duplicate dispatches
# --- dedupe in the Branch Fixer instead of launching a second run.


def test_dispatch_payload_includes_fingerprint() -> None:
    """FAR-1935: the dispatch payload MUST carry a ``fingerprint``, mirroring
    ci.yml's notify-pr-failure payload. Without it the Branch Fixer cannot
    dedupe repeat dispatches for the same conflict (observed on #1937, which
    was dispatched twice ~2h apart while the first run was still live)."""
    raw = _raw()
    assert "--arg fingerprint" in raw, "Dispatch payload omits --arg fingerprint (FAR-1935)"
    assert "fingerprint: $fingerprint" in raw, "Dispatch jq object omits the fingerprint key (FAR-1935)"


def test_fingerprint_is_keyed_on_pr_and_main_sha() -> None:
    """FAR-1935: the fingerprint must key on the conflicting state — the PR
    number and the tip of main. Same PR + same main = the SAME merge to
    reconcile = dedupe; main moved = a genuinely different merge = different
    fingerprint = allowed through."""
    raw = _raw()
    expected = 'FINGERPRINT="conflict|pr:${PR_NUM}|main:${MAIN_SHA}"'
    assert expected in raw, "Fingerprint shape is not conflict|pr:<n>|main:<sha> (FAR-1935)"


def test_resolves_main_sha_for_the_fingerprint() -> None:
    """FAR-1935: the main SHA the fingerprint keys on must be resolved by the
    workflow. This job has no actions/checkout step and therefore no git remote,
    so it is fetched through the GitHub API."""
    raw = _raw()
    assert "repos/farnalabs/modulo/commits/main" in raw, "Workflow does not resolve the tip of main (FAR-1935)"
    assert "MAIN_SHA=" in raw, "Workflow never assigns MAIN_SHA (FAR-1935)"


# --- FAR-1949: an undelivered Branch Fixer dispatch must leave a durable,
# --- PR-attributable signal on the PR. The "never SILENT" requirement is
# --- FAR-1939, but its merge-queue.yml fallback only emits ::error:: + exit 1;
# --- the durable-comment + marker pattern mirrors this workflow's own
# --- manual-delivery note ("Surface manual-delivery conflicts").


def _dispatch_step_block() -> str:
    """Raw text of the 'Dispatch the Branch Fixer' step, up to the next step.

    Scoping the containment checks below to this one step matters: the
    manual-delivery step already calls ``gh pr comment``, so a file-wide
    ``"gh pr comment" in raw`` check would pass even while the dispatch-failure
    arm posts nothing — exactly the FAR-1949 defect."""
    raw = _raw()
    marker = "- name: Dispatch the Branch Fixer"
    start = raw.index(marker)
    rest = raw[start + len(marker) :]
    end = rest.find("- name: ")
    return rest if end == -1 else rest[:end]


def test_dispatch_failure_posts_a_pr_comment() -> None:
    """FAR-1949: after the retries are exhausted the step must leave a durable
    comment on the affected PR. On 2026-10-17 the dispatch was refused (HTTP
    429, concurrent-run limit) and the conflicted PR carried nothing saying
    reconciliation had been attempted and refused."""
    block = _dispatch_step_block()
    assert 'gh pr comment "$PR_NUM"' in block, (
        "Dispatch-failure arm must post a `gh pr comment` on the affected PR (FAR-1949)"
    )


def test_dispatch_failure_comment_says_why_and_links_the_run() -> None:
    """FAR-1949: the comment must record why the fixer was never asked (the
    HTTP status / error detail) and link the run, so a conflicted PR is
    visibly stuck rather than silently stuck."""
    block = _dispatch_step_block()
    why = (
        "could NOT dispatch the Branch Fixer for this PR: all 5 webhook attempts "
        "failed (last HTTP ${LAST_HTTP}: ${DETAIL})"
    )
    assert why in block, "Undelivered-notification body must state why (HTTP status) (FAR-1949)"
    assert "actions/runs/${{ github.run_id }}" in block, (
        "Undelivered-notification body must carry the run URL (FAR-1949)"
    )


def test_dispatch_failure_error_names_the_pr() -> None:
    """FAR-1949: the ::error:: must name the affected PR so the line is
    attributable rather than an anonymous count."""
    block = _dispatch_step_block()
    assert (
        "::error::Branch Fixer dispatch failed for PR #$PR_NUM (HTTP ${LAST_HTTP}: "
        "${DETAIL}) after retries — this PR was never handed to the fixer."
    ) in block, "Dispatch-failure ::error:: must name the affected PR (FAR-1949)"


def test_dispatch_failure_escalates_when_the_comment_fails() -> None:
    """FAR-1949: if the durable comment itself cannot be posted, that gap must
    not be silent — an escalated ::error:: names the PR."""
    block = _dispatch_step_block()
    assert "::error::PR #$PR_NUM: could not post the undelivered-notification comment either" in block, (
        "Missing escalated ::error:: for a failed undelivered-notification comment (FAR-1949)"
    )


def test_dispatch_failure_keeps_the_loud_failure_and_can_comment() -> None:
    """FAR-1949: the durable comment is the fix, but the loud failure and the
    credentials that make the comment possible must both survive. The retry
    count/backoff are unchanged; the step must still exit 1, the step env must
    still carry GH_TOKEN, and the token must still be granted
    ``pull-requests: write``. The first assertion is new (it fails before the
    change); the rest are retained invariants pinned alongside it."""
    block = _dispatch_step_block()
    assert "this PR was never handed to the fixer." in block
    assert "exit 1" in block, "Dispatch-failure arm must still exit 1 (FAR-1949)"
    assert "GH_TOKEN" in block, "Dispatch step env must keep GH_TOKEN for the comment (FAR-1949)"
    assert "pull-requests: write" in _raw(), "pull-requests: write permission missing (FAR-1949)"


def test_dispatch_failure_comment_is_idempotent_via_marker() -> None:
    """FAR-1949 review: this reconciler fires every 15 minutes, so a
    persistently failing webhook (e.g. a sustained 429) must not post a fresh
    undelivered-dispatch comment on every cycle. The note must carry an HTML
    marker and be skipped when that marker already exists — the same
    idempotency the manual-delivery note in this workflow uses."""
    block = _dispatch_step_block()
    assert "<!-- pr-conflict-reconciler:undelivered-dispatch -->" in block, (
        "Undelivered-dispatch comment must carry a dedup marker (FAR-1949 review)"
    )
    assert 'gh pr view "$PR_NUM" --repo farnalabs/modulo --json comments' in block, (
        "Undelivered-dispatch arm must check for an existing marker comment (FAR-1949 review)"
    )
    assert "UNDELIVERED_MARKER" in block, "Marker must be used in the jq existence check (FAR-1949 review)"
    assert "already carries an undelivered-dispatch note" in block, (
        "Undelivered-dispatch arm must skip reposting when the marker exists (FAR-1949 review)"
    )


def test_dispatch_failure_escalation_names_the_comment_failure_separately() -> None:
    """FAR-1949 review: the escalated ::error:: must distinguish the COMMENT
    failure from the webhook failure. Appending the webhook's HTTP status next
    to the comment failure made a reader misattribute it as the reason the
    comment could not be posted."""
    block = _dispatch_step_block()
    assert "comment post failed" in block, "Escalated ::error:: must name the comment failure (FAR-1949 review)"
    assert "the dispatch failure was webhook HTTP" in block, (
        "Escalated ::error:: must label the webhook status as the dispatch failure (FAR-1949 review)"
    )
