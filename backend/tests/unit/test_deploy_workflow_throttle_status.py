"""Structural gate: the FAR-954 throttle-status annotation job must survive edits.

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps a throttled no-op run
visibly distinct from a real deployment. The sibling
``test_deploy_workflow_rehearsal.py`` exists for the same reason.

FAR-954: a throttled deploy run concludes SUCCESS without deploying anything,
so the run must carry a loud summary annotation. ``deploy-throttle-status``
reads ``needs.deploy-throttle.outputs.should_deploy`` and writes the
throttled/deploying distinction into the step summary.
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
