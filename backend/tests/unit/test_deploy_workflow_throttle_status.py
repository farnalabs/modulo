"""Structural gate: the FAR-954 throttle-status annotation job must survive edits.

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps a throttled no-op run
visibly distinct from a real deployment. The sibling
``test_deploy_workflow_rehearsal.py`` exists for the same reason.

FAR-954: a throttled deploy run concludes SUCCESS without deploying anything,
so the run must carry a loud summary annotation. ``deploy-throttle-status``
reads ``needs.deploy-throttle.outputs.should_deploy`` and writes the
throttled/deploying distinction into the step summary.

Legibility follow-up (throttle skip visibility): the run additionally carries
(1) a verdict summary written by the ``deploy-throttle`` job itself, including
WHY it skipped and WHEN the next deploy is expected (``skip_reason`` /
``next_deploy`` outputs), (2) a dynamic job display name so the skip is
visible in the run's job list without opening a log, and (3) a ``::warning::``
(not ``::notice::``) annotation on the skip path.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

_JOB_ID = "deploy-throttle-status"
_SUMMARY_STEP_NAME = "Write throttle/deploy summary annotation"
_SHOULD_DEPLOY = "${{ needs.deploy-throttle.outputs.should_deploy }}"


def _workflow() -> dict:
    return yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))


def _summary_step() -> dict:
    job = _workflow()["jobs"][_JOB_ID]
    steps = job["steps"]
    names = [step.get("name", "") for step in steps]
    return steps[names.index(_SUMMARY_STEP_NAME)]


def _throttle_check_step() -> dict:
    job = _workflow()["jobs"]["deploy-throttle"]
    steps = job["steps"]
    ids = [step.get("id", "") for step in steps]
    return steps[ids.index("check")]


def test_throttle_status_job_exists_and_always_runs() -> None:
    job = _workflow()["jobs"][_JOB_ID]
    assert job["if"] == "always()", f"{_JOB_ID}: must run even when the throttle skips every deploy job"
    assert "deploy-throttle" in job["needs"], f"{_JOB_ID}: must depend on deploy-throttle to read should_deploy"


def test_throttle_status_keys_off_should_deploy() -> None:
    step = _summary_step()
    assert step["env"]["SHOULD_DEPLOY"] == _SHOULD_DEPLOY, (
        f"{_JOB_ID}: annotation must key off needs.deploy-throttle.outputs.should_deploy"
    )
    script = step["run"]
    assert 'if [ "$SHOULD_DEPLOY" = "true" ]' in script
    assert "THROTTLED" in script, "the throttled branch must label the run unmistakably"
    assert "NO DEPLOYMENT" in script


def test_deployed_summary_interpolates_short_sha_without_sed_patch() -> None:
    """The deployed branch must not rely on a post-hoc ``sed`` placeholder patch.

    FAR-954 review: the summary is appended into ``$GITHUB_STEP_SUMMARY``; a
    quoted heredoc plus a ``sed`` pass is fragile (a placeholder that the regex
    fails to match renders verbatim). The computed short SHA must be
    interpolated by the shell itself, so the heredoc must stay unquoted and the
    ``sed`` cleanup pass must not return.
    """
    script = _summary_step()["run"]
    assert "<<'EOF'" not in script, "deployed summary must use an unquoted heredoc so ${SHORT_SHA} expands"
    assert "sed -i" not in script, "drop the sed placeholder patch and write the value directly"
    assert "**Deploying SHA:** ${SHORT_SHA}" in script, (
        "deployed summary must interpolate the computed short SHA into the heredoc"
    )


def test_throttle_job_publishes_skip_reason_and_next_deploy() -> None:
    """The throttle job must tell readers WHY it skipped and WHEN the next deploy is."""
    workflow = _workflow()
    outputs = workflow["jobs"]["deploy-throttle"]["outputs"]
    assert "skip_reason" in outputs, "deploy-throttle must export skip_reason for the status job"
    assert "next_deploy" in outputs, "deploy-throttle must export next_deploy for the status job"

    check_step = _throttle_check_step()
    script = check_step["run"]
    assert "$GITHUB_STEP_SUMMARY" in script, "the throttle job itself must write a verdict job summary"
    assert "skip_reason=in_progress" in script, "the in-progress skip must record its reason"
    assert "skip_reason=interval_throttled" in script, "the interval skip must record its reason"
    assert "next_deploy=" in script, "every skip/deploy branch must record when the next deploy is expected"
    # The next-eligibility timestamp must derive from the SAME anchor the skip
    # used - never from `now` - or the summary can disagree with the throttle.
    assert "NEXT_TS=$((LAST_TS + INTERVAL_HOURS * 3600))" in script, (
        "next-deploy estimate must be anchor + interval, computed from the skip's own anchor"
    )
    # Annotations must state the skip explicitly, not just 'skipping deploy'.
    assert "This run will NOT deploy" in script


def test_status_job_display_name_reflects_throttle_outcome() -> None:
    """The job's display name must be dynamic so the skip shows in the job list.

    A run's name (``run-name``) is fixed at run creation - before the throttle
    decides - and GitHub has no distinct terminal run state we can set without
    failing the run, so the dynamic job name is the at-a-glance signal.
    """
    name = _workflow()["jobs"][_JOB_ID]["name"]
    assert "needs.deploy-throttle.outputs.should_deploy" in name, (
        "status job name must key off should_deploy so THROTTLED - NO DEPLOY is visible at a glance"
    )
    assert "THROTTLED - NO DEPLOY" in name
    assert "DEPLOYING" in name


def test_status_skip_path_uses_warning_annotation_with_reason_and_next_deploy() -> None:
    step = _summary_step()
    assert step["env"]["SKIP_REASON"] == "${{ needs.deploy-throttle.outputs.skip_reason }}", (
        "status job must consume the throttle job's skip_reason, not re-derive the cause"
    )
    assert step["env"]["NEXT_DEPLOY"] == "${{ needs.deploy-throttle.outputs.next_deploy }}", (
        "status job must consume the throttle job's next_deploy, not re-derive the time"
    )
    script = step["run"]
    assert '"::warning::THROTTLED' in script, (
        "the skip must be a warning-level annotation so it is not lost among notices"
    )
    assert "Next deploy expected:" in script, "the skip summary must say when the next deploy is expected"
    assert "Another deploy run is already in progress" in script, "in-progress batching must be named as the reason"
    assert "under the throttle interval" in script, "interval throttling must be named as the reason"
    # force_deploy must still deploy (and say so) - the bypass must survive.
    assert "force_deploy=true" in script


def test_force_deploy_bypass_still_deploys() -> None:
    """force_deploy short-circuits the throttle before any check runs."""
    script = _throttle_check_step()["run"]
    force_idx = script.index('if [ "$FORCE_DEPLOY" = "true" ]')
    in_progress_idx = script.index('if [ -n "$IN_PROGRESS" ]')
    assert force_idx < in_progress_idx, (
        "force_deploy must exit before the in-progress/throttle checks - the bypass order is the policy"
    )
    assert 'echo "should_deploy=true" >> "$GITHUB_OUTPUT"' in script
    assert "Force deploy requested" in script
