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
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
NATIVE_SMOKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "native-release-smoke.yml"

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
        assert upload_step["uses"].startswith("actions/upload-artifact@"), (
            f"{job_id}: log upload must use the official upload-artifact action"
        )
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


def test_release_identity_resolves_annotated_tags_to_commit_sha_and_fails_closed() -> None:
    workflow_text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "TAG_OBJECT_TYPE=\"$(jq -r '.object.type // empty'" in workflow_text
    assert "TAG_OBJECT_SHA=\"$(jq -r '.object.sha // empty'" in workflow_text
    assert 'if [ "$TAG_OBJECT_TYPE" = "tag" ]; then' in workflow_text
    assert "gh api \"repos/${{ github.repository }}/git/tags/${TAG_OBJECT_SHA}\" --jq '.object.sha'" in workflow_text
    assert 'elif [ "$TAG_OBJECT_TYPE" = "commit" ]; then' in workflow_text
    assert 'ACTUAL_SHA="$TAG_OBJECT_SHA"' in workflow_text
    assert "resolved to unsupported Git object type" in workflow_text
    assert "did not resolve to a full commit SHA" in workflow_text
    assert 'if [ "$ACTUAL_SHA" != "$REQUESTED_SHA" ]; then' in workflow_text


def test_release_identity_keeps_normal_deploy_outside_rc_validation() -> None:
    workflow = _workflow()
    step_names = _step_names(workflow["jobs"]["deploy-throttle"]["steps"])
    validation_script = workflow["jobs"]["deploy-throttle"]["steps"][
        step_names.index("Validate requested release identity")
    ]["run"]

    assert 'if [ -z "$REQUESTED_REF" ] && [ -z "$REQUESTED_SHA" ]; then' in validation_script
    assert 'echo "Normal deploy identity: ${{ github.sha }}"' in validation_script
    assert "release_ref must be an RC tag" in validation_script


def test_release_attests_bundle_tarball_and_sbom_subjects() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["attest-artefacts"]
    assert job["needs"] == ["produce-sbom", "bundle"]
    names = _step_names(job["steps"])
    assert "Download the SBOM artefact" in names
    assert "Download the published bundle artefact" in names
    bundle_download = job["steps"][names.index("Download the published bundle artefact")]
    assert bundle_download["with"]["name"] == "bundle-artefacts-amd64"
    assert bundle_download["with"]["path"] == "release-artefacts"
    assert bundle_download["if"] == "inputs.mode == 'cut' || inputs.mode == 'recut'"
    attest = job["steps"][names.index("Attest the file artefacts")]
    assert attest["with"]["subject-path"] == "release-artefacts/*"


def test_native_release_smoke_verifies_release_tarball_attestation_before_install() -> None:
    workflow = yaml.safe_load(NATIVE_SMOKE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release-smoke"]["steps"]
    names = _step_names(steps)
    verify_idx = names.index("Verify release tarball attestation before install")
    install_idx = names.index("Install from the artifact (verified, installer enforces signature + sha256)")
    verify = steps[verify_idx]
    assert verify_idx < install_idx
    assert verify["if"] == "github.event_name == 'push'"
    assert 'gh attestation verify "${TARBALL}"' in verify["run"]


def test_native_release_smoke_n_minus_one_reuses_fail_closed_artifact_gates() -> None:
    workflow = yaml.safe_load(NATIVE_SMOKE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["canary"]["steps"]
    names = _step_names(steps)
    n_minus_one = steps[names.index("N-1 upgrade leg (canary leg only)")]
    script = n_minus_one["run"]

    assert "n1-artifact" in script
    assert "RELEASE_MANIFEST.json" in script
    assert "RELEASE_MANIFEST.json.sig" in script
    assert "SHA256SUMS" in script
    assert 'sha256sum "${n1_dir}/${n1_tarball}"' in script
    assert 'grep -F "${n1_tarball}" "${n1_dir}/SHA256SUMS"' in script
    assert 'gh attestation verify "${n1_dir}/${n1_tarball}"' in script
    assert 'bash scripts/install.sh --from-file "${n1_dir}/${n1_tarball}"' in script
    assert script.index("RELEASE_MANIFEST.json") < script.index("bash scripts/install.sh")
    assert script.index("RELEASE_MANIFEST.json.sig") < script.index("bash scripts/install.sh")
    assert script.index("SHA256SUMS") < script.index("bash scripts/install.sh")
    assert script.index("sha256sum") < script.index("bash scripts/install.sh")
    assert script.index("gh attestation verify") < script.index("bash scripts/install.sh")


def test_release_sh_passes_rehearsal_env_through() -> None:
    """release.sh must not sanitise the env alembic's env.py reads.

    The rehearsal env var reaches ``alembic upgrade heads`` through the
    process environment: release.sh only sets DATABASE_URL/DATABASE_ADMIN_URL
    and never unsets anything.
    """
    release_sh = (REPO_ROOT / "deploy" / "fly" / "release.sh").read_text(encoding="utf-8")
    assert "unset " not in release_sh, "release.sh must not unset env vars (would strip ALEMBIC_REHEARSAL)"
    assert "alembic upgrade heads" in release_sh
