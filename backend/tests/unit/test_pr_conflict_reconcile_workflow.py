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

_BRANCH_FIXER_NAME = "Fix: Modulo Branch Fixer"


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


# --- Branch Fixer dispatch by name ---


def test_dispatches_branch_fixer_by_name() -> None:
    raw = _raw()
    assert _BRANCH_FIXER_NAME in raw, f"Does not reference Branch Fixer workflow by name '{_BRANCH_FIXER_NAME}'"


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
