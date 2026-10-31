"""Structural gate: deploy.yml must wire the migration rehearsal into BOTH
deploy legs (FAR-717).

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps the rehearsal-first
pre-deploy wiring intact — the same reasoning as the sibling
``test_extract_machine_id.py`` for the machine-id parser.
"""

import importlib.util
import re
import textwrap
from pathlib import Path
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


def test_release_cut_uses_protected_main_pr_and_exact_merge_ref() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    names = _step_names(steps)
    cut = steps[names.index("Prepare exact cut commit through protected main (cut only)")]
    script = cut["run"]

    assert "git push origin main" not in script
    assert 'git push --set-upstream origin "$BRANCH"' in script
    assert "gh pr create" in script
    assert 'gh pr view "$PR_NUMBER"' in script
    assert "mergeCommit.oid" in script
    assert 'git fetch origin "$CUT_SHA"' in script
    assert 'git checkout --detach "$CUT_SHA"' in script
    assert "MERGED_VERSION=" in script
    assert 'gh workflow run "CI: Fast Validation"' in script
    assert "gh workflow run pr-review.yml" in script

    cleanup_names = [name for name in names if "Clean up publication hold" in name]
    assert len(cleanup_names) == 2
    for cleanup_name in cleanup_names:
        cleanup = steps[names.index(cleanup_name)]
        assert "always()" in cleanup["if"]
        assert "steps.publication-hold.outcome == 'success'" in cleanup["if"]
        assert "git push origin --delete release-publication-hold" in cleanup["run"]
    failed_cut_cleanup = steps[names.index("Clean up publication hold after failed release cut")]
    assert "steps.cut.outcome != 'success'" in failed_cut_cleanup["if"]


def test_release_cut_tags_the_exact_merged_commit_after_schema_gate() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    names = _step_names(steps)
    prepare_idx = names.index("Prepare exact cut commit through protected main (cut only)")
    schema_idx = names.index("Assert schema-freshness gate green on exact cut/recut commit")
    cut_idx = names.index("Cut release candidate and freeze main")
    assert prepare_idx < schema_idx < cut_idx
    assert steps[schema_idx]["id"] == "schema-freshness"
    assert "steps.cut-commit.outputs.cut_sha" in steps[schema_idx]["env"]["CUT_SHA"]
    assert 'echo "cut_sha=$(git rev-parse HEAD)"' in steps[cut_idx]["run"]
