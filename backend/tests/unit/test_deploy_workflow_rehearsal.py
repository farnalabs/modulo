"""Structural gate: deploy.yml must wire the migration rehearsal into BOTH
deploy legs (FAR-717).

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps the rehearsal-first
pre-deploy wiring intact — the same reasoning as the sibling
``test_extract_machine_id.py`` for the machine-id parser.
"""

import importlib.util
import os
import re
import shlex
import subprocess
import textwrap
from pathlib import Path
from shutil import which
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
BUNDLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bundle-release.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
NATIVE_SMOKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "native-release-smoke.yml"
MANIFEST_SOURCE = REPO_ROOT / "backend" / "src" / "modulo" / "launcher" / "manifest.py"

_REHEARSAL_STEP_NAME = "Pre-deploy REHEARSAL migrations (one-off machine — always rolls back)"
_REAL_STEP_NAME = "Pre-deploy migrations (one-off machine)"
_REHEARSAL_DESTROY_NAME = "Destroy pre-rehearsal machine"

#: (job id, fly app, artifact suffix, fly-logs app flag)
_LEGS = (
    ("deploy-staging", "staging-modulo", "staging"),
    ("deploy-production", "app-modulo", "prod"),
)


def _workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8")))


def _step_names(steps: list[dict[str, Any]]) -> list[str]:
    return [step.get("name", "") for step in steps]


def _upload_step_name(suffix: str) -> str:
    return f"Upload migration machine logs ({suffix})"


def _python_heredoc(run_script: str, marker: str = "PY") -> str:
    match = re.search(rf"python - <<'{marker}'\n(?P<body>.*?)\n\s*{marker}", run_script, re.DOTALL)
    assert match is not None, "expected embedded Python heredoc"
    return textwrap.dedent(match.group("body"))


def _release_validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "validate_release_tag", REPO_ROOT / "scripts" / "validate_release_tag.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("release validator module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_signature_heredocs_compile_as_python() -> None:
    workflows = ((BUNDLE_WORKFLOW, "PYEOF"), (RELEASE_WORKFLOW, "PY"))
    for workflow_path, marker in workflows:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        scripts = [
            step["run"]
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "verify_signature" in step.get("run", "") and "MANIFEST_SIGNING_KEY" in step.get("run", "")
        ]
        assert len(scripts) == 1, f"expected one signing script in {workflow_path}"
        compile(_python_heredoc(scripts[0], marker), str(workflow_path), "exec")


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


def test_release_identity_step_passes_gh_token_to_its_gh_api_calls() -> None:
    """The RC identity lookup calls ``gh api``, which needs ``GH_TOKEN`` in the
    step environment; without it every RC dispatch dies in deploy-throttle.
    Normal deploys exit before the ``gh`` calls, so CI cannot catch it - this
    structural assertion is the regression gate.
    """
    workflow = _workflow()
    step_names = _step_names(workflow["jobs"]["deploy-throttle"]["steps"])
    step = workflow["jobs"]["deploy-throttle"]["steps"][step_names.index("Validate requested release identity")]

    assert "gh api" in step["run"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"


#: Caller job id -> called reusable workflow, relative to the repo root.
_INSTALLER_CALLS = (
    ("windows-installer", "./.github/workflows/windows-installer.yml"),
    ("macos-installer", "./.github/workflows/macos-installer.yml"),
)

#: GitHub permission levels, weakest to strongest. ``none`` declares no access.
_PERMISSION_RANK = {"none": 0, "read": 1, "write": 2}


def _declared_permissions(workflow: dict[str, Any]) -> dict[str, str]:
    """Every permission a reusable workflow declares, at its strongest level.

    A caller cannot elevate permissions across a reusable-workflow boundary, so
    it must grant at least what the called workflow declares. The declaration
    has two layers and both count: the workflow-level default (inherited by
    jobs that do not override it) and each job's own ``permissions`` block.
    """
    blocks: list[dict[str, str]] = [workflow.get("permissions") or {}]
    blocks.extend(job.get("permissions") or {} for job in workflow.get("jobs", {}).values())

    declared: dict[str, str] = {}
    for block in blocks:
        for permission, level in block.items():
            current = declared.get(permission)
            if current is None or _PERMISSION_RANK[level] > _PERMISSION_RANK[current]:
                declared[permission] = level
    return {permission: level for permission, level in declared.items() if _PERMISSION_RANK[level] > 0}


def test_release_installer_callers_grant_permissions_their_jobs_require() -> None:
    """Every installer caller must grant >= what its called workflow declares.

    A hardcoded expected-permissions dict only pins today's values; this parses
    the called workflow so the gate fails if EITHER side moves out of step —
    a caller under-granting, or a newly added job declaring a permission the
    caller never granted (the failure a real cut/recut surfaces, not a dry-run).
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    for caller_job_id, called_relative_path in _INSTALLER_CALLS:
        caller = workflow["jobs"][caller_job_id]
        assert caller["uses"] == called_relative_path, f"{caller_job_id} calls an unexpected workflow"

        called = yaml.safe_load((REPO_ROOT / called_relative_path).read_text(encoding="utf-8"))
        granted = caller.get("permissions") or {}

        for permission, required_level in sorted(_declared_permissions(called).items()):
            assert permission in granted, (
                f"{caller_job_id} does not grant '{permission}', which {called_relative_path} declares"
            )
            assert _PERMISSION_RANK[granted[permission]] >= _PERMISSION_RANK[required_level], (
                f"{caller_job_id} grants '{permission}: {granted[permission]}' but "
                f"{called_relative_path} declares '{permission}: {required_level}'"
            )


def test_release_attests_bundle_tarball_and_sbom_subjects() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["attest-artefacts"]
    assert job["needs"] == ["produce-sbom", "final-release-manifest", "release"]
    names = _step_names(job["steps"])
    assert "Download the SBOM artefact" in names
    assert "Download the final cross-platform release artefact" in names
    bundle_download = job["steps"][names.index("Download the final cross-platform release artefact")]
    assert bundle_download["with"]["name"] == "final-release-artefacts-${{ inputs.version }}"
    assert bundle_download["with"]["path"] == "release-artefacts"
    assert bundle_download["if"] == "inputs.mode == 'cut' || inputs.mode == 'recut'"
    attest = job["steps"][names.index("Attest the file artefacts")]
    assert attest["with"]["subject-path"] == "release-artefacts/*"

    digest_validation = job["steps"][names.index("Validate image digest outputs")]
    assert digest_validation["if"] == "inputs.mode == 'cut' || inputs.mode == 'recut' || inputs.mode == 'promote'"
    assert "sha256:[0-9a-f]{64}" in digest_validation["run"]
    assert digest_validation["env"] == {
        "ALL_IN_ONE_DIGEST": "${{ needs.release.outputs.all_in_one_digest }}",
        "BACKEND_DIGEST": "${{ needs.release.outputs.backend_digest }}",
        "FRONTEND_DIGEST": "${{ needs.release.outputs.frontend_digest }}",
        "RUNNER_OPENCODE_DIGEST": "${{ needs.release.outputs.runner_opencode_digest }}",
    }

    expected_images = {
        "Attest all-in-one image": ("ghcr.io/${{ github.repository }}", "all_in_one_digest"),
        "Attest backend image": ("ghcr.io/${{ github.repository }}/backend", "backend_digest"),
        "Attest frontend image": ("ghcr.io/${{ github.repository }}/frontend", "frontend_digest"),
        "Attest runner-opencode image": (
            "ghcr.io/${{ github.repository }}/runner-opencode",
            "runner_opencode_digest",
        ),
    }
    for step_name, (subject_name, output_name) in expected_images.items():
        image_attestation = job["steps"][names.index(step_name)]
        assert image_attestation["if"] == digest_validation["if"]
        assert image_attestation["with"] == {
            "subject-name": subject_name,
            "subject-digest": f"${{{{ needs.release.outputs.{output_name} }}}}",
        }

    verify = job["steps"][names.index("Verify image attestations")]
    assert verify["if"] == digest_validation["if"]
    assert verify["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert 'gh attestation verify "$subject" --repo "${{ github.repository }}"' in verify["run"]
    assert verify["run"].count("ghcr.io/$image_name@$digest") == 1


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


def test_native_release_smoke_has_exactly_one_fail_closed_n_minus_one_leg() -> None:
    """N-1 is a canary-only leg. A second N-1 step must not creep into another
    job: the old release-smoke copy was unreachable (``CANARY`` is set only on
    the canary job, ``N_MINUS_1`` only resolved there) yet still read as a live
    check, and it installed N-1 with a plain curl and no attestation gate.
    """
    workflow = yaml.safe_load(NATIVE_SMOKE_WORKFLOW.read_text(encoding="utf-8"))
    legs = [
        (job_id, step)
        for job_id, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if step.get("name") == "N-1 upgrade leg (canary leg only)"
    ]

    assert [job_id for job_id, _step in legs] == ["canary"]
    assert 'gh attestation verify "${n1_dir}/${n1_tarball}"' in legs[0][1]["run"]


def test_native_release_smoke_uses_central_release_validator_for_all_consumers() -> None:
    workflow_text = NATIVE_SMOKE_WORKFLOW.read_text(encoding="utf-8")
    assert workflow_text.count("scripts/validate_release_tag.py validate") >= 4
    assert "scripts/validate_release_tag.py resolve-n-minus-1" in workflow_text
    assert "releases?per_page=100" in workflow_text
    assert "--paginate --slurp" in workflow_text
    assert "release-pages.json" in workflow_text
    assert "current canary ${tag}" not in workflow_text


def test_release_validator_accepts_only_exact_published_release() -> None:
    validator = _release_validator()
    published = {"tag_name": "bundle-v1.2.3", "draft": False, "prerelease": False}
    assert validator.validate_published_release("bundle-v1.2.3", published) == "bundle-v1.2.3"
    for tag in ("bundle-v1.2", "bundle-v1.2.3-rc1"):
        with pytest.raises(ValueError, match="tag must match"):
            validator.validate_published_release(tag, published)
    for release in (
        {"tag_name": "bundle-v1.2.3", "draft": True, "prerelease": False},
        {"tag_name": "bundle-v1.2.3", "draft": False, "prerelease": True},
    ):
        with pytest.raises(ValueError, match="release is not published"):
            validator.validate_published_release("bundle-v1.2.3", release)


def test_release_validator_resolves_semantic_predecessor_across_pages() -> None:
    validator = _release_validator()
    releases = [
        {"tag_name": "bundle-v1.10.0", "draft": False, "prerelease": False},
        {"tag_name": "bundle-v1.2.0", "draft": False, "prerelease": False},
        {"tag_name": "bundle-v1.9.0", "draft": False, "prerelease": False},
    ]
    assert validator.resolve_n_minus_1([releases[:2], releases[2:]], "bundle-v1.10.0") == "bundle-v1.9.0"


def test_release_validator_excludes_drafts_and_prereleases() -> None:
    releases = [
        {"tag_name": "bundle-v1.0.0", "draft": False, "prerelease": False},
        {"tag_name": "bundle-v1.1.0", "draft": True, "prerelease": False},
        {"tag_name": "bundle-v1.2.0", "draft": False, "prerelease": True},
        {"tag_name": "bundle-v1.3.0", "draft": False, "prerelease": False},
    ]
    assert _release_validator().resolve_n_minus_1([releases], "bundle-v1.3.0") == "bundle-v1.0.0"


def test_release_validator_fails_closed_on_api_failure() -> None:
    with pytest.raises(ValueError, match="release history response is not a list"):
        _release_validator().resolve_n_minus_1(None, "bundle-v1.0.0")


def test_release_validator_fails_closed_on_malformed_history() -> None:
    with pytest.raises(ValueError, match=re.escape("tag must match bundle-vX.Y.Z exactly")):
        _release_validator().resolve_n_minus_1(
            [[{"tag_name": "bundle-v1.0", "draft": False, "prerelease": False}]],
            "bundle-v1.0",
        )


def test_release_validator_handles_first_release() -> None:
    assert (
        _release_validator().resolve_n_minus_1(
            [[{"tag_name": "bundle-v1.0.0", "draft": False, "prerelease": False}]],
            "bundle-v1.0.0",
        )
        is None
    )


def test_release_validator_fails_when_current_release_is_missing() -> None:
    with pytest.raises(ValueError, match="current release is not published"):
        _release_validator().resolve_n_minus_1(
            [[{"tag_name": "bundle-v1.0.0", "draft": False, "prerelease": False}]],
            "bundle-v2.0.0",
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


def test_release_signing_derives_key_id_from_current_slot() -> None:
    manifest_source = MANIFEST_SOURCE.read_text(encoding="utf-8")
    current_match = re.search(r'_KEY_ID_CURRENT = "(?P<key_id>[^"]+)"', manifest_source)
    assert current_match is not None
    current_key_id = current_match.group("key_id")

    for workflow_path in (BUNDLE_WORKFLOW, RELEASE_WORKFLOW):
        workflow_text = workflow_path.read_text(encoding="utf-8")
        assert "key_id=manifest_module._KEY_ID_CURRENT" in workflow_text or "key_id=m._KEY_ID_CURRENT" in workflow_text
    release_workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    verify_steps = release_workflow["jobs"]["publish-release"]["steps"]
    verify_name = "Verify final manifest signature against the checked-in trust store"
    verify_step = verify_steps[_step_names(verify_steps).index(verify_name)]
    assert verify_step["env"]["EXPECTED_KEY_ID"] == current_key_id

    assert "The release workflows always\nsign with :data:`_KEY_ID_CURRENT`" in manifest_source
    assert "literal replacement id" in manifest_source
    assert "Merely changing the secret while CURRENT remains the old key" in manifest_source


def test_bundle_release_verifies_generated_signature_before_upload() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "bundle-release.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["bundle"]["steps"]
    names = _step_names(steps)
    sign_idx = names.index("Generate and sign the SIGNED release manifest")
    upload_idx = names.index("Upload bundle artefacts as workflow outputs")
    sign_step = steps[sign_idx]
    script = sign_step["run"]
    assert sign_idx < upload_idx
    assert sign_step["env"]["MANIFEST_SIGNING_KEY"] == "${{ secrets.BUNDLE_MANIFEST_SIGNING_KEY }}"
    assert "verify_signature(payload, signature)" in script
    assert "_KEY_ID_CURRENT" in script
    assert "generated manifest signature failed trust-store verification" in script
    assert "generated manifest signature did not verify under the expected current key_id" in script
    assert "private_key_hex" in script
    assert "MANIFEST_SIGNING_KEY" not in steps[upload_idx].get("run", "")


def test_final_release_manifest_verification_step_precedes_github_release() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    final_names = _step_names(final_steps)
    build_idx = final_names.index("Build final signed manifest after every platform artifact")
    script = final_steps[build_idx]["run"]
    assert final_steps[build_idx]["env"]["MANIFEST_SIGNING_KEY"] == "${{ secrets.BUNDLE_MANIFEST_SIGNING_KEY }}"
    assert "verify_signature(payload, sig)" in script
    assert "_KEY_ID_CURRENT" in script
    publish_steps = workflow["jobs"]["publish-release"]["steps"]
    publish_names = _step_names(publish_steps)
    verify_idx = publish_names.index("Verify final manifest signature against the checked-in trust store")
    publish_idx = publish_names.index("Create GitHub Release with assets")
    verify_script = publish_steps[verify_idx]["run"]
    assert verify_idx < publish_idx
    assert "read_signature_file" in verify_script
    assert "verify_release_from_files" in verify_script
    assert "EXPECTED_KEY_ID" in publish_steps[verify_idx]["env"]
    assert "RELEASE_MANIFEST.json.sig" in verify_script
    assert "sha256sum --check SHA256SUMS" not in verify_script


def test_portable_artifact_presence_check_is_not_signature_gate() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-release"]["steps"]
    names = _step_names(steps)
    portable_idx = names.index("Verify portable release artefacts")
    final_idx = names.index("Verify final manifest signature against the checked-in trust store")
    assert portable_idx < final_idx
    assert "verify_release_from_files" not in steps[portable_idx]["run"]


def test_release_deploy_dispatch_runs_from_main_for_serialisation() -> None:
    """Deploy must be dispatched from ``main``, not the RC tag.

    ``deploy.yml`` serialises on ``deploy-pipeline-${{ github.ref_name }}``. A
    dispatch ``--ref ${RC_TAG}`` gives every RC its own concurrency group, so
    two forced RC deploys can run concurrently against the same Fly apps and
    the older RC can win. The RC commit is still deployed from ``main`` because
    ``deploy.yml`` checks out ``release_ref``/``release_sha`` explicitly.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    names = _step_names(steps)
    dispatch = steps[names.index("Dispatch Deploy and rc-validate")]["run"]

    assert 'gh workflow run "Deploy: Staging + Prod" --ref main' in dispatch
    assert 'gh workflow run "Deploy: Staging + Prod" --ref "${RC_TAG}"' not in dispatch
    assert "-f force_deploy=true" in dispatch
    assert '-f release_ref="${RC_TAG}"' in dispatch
    assert '-f release_sha="${RC_SHA}"' in dispatch


def _release_step(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    return cast(dict[str, Any], steps[_step_names(steps).index(name)])


def _release_job_run(name: str) -> str:
    run = _release_step(name).get("run")
    assert isinstance(run, str), f"{name!r} must contain a shell run block"
    return run


def _shell_tokens(script: str) -> list[str]:
    tokens: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars=";&|!(){}")
        lexer.commenters = "#"
        lexer.whitespace_split = True
        try:
            raw_tokens = list(lexer)
        except ValueError:
            raw_tokens = stripped.split()
        for raw_token in raw_tokens:
            tokens.extend(part for part in re.split(r"(&&|\|\||[;&|!(){}])", raw_token) if part)
        tokens.append(";")
    return tokens


def _assert_protected_cut_commands_are_safe(cut_path: str) -> None:
    tokens = _shell_tokens(cut_path)
    violations: list[str] = []
    force_pushes: list[list[str]] = []
    command_prefixes = {";", "&&", "||", "&", "|", "(", "{", "if", "then", "elif", "else", "do", "!"}
    for index, token in enumerate(tokens):
        if index and tokens[index - 1] not in command_prefixes:
            continue
        if token == "gh" and tokens[index + 1 : index + 3] == ["pr", "merge"]:
            violations.append("gh pr merge")
        if token != "git" or index + 1 >= len(tokens) or tokens[index + 1] != "push":
            continue
        end = index + 2
        while end < len(tokens) and tokens[end] not in {";", "&&", "||", "&", "|", ")", "}"}:
            end += 1
        invocation = tokens[index:end]
        if "main" in invocation:
            violations.append("git push to main")
        if {"--force", "-f", "--force-with-lease"}.intersection(invocation):
            force_pushes.append(invocation)
            if invocation != ["git", "push", "origin", "release-freeze", "--force"]:
                violations.append("unexpected force push: " + " ".join(invocation))
    if "--admin" in tokens:
        violations.append("gh pr merge --admin")
    assert not violations, "protected release cut bypasses found: " + "; ".join(violations)
    if not force_pushes:
        return
    assert len(force_pushes) == 2
    assert all(push == ["git", "push", "origin", "release-freeze", "--force"] for push in force_pushes)


def _cut_path_scripts() -> list[tuple[Path, str]]:
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    scripts = [
        (RELEASE_WORKFLOW, step["run"])
        for step in release["jobs"]["release"]["steps"]
        if isinstance(step.get("run"), str) and "inputs.mode == 'promote'" not in str(step.get("if", ""))
    ]
    for workflow_path in (
        BUNDLE_WORKFLOW,
        REPO_ROOT / ".github" / "workflows" / "windows-installer.yml",
        REPO_ROOT / ".github" / "workflows" / "macos-installer.yml",
    ):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        scripts.extend(
            (workflow_path, step["run"])
            for job in workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if isinstance(step.get("run"), str)
        )
    return scripts


def test_release_cut_uses_protected_main_pr_and_exact_merge_ref() -> None:
    script = _release_job_run("Prepare exact cut commit through protected main (cut only)")
    assert "git push origin main" not in script
    assert 'BRANCH="release/version-${VERSION}"' in script
    assert 'git push --set-upstream origin "$BRANCH"' in script
    assert "gh pr create" in script
    assert "mergeCommit.oid" in script
    assert 'git fetch origin "$CUT_SHA"' in script
    assert 'git checkout --detach "$CUT_SHA"' in script
    assert 'gh workflow run "CI: Fast Validation"' in script
    assert "gh workflow run pr-review.yml" in script


def test_release_cut_contracts_are_scanned_across_complete_cut_workflow_graph() -> None:
    scripts = _cut_path_scripts()
    assert {path for path, _script in scripts} == {
        RELEASE_WORKFLOW,
        BUNDLE_WORKFLOW,
        REPO_ROOT / ".github" / "workflows" / "windows-installer.yml",
        REPO_ROOT / ".github" / "workflows" / "macos-installer.yml",
    }
    _assert_protected_cut_commands_are_safe("\n".join(script for _path, script in scripts))


@pytest.mark.parametrize(
    "script",
    [
        "echo x && git push origin main",
        "echo x && gh pr merge 1",
        "if (git push origin main); then exit 1; fi",
        "x=$(git push origin main)",
        "if ! git push origin main; then exit 1; fi",
        "git push origin branch --force",
        "gh pr merge --admin 123",
    ],
)
def test_protected_cut_scanner_rejects_nested_release_bypasses(script: str) -> None:
    with pytest.raises(AssertionError, match="protected release cut bypasses"):
        _assert_protected_cut_commands_are_safe(script)


def test_release_cut_tags_exact_merged_commit_after_schema_gate() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    names = _step_names(workflow["jobs"]["release"]["steps"])
    prepare = names.index("Prepare exact cut commit through protected main (cut only)")
    schema = names.index("Assert schema-freshness gate green on exact cut/recut commit")
    cut = names.index("Cut release candidate and freeze main")
    assert prepare < schema < cut
    assert workflow["jobs"]["release"]["steps"][schema]["id"] == "schema-freshness"
    assert "steps.cut-commit.outputs.cut_sha" in workflow["jobs"]["release"]["steps"][schema]["env"]["CUT_SHA"]
    assert 'echo "cut_sha=$(git rev-parse HEAD)"' in workflow["jobs"]["release"]["steps"][cut]["run"]


def test_release_cut_cleanup_is_failure_safe() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    names = _step_names(workflow["jobs"]["release"]["steps"])
    for name in (
        "Clean up publication hold after failed cut preparation",
        "Clean up publication hold after failed release cut",
    ):
        step = workflow["jobs"]["release"]["steps"][names.index(name)]
        assert "always()" in step["if"]
        assert "steps.publication-hold.outputs.established == 'true'" in step["if"]
        assert "git push origin --delete release-publication-hold" in step["run"]


def _promote_step() -> dict[str, Any]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    return steps[_step_names(steps).index("Promote to GA and lift freeze")]


def test_release_promote_uses_a_single_protected_pr_and_exact_merged_sha() -> None:
    script = _promote_step()["run"]

    assert "git push origin main" not in script
    assert 'BRANCH="release/promote-${VERSION}"' in script
    assert 'gh pr list --repo "${GITHUB_REPOSITORY}" --head "$BRANCH" --base main --state open' in script
    assert "closed PR" in script
    assert 'git push --set-upstream origin "$BRANCH"' in script
    assert 'gh pr create --repo "${GITHUB_REPOSITORY}"' in script
    assert 'gh workflow run "CI: Fast Validation"' in script
    assert "gh workflow run pr-review.yml" in script
    assert "MAX_POLLS=360" in script
    assert "sleep 15" in script
    assert 'MERGED_SHA="$(jq -r \'.mergeCommit.oid // empty\' <<<"$PR_JSON")"' in script
    assert 'git fetch origin "$MERGED_SHA"' in script
    assert 'git checkout --detach "$MERGED_SHA"' in script
    assert 'git tag -a "$GA_TAG"' in script
    assert 'git push origin "$GA_TAG"' in script
    assert "release-freeze remains active until all publication steps finish" in script
    assert "release-freeze" in script
    assert "promote_sha=$MERGED_SHA" in script


def _git(cwd: Path, *args: str) -> str:
    git = which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - fixed executable from PATH and test-controlled arguments
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _promote_harness(
    tmp_path: Path, *, scenario: str, ci_sha: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, str, str, dict[str, str]]:
    root = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "base")
    _git(root, "tag", "-a", "v1.2.2", "-m", "previous GA")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    _git(root, "add", "CHANGELOG.md")
    _git(root, "commit", "-m", "feat: release change (#123)")
    rc_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", "-a", "v1.2.3-rc1", "-m", "rc")
    _git(root, "tag", "-a", "validated/v1.2.3-rc1", "-m", "validated")
    _git(root, "tag", "-a", "release-freeze", "-m", "freeze")
    _git(root, "init", "--bare", str(origin))
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "origin", "main", "--tags")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then printf 'gh version test\\n'; exit 0; fi
branch_sha() { git rev-parse "refs/remotes/origin/release/promote-1.2.3"; }
case "${1:-}" in
  pr)
    case "${2:-}" in
      list)
        if [[ "$*" == *"--state open"* ]]; then
          [[ "${SCENARIO}" == valid ]] && printf '\\n' || printf '42\\n'
        else
          printf '\\n'
        fi
        ;;
      view)
        if [[ "$*" == *"labels,title,url"* ]]; then
          printf '%s\\n' '{"labels":[{"name":"changelog: feature"}],"title":"Release change","url":"https://github.example/pull/123"}'
          exit 0
        fi
        count_file="$HARNESS_DIR/pr-view-count"
        count=0; [[ -f "$count_file" ]] && count=$(cat "$count_file")
        count=$((count + 1)); printf '%s' "$count" > "$count_file"
        if [[ "$count" -eq 1 ]]; then
          current_sha=$(branch_sha)
          [[ "${SCENARIO}" == stale ]] && current_sha="$PR_SHA"
          printf '%s\\n' '{"state":"OPEN","headRefName":"release/promote-1.2.3",'\\
            '"baseRefName":"main","headRefOid":"'"$current_sha"'"}'
        else
          current_sha=$(branch_sha)
          [[ "${SCENARIO}" == stale ]] && current_sha="$PR_SHA"
          tree=$(git rev-parse "$current_sha^{tree}")
          merged=$(printf 'Merge pull request #42\\n\\n' | git commit-tree "$tree" -p "$RC_SHA" -p "$current_sha")
          git update-ref refs/heads/merge-result "$merged"
          git push origin refs/heads/merge-result >/dev/null
          printf '%s\\n' '{"state":"MERGED","mergedAt":"2026-01-01T00:00:00Z",'\\
            '"headRefOid":"'"$current_sha"'","mergeCommit":{"oid":"'"$merged"'"}}'
        fi
        ;;
      create) printf 'https://github.example/pull/42\\n' ;;
    esac
    ;;
   api) branch_sha ;;
   release)
     case "${2:-}" in
       view) exit 1 ;;
       create|edit)
         if [[ "${FAIL_PUBLICATION_BLOCK:-}" == notes ]]; then
           printf 'release publication failure\n' >&2
           exit 1
         fi
         title=""
         notes_file=""
         tag="${3:-}"
         while (($#)); do
           case "$1" in
             --title) title="$2"; shift 2 ;;
             --notes-file) notes_file="$2"; shift 2 ;;
             *) shift ;;
           esac
         done
         printf 'tag=%s\ntitle=%s\n' "$tag" "$title" > "$HARNESS_DIR/release-metadata"
         cp "$notes_file" "$HARNESS_DIR/release-notes"
         printf 'release publication marker\n' >> "$HARNESS_DIR/publication-markers"
         ;;
        *) ;;
      esac
     ;;

   workflow) ;;


   run)

    case "${2:-}" in
      list) printf '[{"databaseId":7,"headSha":"%s","createdAt":"2026-01-01T00:00:00Z"}]\\n' "$(branch_sha)" ;;
      watch) ;;
      view) printf '{"headSha":"%s","status":"completed","conclusion":"success"}\\n' "${CI_SHA:-$(branch_sha)}" ;;
    esac
    ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
freeze_marker() {
  git -C "$ROOT" rev-parse -q --verify refs/tags/release-freeze >/dev/null 2>&1 || {
    printf 'freeze missing for docker\\n' >&2
    exit 1
  }
  printf 'freeze-present:docker\\n' >> "$HARNESS_DIR/publication-markers"
}
freeze_marker
if [[ "${1:-}" == login ]]; then
  printf 'docker login marker\\n' >> "$HARNESS_DIR/publication-markers"
  exit 0
fi
          if [[ "${FAIL_PUBLICATION_BLOCK:-}" == retag ]]; then
            printf 'docker retag failure\\n' >&2
            exit 1
          fi

if [[ "${1:-}" == buildx && "${2:-}" == imagetools && "${3:-}" == create ]]; then
  if [[ "${FAIL_PUBLICATION_BLOCK:-}" == retag ]]; then
    printf 'docker retag failure\\n' >&2
    exit 1
  fi
  printf 'docker retag marker %s\\n' "$*" >> "$HARNESS_DIR/publication-markers"
  exit 0
fi
if [[ "${1:-}" == buildx && "${2:-}" == imagetools && "${3:-}" == inspect ]]; then
  printf 'sha256:%064d\\n' 1
  exit 0
fi
printf 'unexpected docker invocation: %s\\n' "$*" >&2
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    helm = bin_dir / "helm"
    helm.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
git -C "$ROOT" rev-parse -q --verify refs/tags/release-freeze >/dev/null 2>&1 || {
  printf 'freeze missing for helm\\n' >&2
  exit 1
}
printf 'freeze-present:helm\\n' >> "$HARNESS_DIR/publication-markers"
case "${1:-}" in
  registry) printf 'helm login marker\\n' >> "$HARNESS_DIR/publication-markers" ;;
  package)
    destination=""
    version=""
    while (($#)); do
      case "$1" in
        --destination) destination="$2"; shift 2 ;;
        --version) version="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    mkdir -p "$destination"
    : > "$destination/modulo-${version}.tgz"
    printf 'helm package marker %s\\n' "$version" >> "$HARNESS_DIR/publication-markers"
    ;;
  push)
    if [[ "${FAIL_PUBLICATION_BLOCK:-}" == helm ]]; then
      printf 'helm push failure\\n' >&2
      exit 1
    fi
    printf 'helm push marker\\n' >> "$HARNESS_DIR/publication-markers"
    ;;
  *) printf 'unexpected helm invocation: %s\\n' "$*" >&2; exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    helm.chmod(0o755)

    python3 = bin_dir / "python3"
    python3.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
git -C "$ROOT" rev-parse -q --verify refs/tags/release-freeze >/dev/null 2>&1 || {
  printf 'freeze missing for notes\\n' >&2
  exit 1
}
printf 'freeze-present:notes\\n' >> "$HARNESS_DIR/publication-markers"
exec /usr/bin/python3 "$@"
""",
        encoding="utf-8",
    )
    python3.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "example/repo",
        "VERSION": "1.2.3",
        "SCENARIO": scenario,
        "ROOT": str(root),
        "RC_SHA": rc_sha,
        "BRANCH_SHA": rc_sha,
        "PR_SHA": rc_sha,
        "GIT_DIR": str(root / ".git"),
        "GITHUB_OUTPUT": str(root / "github-output.txt"),
        "HARNESS_DIR": str(tmp_path),
    }
    if scenario != "valid":
        _git(root, "checkout", "-b", "release/promote-1.2.3", rc_sha)
        changelog = "# Changelog\n\n## [Unreleased]\n"
        if scenario == "wrong-heading":
            changelog += "\n## [1.2.4] — 2026-01-01\n\n### Features\n- wrong version\n"
        elif scenario == "wrong-body":
            changelog += "\n## [1.2.3] — 2026-01-01\n\n### Features\n- wrong release body\n"
        else:
            changelog += "\n## [1.2.3] — 2026-01-01\n\n### Features\n- Release change\n"
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        _git(root, "add", "CHANGELOG.md")
        _git(root, "commit", "-m", "promote metadata")
        branch_sha = _git(root, "rev-parse", "HEAD")
        _git(root, "push", "origin", "HEAD:refs/heads/release/promote-1.2.3")
        _git(root, "checkout", "main")
        env["BRANCH_SHA"] = branch_sha
        env["PR_SHA"] = branch_sha
    if scenario == "stale":
        env["PR_SHA"] = rc_sha
    if scenario == "mismatch":
        env["PR_SHA"] = rc_sha
    if ci_sha is not None:
        env["CI_SHA"] = ci_sha
    (root / "scripts").mkdir()
    generator = Path(__file__).resolve().parents[3] / "scripts" / "generate_changelog.py"
    (root / "scripts" / "generate_changelog.py").write_text(generator.read_text(encoding="utf-8"), encoding="utf-8")
    scripts = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    promote = next(step["run"] for step in scripts if step.get("name") == "Promote to GA and lift freeze")
    bash = which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - fixed shell and workflow text under test
        [bash, "-eo", "pipefail", "-c", promote],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    _git(root, "push", "origin", "refs/tags/release-freeze:refs/tags/release-freeze")
    return result, root, rc_sha, env["PR_SHA"], env


def _run_release_block(
    root: Path,
    env: dict[str, str],
    name: str,
    failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    steps = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    script = next(step["run"] for step in steps if step.get("name") == name)
    script = script.replace("${{ steps.promote.outputs.rc_tag }}", '"$RC_TAG"')
    script = script.replace("${{ steps.promote.outputs.ga_tag }}", '"$GA_TAG"')
    script = script.replace("${{ steps.promote.outputs.promote_sha }}", '"$PROMOTE_SHA"')
    block_env = {
        **env,
        "FAIL_PUBLICATION_BLOCK": "" if failure is None else failure,
        "GH_TOKEN": "test",
        "REGISTRY_USER": "test-user",
        "IMAGE_NAMESPACE": "example/repo",
        "RC_TAG": "v1.2.3-rc1",
        "GA_TAG": "v1.2.3",
        "PROMOTE_SHA": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "GITHUB_OUTPUT": str(root / "publication-output.txt"),
    }
    return subprocess.run(  # noqa: S603 - fixed shell and workflow text under test
        [which("bash") or "bash", "-eo", "pipefail", "-c", script],
        cwd=root,
        env=block_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _freeze_lift_script() -> str:
    steps = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    return next(
        step["run"] for step in steps if step.get("name") == "Lift release freeze after successful GA publication"
    )


def _run_publication_sequence(
    root: Path, env: dict[str, str], *, failure: str | None = None
) -> subprocess.CompletedProcess[str]:
    block_names = (
        "Retag container images for GA",
        "Push Helm chart to OCI registry",
        "Update CHANGELOG.md and GitHub Release for GA",
    )
    blocks = []
    workflow_steps = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    for block_name in block_names:
        script = next(step["run"] for step in workflow_steps if step.get("name") == block_name)
        blocks.append(
            script.replace("${{ steps.promote.outputs.rc_tag }}", '"$RC_TAG"')
            .replace("${{ steps.promote.outputs.ga_tag }}", '"$GA_TAG"')
            .replace("${{ steps.promote.outputs.promote_sha }}", '"$PROMOTE_SHA"')
        )
    recovery = _release_step("Re-establish release freeze after failed GA publication")["run"]
    sequence = "\n\n".join(
        [
            "failure=0",
            f"if !(\n{blocks[0]}\n); then failure=1; fi",
            f"if (( failure == 0 )); then\nif ! (\n{blocks[1]}\n); then failure=1; fi\nfi",
            f"if (( failure == 0 )); then\nif ! (\n{blocks[2]}\n); then failure=1; fi\nfi",
            f"if (( failure != 0 )); then\n{recovery}\nfi",
        ]
    )

    sequence_env = {
        **env,
        "FAIL_PUBLICATION_BLOCK": "" if failure is None else failure,
        "GH_TOKEN": "test",
        "REGISTRY_USER": "test-user",
        "IMAGE_NAMESPACE": "example/repo",
        "RC_TAG": "v1.2.3-rc1",
        "GA_TAG": "v1.2.3",
        "PROMOTE_SHA": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "GITHUB_OUTPUT": str(root / "publication-output.txt"),
    }
    return subprocess.run(  # noqa: S603 - workflow blocks are repository-owned test fixtures
        [which("bash") or "bash", "-eo", "pipefail", "-c", sequence],
        cwd=root,
        env=sequence_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_release_promote_successfully_publishes_exact_tag_and_lifts_freeze(tmp_path: Path) -> None:
    result, root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path, scenario="valid")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "GA tag v1.2.3 published at" in result.stdout
    assert "release-freeze remains active" in result.stdout
    markers_path = root.parent / "publication-markers"
    markers_path.write_text("", encoding="utf-8")
    publication = _run_publication_sequence(root, env)
    assert publication.returncode == 0, publication.stderr + publication.stdout
    markers = (root.parent / "publication-markers").read_text(encoding="utf-8").splitlines()
    release_metadata = (root.parent / "release-metadata").read_text(encoding="utf-8").splitlines()
    assert release_metadata == ["tag=v1.2.3", "title=v1.2.3"]
    release_notes = (root.parent / "release-notes").read_text(encoding="utf-8")
    assert "## [1.2.3]" in release_notes
    assert "Release change" in release_notes
    assert markers.count("freeze-present:docker") >= 1
    assert markers.count("freeze-present:helm") >= 1
    assert markers.count("freeze-present:notes") >= 1
    assert markers.index("freeze-present:docker") < markers.index("freeze-present:helm")
    assert markers.index("freeze-present:helm") < markers.index("freeze-present:notes")
    output_values = dict(
        line.split("=", 1) for line in (root / "github-output.txt").read_text(encoding="utf-8").splitlines()
    )
    assert output_values == {
        "ga_tag": "v1.2.3",
        "rc_tag": "v1.2.3-rc1",
        "promote_sha": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
    }

    lift = subprocess.run(  # noqa: S603 - fixed shell and workflow text under test
        [which("bash") or "bash", "-eo", "pipefail", "-c", _freeze_lift_script()],
        cwd=root,
        env={**env, "PATH": env["PATH"], "GH_TOKEN": "test"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert lift.returncode == 0, lift.stderr + lift.stdout
    assert "freeze lifted" in lift.stdout
    remote_tag_object = _git(root, "ls-remote", "origin", "refs/tags/v1.2.3").split()[0]
    remote_ga_sha = _git(root, "cat-file", "tag", remote_tag_object).splitlines()[0].split()[1]
    assert remote_ga_sha == _git(root, "rev-parse", "v1.2.3^{commit}")
    assert not _git(root, "ls-remote", "origin", "refs/tags/release-freeze")


def test_release_promote_failure_reestablishes_freeze_without_lift(tmp_path: Path) -> None:
    for failure in ("retag", "helm", "notes"):
        result, root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path / failure, scenario="valid")
        assert result.returncode == 0, result.stderr + result.stdout
        publication = _run_publication_sequence(root, env, failure=failure)
        assert publication.returncode == 0, publication.stderr + publication.stdout
        assert _git(root, "ls-remote", "origin", "refs/tags/release-freeze")


def test_release_promote_rejects_wrong_version_heading(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="wrong-heading")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "exact generated release metadata" in output


def test_release_promote_rejects_wrong_generated_release_body(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="wrong-body")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "exact generated release metadata" in output


def test_release_promote_rejects_stale_reused_pr_head(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="stale")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "does not match release branch" in output


def test_release_promote_rejects_old_ci_head(tmp_path: Path) -> None:
    old_ci_sha = "0" * 40
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="valid", ci_sha=old_ci_sha)
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "did not succeed for exact promote PR head" in output


def test_release_promote_shell_fragments_are_executable_bash() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    names = _step_names(steps)
    for name in (
        "Promote to GA and lift freeze",
        "Re-establish release freeze after failed GA publication",
        "Lift release freeze after successful GA publication",
    ):
        script = steps[names.index(name)]["run"]
        bash = which("bash")
        assert bash is not None
        result = subprocess.run(  # noqa: S603 - fixed executable from PATH; workflow text is syntax-checked only
            [bash, "-n"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            timeout=30,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_release_promote_reuses_only_exact_current_rc_metadata_pr() -> None:
    script = _promote_step()["run"]

    assert 'PR_HEAD_REF="$(jq -r' in script
    assert 'PR_BASE_REF="$(jq -r' in script
    assert 'PR_STATE="$(jq -r' in script
    assert 'BRANCH_SHA="$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${BRANCH}"' in script
    assert 'git merge-base --is-ancestor "$RC_SHA" "$PR_SHA"' in script
    assert 'git diff --quiet "${RC_SHA}..${PR_SHA}" -- CHANGELOG.md' in script
    assert "refusing stale or externally modified PR" in script
    assert "refusing stale PR reuse" in script


def test_release_promote_requires_exact_head_ci_success_before_merge() -> None:
    script = _promote_step()["run"]

    assert 'gh run list --repo "${GITHUB_REPOSITORY}" --workflow "CI: Fast Validation"' in script
    assert "select(.headSha == $sha)" in script
    assert 'gh run watch "$CI_RUN_ID" --repo "${GITHUB_REPOSITORY}" --exit-status' in script
    assert 'CI_JSON="$(gh run view "$CI_RUN_ID"' in script
    assert 'jq -r \'.conclusion\' <<<"$CI_JSON")" != "success"' in script
    assert 'CURRENT_PR_SHA="$(jq -r' in script
    assert "changed head from ${PR_SHA}" in script


def test_release_promote_freeze_cleanup_is_final_and_failure_safe() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    names = _step_names(steps)
    promote_idx = names.index("Promote to GA and lift freeze")
    retag_idx = names.index("Retag container images for GA")
    helm_idx = names.index("Push Helm chart to OCI registry")
    notes_idx = names.index("Update CHANGELOG.md and GitHub Release for GA")
    reestablish_idx = names.index("Re-establish release freeze after failed GA publication")
    lift_idx = names.index("Lift release freeze after successful GA publication")

    assert promote_idx < retag_idx < helm_idx < notes_idx < reestablish_idx < lift_idx
    assert steps[reestablish_idx]["if"] == "inputs.mode == 'promote' && failure()"
    assert steps[lift_idx]["if"] == "inputs.mode == 'promote' && success()"
    assert "git push origin --delete release-freeze" not in steps[promote_idx]["run"]
    assert "git push origin --delete release-freeze" in steps[lift_idx]["run"]
    assert "git push origin release-freeze --force" in steps[reestablish_idx]["run"]


def test_release_promote_has_no_bypass_or_silent_fallback() -> None:
    script = _promote_step()["run"]

    assert "--admin" not in script
    assert "--force" not in script
    assert "|| true" not in script
    assert "git push origin main" not in script
    assert "git push --force origin main" not in script
    assert "gh pr merge" not in script
    assert "closed PR" in script
    assert "closed without merging" in script
    assert "Timed out waiting for promote PR" in script
    assert 'if [ "$(git rev-parse HEAD)" != "$MERGED_SHA" ]; then' in script
    assert 'if [ "$TAG_SHA" != "$MERGED_SHA" ]; then' in script


def test_release_promote_wait_budget_fits_release_job_timeout() -> None:
    script = _promote_step()["run"]
    max_polls = int(re.search(r"MAX_POLLS=(\d+)", script).group(1))
    poll_seconds = int(re.search(r"sleep (\d+)", script).group(1))
    release_job = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]

    assert max_polls * poll_seconds == 3600
    assert max_polls * poll_seconds <= release_job["timeout-minutes"] * 60


def test_release_promote_bypass_scan_excludes_cut_path() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    promote_scripts = [step["run"] for step in steps if "promote" in step.get("if", "") and "run" in step]
    assert len(promote_scripts) >= 4

    for script in promote_scripts:
        assert "--admin" not in script
        assert "git push origin main" not in script
        assert "git push --force origin main" not in script
        assert "git checkout -B main origin/main" not in script

    promote = _promote_step()["run"]
    assert "release-freeze remains active until all publication steps finish" in promote
