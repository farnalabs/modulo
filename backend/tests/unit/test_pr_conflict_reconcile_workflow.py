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
