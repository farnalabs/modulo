"""Structural gate: deploy.yml must wire the migration rehearsal into BOTH
deploy legs (FAR-717).

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps the rehearsal-first
pre-deploy wiring intact — the same reasoning as the sibling
``test_extract_machine_id.py`` for the machine-id parser.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

_REHEARSAL_STEP_NAME = "Pre-deploy REHEARSAL migrations (one-off machine — always rolls back)"
_REAL_STEP_NAME = "Pre-deploy migrations (one-off machine)"
_REHEARSAL_DESTROY_NAME = "Destroy pre-rehearsal machine"

#: (job id, fly app, artifact suffix, fly-logs app flag)
_LEGS = (
    ("deploy-staging", "staging-modulo", "staging"),
    ("deploy-production", "app-modulo", "prod"),
)


def _workflow() -> dict:
    return yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))


def _step_names(steps: list[dict]) -> list[str]:
    return [step.get("name", "") for step in steps]


def _upload_step_name(suffix: str) -> str:
    return f"Upload migration machine logs ({suffix})"


def test_rehearsal_step_exists_before_real_migrations_per_leg() -> None:
    workflow = _workflow()
    for job_id, app, _suffix in _LEGS:
        steps = workflow["jobs"][job_id]["steps"]
        names = _step_names(steps)
        assert _REHEARSAL_STEP_NAME in names, f"{job_id}: rehearsal step missing"
        assert _REAL_STEP_NAME in names, f"{job_id}: real pre-migrate step missing"
        rehearsal_idx = names.index(_REHEARSAL_STEP_NAME)
        real_idx = names.index(_REAL_STEP_NAME)
        assert rehearsal_idx < real_idx, f"{job_id}: rehearsal must run BEFORE the real pre-migrate"

        destroy_idx = names.index(_REHEARSAL_DESTROY_NAME)
        assert destroy_idx == rehearsal_idx + 1, (
            f"{job_id}: rehearsal destroy must immediately follow the rehearsal step"
        )
        assert steps[destroy_idx].get("if") == "always()", f"{job_id}: rehearsal destroy must run in every outcome"


def test_rehearsal_launch_matches_real_launch_with_rehearsal_env() -> None:
    workflow = _workflow()
    for job_id, app, _suffix in _LEGS:
        steps = workflow["jobs"][job_id]["steps"]
        names = _step_names(steps)
        rehearsal_script = steps[names.index(_REHEARSAL_STEP_NAME)]["run"]
        real_script = steps[names.index(_REAL_STEP_NAME)]["run"]

        # Same one-off machine contract, plus the rehearsal env var.
        assert f"--app {app} --restart=no" in rehearsal_script
        assert "--entrypoint /bin/bash" in rehearsal_script
        assert '"/release.sh"' in rehearsal_script
        assert "--env ALEMBIC_REHEARSAL=1" in rehearsal_script
        assert "--env ALEMBIC_REHEARSAL=1" not in real_script, f"{job_id}: the REAL run must NOT flag rehearsal mode"

        # The exit-code gate is present and failure names the rehearsal.
        assert "::error::REHEARSAL migration machine" in rehearsal_script


def test_machine_log_capture_wired_per_leg() -> None:
    """Failure paths fetch the machine's logs (post-mortem diagnosis saver)."""
    workflow = _workflow()
    for job_id, app, suffix in _LEGS:
        steps = workflow["jobs"][job_id]["steps"]
        names = _step_names(steps)
        rehearsal_script = steps[names.index(_REHEARSAL_STEP_NAME)]["run"]
        real_script = steps[names.index(_REAL_STEP_NAME)]["run"]

        capture_cmd = f"fly logs --app {app} --machine"
        assert capture_cmd in rehearsal_script, f"{job_id}: rehearsal failure must capture machine logs"
        assert capture_cmd in real_script, f"{job_id}: real pre-migrate failure must capture machine logs"
        assert "--no-tail" in rehearsal_script, "logs fetch must not stream"

        # Artifacts: distinct names per leg, short retention.
        upload_name = _upload_step_name(suffix)
        assert upload_name in names, f"{job_id}: artifact upload step missing"
        upload_step = steps[names.index(upload_name)]
        assert upload_step["uses"] == "actions/upload-artifact@v4"
        assert upload_step["with"]["retention-days"] == 3
        assert upload_step["if"] == "always()"
        assert f"pre-migrate-machine-logs-{suffix}" == upload_step["with"]["name"]
        assert "/tmp/prerehearse-failure.log" in upload_step["with"]["path"]
        assert "/tmp/premigrate-failure.log" in upload_step["with"]["path"]


def test_real_run_discovery_excludes_leaked_rehearsal_machine() -> None:
    workflow = _workflow()
    for job_id, _app, _suffix in _LEGS:
        steps = workflow["jobs"][job_id]["steps"]
        names = _step_names(steps)
        real_script = steps[names.index(_REAL_STEP_NAME)]["run"]
        assert "/tmp/prerehearse-machine-id.txt" in real_script, (
            f"{job_id}: real pre-migrate discovery must exclude the rehearsal machine's id"
        )


def test_release_sh_passes_rehearsal_env_through() -> None:
    """release.sh must not sanitise the env alembic's env.py reads.

    The rehearsal env var reaches ``alembic upgrade heads`` through the
    process environment: release.sh only sets DATABASE_URL/DATABASE_ADMIN_URL
    and never unsets anything.
    """
    release_sh = (REPO_ROOT / "deploy" / "fly" / "release.sh").read_text(encoding="utf-8")
    assert "unset " not in release_sh, "release.sh must not unset env vars (would strip ALEMBIC_REHEARSAL)"
    assert "alembic upgrade heads" in release_sh
