"""Structural gate: deploy.yml must wire the migration rehearsal into BOTH
deploy legs (FAR-717).

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps the rehearsal-first
pre-deploy wiring intact — the same reasoning as the sibling
``test_extract_machine_id.py`` for the machine-id parser.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
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

#: This module EXECUTES the workflows' shipped shell fragments under a real
#: POSIX shell, so it needs a POSIX shell rather than the Windows WSL launcher
#: stub. The platform requirement is declared once, as a marker; the gate that
#: skips it on Windows lives in ``backend/tests/conftest.py``
#: (``POSIX_ONLY_MARKER``) and Linux CI executes this suite for real.
pytestmark = pytest.mark.posix_only

_REHEARSAL_STEP_NAME = "Pre-deploy REHEARSAL migrations (one-off machine — always rolls back)"
_REAL_STEP_NAME = "Pre-deploy migrations (one-off machine)"
_REHEARSAL_DESTROY_NAME = "Destroy pre-rehearsal machine"

#: (job id, fly app, artifact suffix, fly-logs app flag)
_LEGS = (
    ("deploy-staging", "staging-modulo", "staging"),
    ("deploy-production", "app-modulo", "prod"),
)

#: This module executes the shipped deploy/release shell scripts for real in a
#: subprocess, with stubbed binaries placed first on PATH - so it needs
#: executable-bit semantics, a POSIX PATH the shell can read, and ``jq``, none
#: of which a Windows host provides (``bash`` resolves to the WSL stub and the
#: stubs are unreadable to it). The platform requirement is declared once, as a
#: marker; the gate that skips it on Windows lives in ``backend/tests/conftest.py``
#: (``POSIX_ONLY_MARKER``) and Linux CI executes this suite for real.
pytestmark = pytest.mark.posix_only


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


def test_promote_rebuilds_compose_bundle_from_ga_tag_and_replaces_release_assets() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["promote-compose"]
    assert job["needs"] == "release"
    assert job["if"] == "inputs.mode == 'promote'"
    names = _step_names(job["steps"])
    pull_idx = names.index("Verify the GA image is anonymously pullable")
    package_idx = names.index("Package final GA compose release bundle")
    sign_idx = names.index("Generate SHA256SUMS and sign the final GA manifest")
    upload_idx = names.index("Replace GA compose assets without clobbering")
    assert pull_idx < package_idx < sign_idx < upload_idx

    pull_script = job["steps"][pull_idx]["run"]
    assert 'docker pull "$IMAGE"' in pull_script
    assert 'IMAGE="ghcr.io/${{ github.repository }}:v${VERSION}"' in pull_script
    # The pull must be anonymous: a credential would let a private image look
    # publishable and the bundle would fail on the evaluator's clean host.
    assert 'export DOCKER_CONFIG="$EMPTY_DOCKER_CONFIG"' in pull_script
    assert "docker login" not in pull_script

    package_script = job["steps"][package_idx]["run"]
    assert '--tag "$GA_TAG"' in package_script
    assert '--tag "$RC_TAG"' not in package_script

    sign_script = job["steps"][sign_idx]["run"]
    assert 'sha256sum "modulo-${VERSION}-compose.tar.gz" > SHA256SUMS' in sign_script
    assert "RELEASE_MANIFEST.json.sig" in sign_script
    assert "sha256sum --check SHA256SUMS" in sign_script

    upload_script = job["steps"][upload_idx]["run"]
    assert 'gh release view "$TAG"' in upload_script
    assert 'gh release upload "$TAG"' in upload_script
    assert "modulo-${VERSION}-compose.tar.gz" in upload_script


def test_promote_retag_publishes_the_v_prefixed_ga_tag_the_compose_bundle_pins() -> None:
    """The GA compose bundle pins ``vX.Y.Z``, so promotion must create that tag.

    Without the v-prefixed stable tag the promoted bundle would reference an
    image that does not exist, and the promote-mode anonymous-pull gate would
    (correctly) fail the release. The bare semver tag stays because the Helm
    chart's ``appVersion`` resolves to it.
    """
    retag = _release_job_run("Retag container images for GA")

    assert '--tag "ghcr.io/$image_name:v$VERSION_NUM"' in retag
    assert '--tag "ghcr.io/$image_name:$VERSION_NUM"' in retag
    assert '--tag "ghcr.io/$image_name:latest"' in retag
    # The fallback path must publish the same tag set, or a retag that degrades
    # to pull/tag/push would silently drop the tag the GA bundle pins.
    assert 'docker push "ghcr.io/$image_name:v$VERSION_NUM"' in retag
    assert 'docker push "ghcr.io/$image_name:$VERSION_NUM"' in retag
    assert 'docker push "ghcr.io/$image_name:latest"' in retag


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
        assert scripts, f"expected at least one signing script in {workflow_path}"
        for script in scripts:
            compile(_python_heredoc(script, marker), str(workflow_path), "exec")


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
    ("windows-native-artifacts", "./.github/workflows/windows-artifacts.yml"),
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
    assert job["needs"] == ["produce-sbom", "final-release-manifest", "release", "windows-installer"]
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


def test_bundle_release_builds_relocatable_postgres_with_runtime_data() -> None:
    workflow_text = BUNDLE_WORKFLOW.read_text(encoding="utf-8")
    versions = (REPO_ROOT / "scripts" / "bundle-versions.env").read_text(encoding="utf-8")

    assert "Install pinned PostgreSQL source-build dependencies" in workflow_text
    assert "build-essential" in workflow_text
    assert "bison" in workflow_text
    assert "flex" in workflow_text
    assert "Download and sha256-verify pinned PostgreSQL source" in workflow_text
    assert 'echo "${PG_SOURCE_SHA256}  postgresql-${PG_VERSION}.tar.gz"' in workflow_text
    assert "Build and sha256-verify PostgreSQL from pinned upstream source" in workflow_text
    assert "../pg-src/configure" in workflow_text
    assert 'BUNDLE_PG_PREFIX="$PWD/build/${BUNDLE_ROOT}/pg"' in workflow_text
    assert '--prefix="$BUNDLE_PG_PREFIX"' in workflow_text
    assert "--without-llvm" in workflow_text
    assert "PG_REQUIRED_EXTENSIONS=" in versions
    assert 'test -z "${PG_REQUIRED_EXTENSIONS}"' in workflow_text
    assert 'test -f "$BUNDLE_PG_PREFIX/share/postgresql/postgres.bki"' in workflow_text
    assert 'test -d "$BUNDLE_PG_PREFIX/share/postgresql/timezonesets"' in workflow_text
    assert "pg_config --sharedir" in workflow_text
    assert 'test -f "$BUNDLE/pg/share/postgresql/postgres.bki"' in workflow_text
    assert 'test -d "$BUNDLE/pg/share/postgresql/timezonesets"' in workflow_text
    assert 'export PATH="$BUNDLE_ROOT/runtime/bin:$BUNDLE_ROOT/pg/bin:$BUNDLE_ROOT/redis' in workflow_text
    assert "postgresql-16.deb" not in workflow_text
    assert "apt.postgresql.org" not in workflow_text


def test_bundle_release_proves_final_postgres_tarball_runtime() -> None:
    workflow = yaml.safe_load(BUNDLE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["bundle"]["steps"]
    names = _step_names(steps)
    smoke_name = "Smoke the final PostgreSQL tarball after relocation as an unprivileged user"
    smoke_script = steps[names.index(smoke_name)]["run"]

    assert 'tarball="build/${TARBALL_NAME}"' in smoke_script
    assert 'test ! -e "$original_bundle"' in smoke_script
    assert 'tar -xzf "$tarball"' in smoke_script
    assert "useradd --system" in smoke_script
    assert "env -i" in smoke_script
    assert 'PATH="$smoke_bundle/pg/bin:/usr/bin:/bin"' in smoke_script
    assert "pg_config --sharedir" in smoke_script
    assert "initdb --pgdata" in smoke_script
    assert "pg_ctl --pgdata" in smoke_script
    assert "SELECT 1;" in smoke_script
    assert "America/New_York" in smoke_script
    assert "pg_dump" in smoke_script
    assert "dropdb" in smoke_script
    assert "stop -m fast -w" in smoke_script
    assert "host PostgreSQL path" in smoke_script


def test_bundle_release_scans_postgres_elf_paths_and_keeps_glibc_gate() -> None:
    workflow_text = BUNDLE_WORKFLOW.read_text(encoding="utf-8")
    assert "Scan shipped PostgreSQL ELF files for build and host paths" in workflow_text
    assert 'strings -a "$binary" | grep -Fq "$build_prefix"' in workflow_text
    assert "unexpected absolute PostgreSQL path" in workflow_text
    assert 'check_binary "$BUNDLE/pg/bin/postgres"' in workflow_text
    assert 'check_binary "$BUNDLE/pg/bin/pg_dump"' in workflow_text


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


def test_windows_bundle_is_scoped_into_the_alpha_release_path() -> None:
    """Windows is Alpha scope (FAR-2338): the installer feeds the manifest.

    The final manifest downloads ``windows-installer-<version>`` the moment
    ``include_windows`` is true, so it must DEPEND on the job that produces that
    artefact — otherwise the download races the producer and the Alpha release
    fails with a missing-artefact error. Publication still only requires the
    manifest, so a Windows failure surfaces as a failed manifest rather than an
    artefact-less publication.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    # The manifest depends on the installer, gated so include_windows=false still works.
    manifest_job = workflow["jobs"]["final-release-manifest"]
    assert "windows-installer" in manifest_job["needs"]
    assert "needs.windows-installer.result == 'success'" in manifest_job["if"]
    # publish-release consumes the manifest, never the installer directly.
    assert "windows-installer" not in workflow["jobs"]["publish-release"]["needs"]

    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    final_names = _step_names(final_steps)
    build_script = final_steps[final_names.index("Build final signed manifest after every platform artifact")]["run"]

    # Windows must only appear inside the INCLUDE_WINDOWS conditional block
    assert 'if [ "$INCLUDE_WINDOWS" = "true" ]; then' in build_script
    # The unconditional path must not have Windows
    assert "modulo-${VERSION}-linux-amd64.tar.gz" in build_script
    assert "Modulo-${VERSION}-macos-arm64.dmg" in build_script
    assert "Modulo-${VERSION}-macos-x86_64.dmg" in build_script
    # The components dict starts with linux+macos only
    assert 'components = {"linux": "amd64", "macos": "native"}' in build_script
    # Windows is only added conditionally
    assert 'components["windows"] = "amd64"' in build_script
    assert 'names.append(f"modulo-{version}-windows-amd64.exe")' in build_script


def test_alpha_bundle_still_requires_the_macos_installer() -> None:
    """Guard the converse: dropping Windows must not silently drop macOS."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    assert "macos-installer" in workflow["jobs"]["final-release-manifest"]["needs"]
    assert "macos-installer" in workflow["jobs"]["publish-release"]["needs"]

    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    downloads = [
        step.get("with", {}).get("name") for step in final_steps if "download-artifact" in str(step.get("uses", ""))
    ]
    assert "macos-installer-${{ inputs.version }}-arm64" in downloads
    assert "macos-installer-${{ inputs.version }}-x86_64" in downloads


def test_publish_release_assets_match_the_alpha_bundle() -> None:
    """The published asset list must contain exactly what the bundle produces.

    The manifest job no longer downloads the Windows exe by default, so a stale Windows
    entry in either the presence check or the upload list would fail the
    publication step it is meant to guard. Windows is only included when
    inputs.include_windows=true (Beta path).
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-release"]["steps"]
    names = _step_names(steps)

    for step_name in ("Verify portable release artefacts", "Create GitHub Release with assets"):
        script = steps[names.index(step_name)]["run"]
        # Windows must only appear inside the INCLUDE_WINDOWS conditional
        assert 'if [ "$INCLUDE_WINDOWS" = "true" ]; then' in script
        assert "modulo-${VERSION}-linux-amd64.tar.gz" in script
        assert "Modulo-${VERSION}-macos-arm64.dmg" in script
        assert "Modulo-${VERSION}-macos-x86_64.dmg" in script


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


def test_release_cut_state_gate_checks_all_retry_safety_conditions() -> None:
    script = _cut_state_script()
    assert "gh api --paginate --slurp" in script
    assert "pulls?state=all&per_page=100" in script
    assert ".title == $title" in script
    assert "--state open" not in script
    assert "--state closed" not in script
    assert "merged_at != null" in script
    assert 'require_remote_ref_absent "refs/heads/${BRANCH}"' in script
    assert 'require_remote_ref_absent "refs/heads/release-publication-hold"' in script
    assert 'REMOTE_REF_STATUS="active"' in script
    assert 'REMOTE_REF_STATUS="error"' in script
    assert 'require_remote_ref_absent "refs/tags/v${VERSION}"' in script
    assert 'require_remote_ref_absent "refs/tags/bundle-v${VERSION}"' in script
    assert 'require_remote_ref_absent "refs/tags/v${VERSION}-rc*"' in script
    assert "gh release list" in script
    assert "Safe retry: prior closed unmerged release PR(s)" in script
    assert "Could not query" in script


def test_release_cut_state_gate_fails_closed_on_ref_query_errors() -> None:
    script = _cut_state_script()
    assert 'if [ "$rc" -ne 2 ]; then' in script
    assert "refusing to treat it as absent" in script


# POSIX-only (this module is mixed): the tests below execute the shipped shell
# script for real, so each carries ``pytest.mark.posix_only``. The gate that skips
# them on Windows lives in ``backend/tests/conftest.py``; the module's remaining
# tests assert on workflow/file text and still run on Windows.
@pytest.mark.posix_only
def test_cut_state_gate_rejects_remote_ref_query_error(tmp_path: Path) -> None:
    result = _run_cut_state_gate(tmp_path, git_failure=True)
    assert result.returncode != 0
    assert "Could not query remote ref" in result.stdout
    assert "remote query failed" in result.stdout + result.stderr


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


def test_release_pr_driving_steps_use_the_trusted_bot_token() -> None:
    """Every release PR-driving step must run as the trusted ``farnalabs`` bot.

    Regression gate for FAR-2213. The cut PR (#2649, run 41228054548) was
    created with ``github.token``: GitHub reported ``author_association: NONE``,
    so ``pr-review.yml`` skipped the review dispatch and ``merge-queue.yml``
    skipped the PR — the release could never merge. The secret is scoped to the
    specific steps that need it (never exported at job level) and each step
    fails closed when it is missing, because a silent fallback to
    ``github.token`` would recreate the exact bug.

    The scan covers every job and mode, so a promote-mode PR step added later
    is gated automatically.
    """
    driving = _release_pr_driving_steps()
    assert driving, "expected at least one step that drives the release PR"
    assert any("Prepare exact cut commit" in label for label, _step in driving)

    for label, step in driving:
        env = cast(dict[str, str], step.get("env") or {})
        assert env.get("GH_TOKEN") == _RELEASE_PR_TOKEN_SECRET, (
            f"{label} drives the release PR with GH_TOKEN={env.get('GH_TOKEN')!r}; "
            "a github.token PR is authored by github-actions[bot] and skipped by the same-repo author guard"
        )
        assert "GITHUB_TOKEN" not in env, f"{label} must keep the branch push on the checkout github.token credential"
        script = cast(str, step["run"])
        assert "github.token" not in script
        assert "MODULO_RELEASE_PR_TOKEN" in script, (
            f"{label} must fail closed with an ::error:: naming MODULO_RELEASE_PR_TOKEN"
        )
        assert '[ -z "${GH_TOKEN:-}" ]' in script, f"{label} must reject an empty GH_TOKEN"
        guard_index = script.index('[ -z "${GH_TOKEN:-}" ]')
        first_gh = min(script.index(command) for command in _RELEASE_PR_DRIVING_COMMANDS if command in script)
        assert guard_index < first_gh, f"{label} must check the secret before any gh call"
        assert 'echo "${GH_TOKEN}"' not in script, f"{label} must never log the token"
        assert "set -x" not in script, f"{label} must never trace the token"


#: The repository secret holding a vault-sourced PAT for the trusted bot
#: identity (`farnalabs`). ``github.token`` cannot be used for release PRs: it
#: authors as ``github-actions[bot]`` with ``author_association: NONE``.
_RELEASE_PR_TOKEN_SECRET = "${{ secrets.MODULO_RELEASE_PR_TOKEN }}"

#: Commands that author, poll, or drive the release PR. A ``github.token`` PR is
#: authored by ``github-actions[bot]`` with ``author_association: NONE``, which
#: ``.github/scripts/same-repo-author-guard.py`` treats as external — so
#: ``pr-review.yml`` skips the review dispatch and ``merge-queue.yml`` skips the
#: PR entirely, leaving it unmergeable (FAR-2213, PR #2649).
_RELEASE_PR_DRIVING_COMMANDS = (
    "gh pr create",
    "gh pr view",
    "gh workflow run pr-review.yml",
    'gh workflow run "CI: Fast Validation"',
)


def _release_workflow_steps() -> list[tuple[str, dict[str, Any]]]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    return [
        (f"{job_id}: {step.get('name', '')}", cast(dict[str, Any], step))
        for job_id, job in workflow["jobs"].items()
        for step in job.get("steps", [])
    ]


def _release_pr_driving_steps() -> list[tuple[str, dict[str, Any]]]:
    """Every step in every job/mode that creates, polls, or drives the release PR."""
    driving: list[tuple[str, dict[str, Any]]] = []
    for label, step in _release_workflow_steps():
        run = step.get("run")
        if isinstance(run, str) and any(command in run for command in _RELEASE_PR_DRIVING_COMMANDS):
            driving.append((label, step))
    return driving


def _release_step(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    return cast(dict[str, Any], steps[_step_names(steps).index(name)])


def _cut_state_script() -> str:
    run = _release_step("Validate prior release cut state (cut only)").get("run")
    assert isinstance(run, str)
    return run


def _cut_wait_budget_seconds(script: str) -> int:
    match = re.search(r"MAX_POLLS=(\d+)", script)
    assert match is not None, "cut script must define MAX_POLLS"
    return int(match.group(1)) * 15


def _schema_wait_budget_seconds(script: str) -> int:
    match = re.search(r"SCHEMA_GATE_MAX_WAIT_SECONDS:-(\d+)", script)
    assert match is not None, "schema script must define a default wait budget"
    return int(match.group(1))


def _release_job_run(name: str) -> str:
    run = _release_step(name).get("run")
    assert isinstance(run, str), f"{name!r} must contain a shell run block"
    return run


def _run_bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute release workflow contracts")
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _cut_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    assert git is not None, "git is required to execute release workflow contracts"
    return subprocess.run(  # noqa: S603 - resolved git executable and test-owned arguments
        [git, *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=True
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _cut_harness(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _cut_git("init", "--bare", str(remote))
    _cut_git("init", "-b", "main", str(repo))
    for relative, content in (
        ("backend/pyproject.toml", '[project]\nversion = "0.1.0"\n'),
        (
            "backend/uv.lock",
            'version = 1\nrevision = 5\n\n[[package]]\nname = "farnalabs-modulo"\n'
            'version = "0.1.0"\nsource = { editable = "." }\n',
        ),
        ("frontend/package.json", '{"version": "0.1.0"}\n'),
        ("deploy/helm/modulo/Chart.yaml", 'version: 0.1.0\nappVersion: "0.1.0"\n'),
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _cut_git("add", ".", cwd=repo)
    _cut_git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base", cwd=repo)
    _cut_git("remote", "add", "origin", str(remote), cwd=repo)
    _cut_git("push", "origin", "main", cwd=repo)
    initial_sha = _cut_git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    for relative in (
        "backend/pyproject.toml",
        "backend/uv.lock",
        "frontend/package.json",
        "deploy/helm/modulo/Chart.yaml",
    ):
        target = repo / relative
        target.write_text(target.read_text(encoding="utf-8").replace("0.1.0", "0.2.0"), encoding="utf-8")
        _cut_git("add", ".", cwd=repo)
    _cut_git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "merged", cwd=repo)
    _cut_git("push", "origin", "HEAD:refs/heads/merged", cwd=repo)
    _cut_git("reset", "--hard", initial_sha, cwd=repo)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    real_git = shutil.which("git")
    assert real_git is not None
    _write_executable(
        bin_dir / "git",
        f"""#!/usr/bin/env bash
set -e
printf 'git %s\\n' \"$*\" >> {shlex.quote(str(log))}
exec {shlex.quote(real_git)} \"$@\"
""",
    )
    _write_executable(
        bin_dir / "python3",
        f"""#!/usr/bin/env bash
set -e
printf 'python3 %s\\n' \"$*\" >> {shlex.quote(str(log))}
if [ \"$1\" = \"-c\" ]; then
  grep -q '0.2.0' backend/pyproject.toml && printf '0.2.0\\n' || printf '0.1.0\\n'
  exit 0
fi
if [ \"$1\" = \"scripts/bump-version.py\" ]; then
  printf '\\n# bump stub\\n' >> backend/pyproject.toml
  printf '\\n# bump stub\\n' >> backend/uv.lock
  printf '\\n# bump stub\\n' >> frontend/package.json
  printf '\\n# bump stub\\n' >> deploy/helm/modulo/Chart.yaml
  exit 0
fi
printf 'unexpected python command: %s\\n' \"$*\" >&2
exit 1
""",
    )
    _write_executable(
        bin_dir / "uv",
        f"""#!/usr/bin/env bash
set -e
printf 'uv %s\\n' \"$*\" >> {shlex.quote(str(log))}
# `uv lock` refreshes the lockfile the workflow later stages; emulate that
# without invoking the real resolver (the fake repo has no dependencies).
printf 'version = 1\\nrevision = 3\\n' > backend/uv.lock
""",
    )
    merged_sha = _cut_git("rev-parse", "refs/remotes/origin/merged", cwd=repo).stdout.strip()
    merged_json = shlex.quote(
        json.dumps(
            {
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "mergeCommit": {"oid": merged_sha},
                "headRefOid": "branch-head",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "CLEAN",
            }
        )
    )
    _write_executable(
        bin_dir / "gh",
        f"""#!/usr/bin/env bash
set -e
printf 'gh %s\\n' \"$*\" >> {shlex.quote(str(log))}
printf '%s\\n' \"${{GH_TOKEN:-}}\" >> {shlex.quote(str(log) + ".tokens")}
case \"$*\" in
  *'pr list'*) exit 0 ;;
  *'pr create'*) printf 'https://github.com/example/repo/pull/7\\n'; exit 0 ;;
  *'pr view'*'state,mergedAt,mergeCommit,headRefOid,reviewDecision,mergeStateStatus'*)
    printf '%s\\n' {merged_json}
    exit 0
    ;;
  *'pr view'*'headRefOid'*) printf 'branch-head\\n'; exit 0 ;;
  *'workflow run'*) exit 0 ;;
  *) printf 'unexpected gh command: %s\\n' \"$*\" >&2; exit 1 ;;
esac
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "example/repo",
            "GH_TOKEN": "test-release-pr-token",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.fetch",
            "GIT_CONFIG_VALUE_0": "+refs/heads/*:refs/remotes/origin/*",
            "VERSION": "0.2.0",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return repo, log, env


def _run_cut_state_gate(
    tmp_path: Path,
    *,
    closed_prs: list[dict[str, Any]] | None = None,
    open_prs: list[dict[str, Any]] | None = None,
    existing_refs: set[str] | None = None,
    gh_failure: str | None = None,
    git_failure: bool = False,
    main_version: str = "0.2.0",
    main_version_unreadable: bool = False,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "cut-state-bin"
    bin_dir.mkdir()
    existing = existing_refs or set()
    release_rows = [{"tagName": ref.removeprefix("release:")} for ref in sorted(existing) if ref.startswith("release:")]
    open_response = (
        "echo query-error >&2; exit 1"
        if gh_failure == "open_prs"
        else "echo " + shlex.quote(json.dumps(open_prs or []))
    )
    closed_response = (
        "echo query-error >&2; exit 1"
        if gh_failure == "closed_prs"
        else "echo " + shlex.quote(json.dumps(closed_prs or []))
    )
    release_response = (
        "echo query-error >&2; exit 1" if gh_failure == "releases" else "echo " + shlex.quote(json.dumps(release_rows))
    )
    all_prs = [
        {
            **pr,
            "state": pr.get("state", "open" if pr in (open_prs or []) else "closed"),
            "merged_at": pr.get("merged_at", pr.get("mergedAt")),
            "head": {"ref": pr.get("headRefName", "release/version-0.2.0")},
            "base": {"ref": pr.get("baseRefName", "main")},
            "title": pr.get("title", "chore(release): bump version to 0.2.0"),
        }
        for pr in [*(open_prs or []), *(closed_prs or [])]
    ]
    api_response = (
        "echo query-error >&2; exit 1"
        if gh_failure == "pr_history"
        else "printf '%s\\n' " + shlex.quote(json.dumps([all_prs]))
    )
    gh_script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        case "$*" in
          *'api --paginate --slurp'*'pulls?state=all&per_page=100'*)
            {api_response}
            ;;
          *'pr list'*'--state open'*)
            {open_response}
            ;;
          *'pr list'*'--state closed'*)
            {closed_response}
            ;;
          *'release list'*)
            {release_response}
            ;;
          *)
            echo "unexpected gh call: $*" >&2
            exit 1
            ;;
        esac
        """
    )
    _write_executable(bin_dir / "gh", gh_script)
    real_git = shutil.which("git")
    assert real_git is not None
    refs_json = shlex.quote(json.dumps(sorted(existing)))
    unreadable = "1" if main_version_unreadable else "0"
    _write_executable(
        bin_dir / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "fetch" ]; then
  exit 0
fi
if [ "$1" = "show" ]; then
  if [ "{unreadable}" = "1" ]; then
    echo "fatal: path 'backend/pyproject.toml' does not exist on 'origin/main'" >&2
    exit 128
  fi
  printf '[project]\\nversion = "%s"\\n' {shlex.quote(main_version)}
  exit 0
fi
if [ "$1" != "ls-remote" ]; then
  exec {shlex.quote(real_git)} "$@"
fi
ref="${{@: -1}}"
refs={refs_json}
if jq -e --arg ref "$ref" 'index($ref) != null' <<<"$refs" >/dev/null; then
  printf 'deadbeef\\t%s\\n' "$ref"
  exit 0
fi
if [ "${{CUT_STATE_GIT_FAILURE:-0}}" = "1" ]; then
  echo "remote query failed" >&2
  exit 1
fi
exit 2
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "example/repo",
            "VERSION": "0.2.0",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "CUT_STATE_GIT_FAILURE": "1" if git_failure else "0",
        }
    )
    return _run_bash(_cut_state_script(), cwd=tmp_path, env=env)


@pytest.mark.posix_only
def test_cut_state_gate_allows_closed_unmerged_pr_when_all_release_identity_is_absent(tmp_path: Path) -> None:
    result = _run_cut_state_gate(
        tmp_path,
        closed_prs=[{"number": 2649, "headRefName": "release/version-0.2.0", "baseRefName": "main", "mergedAt": None}],
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Safe retry: prior closed unmerged release PR(s) #2649" in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_matches_exact_version_identity_across_branches(tmp_path: Path) -> None:
    result = _run_cut_state_gate(
        tmp_path,
        closed_prs=[
            {
                "number": 2649,
                "headRefName": "recovery/release-0.2.0",
                "baseRefName": "main",
                "title": "chore(release): bump version to 0.2.0",
                "mergedAt": None,
            },
            {
                "number": 2650,
                "headRefName": "release/version-0.2.00",
                "baseRefName": "main",
                "title": "chore(release): bump version to 0.2.00",
                "mergedAt": None,
            },
        ],
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Safe retry: prior closed unmerged release PR(s) #2649" in result.stdout
    assert "#2650" not in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_rejects_closed_unmerged_pr_when_branch_exists(tmp_path: Path) -> None:
    result = _run_cut_state_gate(
        tmp_path,
        closed_prs=[{"number": 2649, "headRefName": "release/version-0.2.0", "baseRefName": "main", "mergedAt": None}],
        existing_refs={"refs/heads/release/version-0.2.0"},
    )
    assert result.returncode != 0
    assert "Remote ref refs/heads/release/version-0.2.0 exists" in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_rejects_closed_merged_pr(tmp_path: Path) -> None:
    result = _run_cut_state_gate(
        tmp_path,
        closed_prs=[
            {
                "number": 2649,
                "headRefName": "release/version-0.2.0",
                "baseRefName": "main",
                "mergedAt": "2026-01-01T00:00:00Z",
            }
        ],
        main_version="0.1.0",
    )
    assert result.returncode != 0
    assert "merged PR(s) #2649" in result.stdout
    assert "main is at 0.1.0" in result.stdout
    assert "refusing to cut again" in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_resumes_merged_bump_pr_when_main_version_matches(tmp_path: Path) -> None:
    result = _run_cut_state_gate(
        tmp_path,
        closed_prs=[
            {
                "number": 2649,
                "headRefName": "release/version-0.2.0",
                "baseRefName": "main",
                "mergedAt": "2026-01-01T00:00:00Z",
            }
        ],
        main_version="0.2.0",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "already landed on main via #2649" in result.stdout
    assert "resuming at tagging" in result.stdout
    assert "refusing to cut again" not in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_rejects_merged_bump_pr_when_main_version_unreadable(tmp_path: Path) -> None:
    result = _run_cut_state_gate(
        tmp_path,
        closed_prs=[
            {
                "number": 2649,
                "headRefName": "release/version-0.2.0",
                "baseRefName": "main",
                "mergedAt": "2026-01-01T00:00:00Z",
            }
        ],
        main_version_unreadable=True,
    )
    assert result.returncode != 0
    assert "::error::Could not read version from main backend/pyproject.toml" in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_rejects_active_publication_hold(tmp_path: Path) -> None:
    result = _run_cut_state_gate(tmp_path, existing_refs={"refs/heads/release-publication-hold"})
    assert result.returncode != 0
    assert "release-publication-hold is active" in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_rejects_existing_release_tag(tmp_path: Path) -> None:
    result = _run_cut_state_gate(tmp_path, existing_refs={"refs/tags/v0.2.0"})
    assert result.returncode != 0
    assert "Remote ref refs/tags/v0.2.0 exists" in result.stdout


@pytest.mark.posix_only
def test_cut_state_gate_rejects_existing_github_release(tmp_path: Path) -> None:
    result = _run_cut_state_gate(tmp_path, existing_refs={"release:v0.2.0"})
    assert result.returncode != 0
    assert "A GitHub release already exists for version 0.2.0" in result.stdout


def test_cut_state_gate_rejects_github_query_error(tmp_path: Path) -> None:
    result = _run_cut_state_gate(tmp_path, gh_failure="pr_history")
    assert result.returncode != 0
    assert result.stderr or result.stdout


@pytest.mark.posix_only
def test_protected_cut_drives_every_gh_call_with_the_trusted_bot_token(tmp_path: Path) -> None:
    repo, log, env = _cut_harness(tmp_path)
    result = _run_bash(
        _release_job_run("Prepare exact cut commit through protected main (cut only)"), cwd=repo, env=env
    )
    assert result.returncode == 0, result.stderr + result.stdout
    observed = {line for line in Path(str(log) + ".tokens").read_text(encoding="utf-8").splitlines() if line}
    assert observed == {env["GH_TOKEN"]}
    assert "gh pr create" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("token", [None, ""])
@pytest.mark.posix_only
def test_protected_cut_fails_closed_without_the_release_pr_token(tmp_path: Path, token: str | None) -> None:
    repo, log, env = _cut_harness(tmp_path)
    if token is None:
        env.pop("GH_TOKEN")
    else:
        env["GH_TOKEN"] = token
    result = _run_bash(
        _release_job_run("Prepare exact cut commit through protected main (cut only)"), cwd=repo, env=env
    )
    assert result.returncode != 0
    assert "MODULO_RELEASE_PR_TOKEN" in result.stdout + result.stderr
    assert not log.exists() or "gh " not in log.read_text(encoding="utf-8")


@pytest.mark.posix_only
def test_protected_cut_executes_branch_pr_and_exact_merged_sha_handoff(tmp_path: Path) -> None:
    repo, log, env = _cut_harness(tmp_path)
    result = _run_bash(
        _release_job_run("Prepare exact cut commit through protected main (cut only)"), cwd=repo, env=env
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}\nlog={log.read_text(encoding='utf-8')}"
    )
    commands = log.read_text(encoding="utf-8")
    assert "git push --set-upstream origin release/version-0.2.0" in commands
    assert "gh pr create" in commands
    assert "git checkout --detach" in commands
    assert "git push origin main" not in commands
    assert "git push origin release/version-0.2.0" not in commands


@pytest.mark.posix_only
def test_protected_cut_schema_handoff_uses_exact_cut_sha(tmp_path: Path) -> None:
    repo, log, env = _cut_harness(tmp_path)
    cut_sha = _cut_git("rev-parse", "refs/remotes/origin/merged", cwd=repo).stdout.strip()
    gh_path = tmp_path / "bin" / "gh"
    _write_executable(
        gh_path,
        f"""#!/usr/bin/env bash
set -e
printf 'gh %s\\n' \"$*\" >> {shlex.quote(str(log))}
printf '%s\\n' \"${{GH_TOKEN:-}}\" >> {shlex.quote(str(log) + ".tokens")}
case \"$*\" in
  *'run list'*) printf '123\\ncompleted\\nsuccess\\n'; exit 0 ;;
  *'actions/runs/123/jobs'*) printf 'success\\n'; exit 0 ;;
  *) printf 'unexpected gh call: %s\\n' \"$*\" >&2; exit 1 ;;
esac
""",
    )
    env.update({"CUT_SHA": cut_sha, "MODE": "cut"})
    result = _run_bash(
        _release_job_run("Assert schema-freshness gate green on exact cut/recut commit"), cwd=repo, env=env
    )
    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert f"--commit {cut_sha}" in commands
    assert cut_sha in result.stdout


_SCHEMA_GATE_STEP_NAME = "Assert schema-freshness gate green on exact cut/recut commit"
_SCHEMA_TARGET_SHA = "a" * 40


def _schema_gate_harness(
    tmp_path: Path,
    *,
    run_lookups: list[tuple[str, int]],
    jobs_conclusion: str = "success",
) -> tuple[dict[str, str], Path]:
    """Run the schema-gate step against a stubbed ``gh``.

    ``run_lookups`` is the raw stdout payload and exit code of each successive
    ``gh run list`` call; once the samples run out the last one repeats. The
    poll budget is set to the number of samples (interval one second), and
    ``sleep`` is stubbed out, so the loop advances on the stub's own state per
    invocation rather than on wall-clock time.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    counter = tmp_path / "gh-count"
    samples = tmp_path / "samples"
    samples.mkdir()
    for index, (payload, returncode) in enumerate(run_lookups):
        (samples / f"{index}.out").write_text(payload, encoding="utf-8")
        (samples / f"{index}.rc").write_text(str(returncode), encoding="utf-8")
    sample_count = len(run_lookups)
    _write_executable(
        bin_dir / "gh",
        f"""#!/usr/bin/env bash
set -u
printf 'gh %s\\n' "$*" >> {shlex.quote(str(log))}
case "$*" in
  *'run list'*)
    count=0
    if [ -f {shlex.quote(str(counter))} ]; then count="$(cat {shlex.quote(str(counter))})"; fi
    count=$((count + 1))
    printf '%s' "$count" > {shlex.quote(str(counter))}
    index=$((count - 1))
    if [ "$index" -ge {sample_count} ]; then index=$(({sample_count} - 1)); fi
    cat {shlex.quote(str(samples))}/"$index.out"
    rc="$(cat {shlex.quote(str(samples))}/"$index.rc")"
    if [ "$rc" -ne 0 ]; then printf 'gh: request failed (sample %s)\\n' "$index" >&2; fi
    exit "$rc"
    ;;
  *'actions/runs/'*'/jobs'*) printf '%s\\n' {shlex.quote(jobs_conclusion)}; exit 0 ;;
  *) printf 'unexpected gh command: %s\\n' "$*" >&2; exit 1 ;;
esac
""",
    )
    _write_executable(
        bin_dir / "sleep",
        f"""#!/usr/bin/env bash
printf 'sleep %s\\n' "$*" >> {shlex.quote(str(log))}
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "example/repo",
        "GH_TOKEN": "test-token",
        "MODE": "cut",
        "CUT_SHA": _SCHEMA_TARGET_SHA,
        "SCHEMA_GATE_POLL_INTERVAL_SECONDS": "1",
        "SCHEMA_GATE_MAX_WAIT_SECONDS": str(sample_count),
    }
    return env, log


def _run_schema_gate(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run_bash(_release_job_run(_SCHEMA_GATE_STEP_NAME), cwd=tmp_path, env=env)


@pytest.mark.posix_only
def test_schema_gate_waits_for_the_exact_commit_run_to_appear(tmp_path: Path) -> None:
    env, log = _schema_gate_harness(
        tmp_path,
        run_lookups=[("\n\n", 0), ("123\nin_progress\n\n", 0), ("123\ncompleted\nsuccess\n", 0)],
    )
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "No CI workflow run for exact target" in output
    assert "CI run 123 is in_progress (attempt 2/3" in output
    assert f"Schema freshness passed on exact target {_SCHEMA_TARGET_SHA}" in output
    commands = log.read_text(encoding="utf-8")
    assert commands.count("run list") == 3
    # Two waits between the three attempts, then the gate is asserted.
    assert commands.count("sleep 1") == 2


@pytest.mark.posix_only
def test_schema_gate_aborts_when_the_exact_commit_run_fails(tmp_path: Path) -> None:
    env, log = _schema_gate_harness(tmp_path, run_lookups=[("123\ncompleted\nfailure\n", 0)])
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "concluded 'failure', not success" in output
    assert _SCHEMA_TARGET_SHA in output
    # The failing run must never be probed for a green job.
    assert "actions/runs/123/jobs" not in log.read_text(encoding="utf-8")


@pytest.mark.posix_only
@pytest.mark.parametrize("jobs_conclusion", ["cancelled", ""])
def test_schema_gate_aborts_when_the_schema_freshness_job_did_not_succeed(tmp_path: Path, jobs_conclusion: str) -> None:
    env, _log = _schema_gate_harness(
        tmp_path, run_lookups=[("123\ncompleted\nsuccess\n", 0)], jobs_conclusion=jobs_conclusion
    )
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    expected = jobs_conclusion or "missing"
    assert f"Schema freshness was '{expected}'" in output
    assert _SCHEMA_TARGET_SHA in output


@pytest.mark.posix_only
def test_schema_gate_fails_closed_and_names_the_sha_when_no_run_appears(tmp_path: Path) -> None:
    env, log = _schema_gate_harness(tmp_path, run_lookups=[("\n\n", 0)] * 3)
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert f"No CI workflow run appeared for exact target {_SCHEMA_TARGET_SHA}" in output
    assert "refusing to tag" in output
    assert "minute(s)" in output
    assert log.read_text(encoding="utf-8").count("run list") == 3


@pytest.mark.posix_only
def test_schema_gate_fails_closed_when_the_run_never_completes(tmp_path: Path) -> None:
    env, _log = _schema_gate_harness(tmp_path, run_lookups=[("123\nin_progress\n\n", 0)] * 2)
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "Timed out after" in output
    assert f"waiting for CI run 123 on exact target {_SCHEMA_TARGET_SHA}" in output
    assert "refusing to tag" in output


@pytest.mark.posix_only
def test_schema_gate_retries_transient_gh_errors(tmp_path: Path) -> None:
    env, log = _schema_gate_harness(
        tmp_path,
        run_lookups=[("", 1), ("123\ncompleted\nsuccess\n", 0)],
    )
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "gh run lookup failed (attempt 1/2)" in output
    assert "Schema freshness passed" in output
    assert log.read_text(encoding="utf-8").count("run list") == 2


@pytest.mark.posix_only
def test_schema_gate_fails_closed_when_gh_never_answers(tmp_path: Path) -> None:
    env, _log = _schema_gate_harness(tmp_path, run_lookups=[("", 1)] * 2)
    result = _run_schema_gate(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert f"Could not confirm a CI workflow run for exact target {_SCHEMA_TARGET_SHA}" in output
    assert "last error" in output
    assert "refusing to tag" in output


def test_schema_gate_polls_instead_of_sampling_once() -> None:
    script = _release_job_run(_SCHEMA_GATE_STEP_NAME)

    assert "for ((poll=1; poll<=MAX_POLLS; poll++)); do" in script
    assert "MAX_POLLS=$(( (MAX_WAIT_SECONDS + POLL_INTERVAL_SECONDS - 1) / POLL_INTERVAL_SECONDS ))" in script
    assert "SCHEMA_GATE_MAX_WAIT_SECONDS:-" in script
    assert "SCHEMA_GATE_POLL_INTERVAL_SECONDS:-" in script
    # The one-shot abort this change removes: a single empty lookup used to end the cut.
    assert "No CI workflow run found for exact target" not in script
    assert "No CI workflow run appeared for exact target" in script


def test_schema_gate_wait_budget_fits_the_release_job_timeout() -> None:
    script = _release_job_run(_SCHEMA_GATE_STEP_NAME)
    budget = _schema_wait_budget_seconds(script)
    release_job = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]

    assert budget == 60 * 60
    assert budget <= release_job["timeout-minutes"] * 60


@pytest.mark.posix_only
def test_protected_cut_cleanup_executes_hold_deletion_after_preparation_failure(tmp_path: Path) -> None:
    repo, _log, env = _cut_harness(tmp_path)
    _write_executable(
        tmp_path / "bin" / "gh",
        """#!/usr/bin/env bash
set -e
case "$*" in
  *'pr list'*'--state open'*) exit 0 ;;
  *'pr list'*'--state closed'*) printf '99\\n'; exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    failed_prep = _run_bash(
        _release_job_run("Prepare exact cut commit through protected main (cut only)"), cwd=repo, env=env
    )
    assert failed_prep.returncode != 0
    cleanup_log = tmp_path / "cleanup.log"
    _write_executable(
        tmp_path / "bin" / "git",
        f"""#!/usr/bin/env bash
printf 'git %s\\n' \"$*\" >> {shlex.quote(str(cleanup_log))}
if [ \"$1 $2 $3 $4\" = 'ls-remote --exit-code origin refs/heads/release-publication-hold' ]; then exit 0; fi
if [ \"$1 $2 $3 $4\" = 'push origin --delete refs/heads/release-publication-hold' ]; then exit 0; fi
exit 0
""",
    )
    cleanup = _run_bash(
        _release_job_run("Clean up publication hold after failed cut preparation"), cwd=tmp_path, env=env
    )
    assert cleanup.returncode == 0, cleanup.stderr
    assert "git push origin --delete refs/heads/release-publication-hold" in cleanup_log.read_text(encoding="utf-8")


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
            if invocation != ["git", "push", "origin", "refs/heads/release-freeze", "--force"]:
                violations.append("unexpected force push: " + " ".join(invocation))
    if "--admin" in tokens:
        violations.append("gh pr merge --admin")
    assert not violations, "protected release cut bypasses found: " + "; ".join(violations)
    if not force_pushes:
        return
    assert len(force_pushes) == 2
    assert all(push == ["git", "push", "origin", "refs/heads/release-freeze", "--force"] for push in force_pushes)


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
        assert "git push origin --delete refs/heads/release-publication-hold" in step["run"]


def _hold_step_script(name: str) -> str:
    script = _release_step(name).get("run")
    assert isinstance(script, str)
    return script


def _run_hold_lookup_script(tmp_path: Path, script: str, *, lookup_rc: int) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "hold-bin"
    bin_dir.mkdir()
    log = tmp_path / "hold-commands.log"
    _write_executable(
        bin_dir / "git",
        f"""#!/usr/bin/env bash
set -u
printf 'git %s\\n' \"$*\" >> {shlex.quote(str(log))}
if [ \"$1\" = ls-remote ]; then
  exit \"${{HOLD_LOOKUP_RC}}\"
fi
exit 0
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOLD_LOOKUP_RC": str(lookup_rc),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "VERSION": "1.2.3",
        "GH_TOKEN": "test",
    }
    return _run_bash(script, cwd=tmp_path, env=env)


@pytest.mark.posix_only
def test_release_publication_hold_establishment_refuses_existing_hold(tmp_path: Path) -> None:
    result = _run_hold_lookup_script(
        tmp_path,
        _hold_step_script("Establish release publication hold (cut only)"),
        lookup_rc=0,
    )
    assert result.returncode != 0
    assert "already exists" in result.stdout
    assert "git branch -f release-publication-hold" not in (tmp_path / "hold-commands.log").read_text(encoding="utf-8")


def _hold_lookup_fragment(script: str) -> str:
    start = script.index("hold_lookup_rc=0")
    branch_marker = "git branch -f release-publication-hold HEAD"
    if branch_marker in script[start:]:
        end = script.index("\n", script.index(branch_marker, start))
    else:
        end = re.search(r"\n\s*esac", script[start:])
        assert end is not None
        end = start + end.end()
    return script[start:end]


@pytest.mark.parametrize(
    "step_name",
    [
        "Establish release publication hold (cut only)",
        "Promote to GA and lift freeze",
    ],
)
@pytest.mark.parametrize("lookup_rc", [2, 17])
@pytest.mark.posix_only
def test_release_publication_hold_establishment_handles_absent_and_remote_error(
    tmp_path: Path, step_name: str, lookup_rc: int
) -> None:
    script = _hold_lookup_fragment(_hold_step_script(step_name))
    result = _run_hold_lookup_script(tmp_path, script, lookup_rc=lookup_rc)
    commands = (tmp_path / "hold-commands.log").read_text(encoding="utf-8")
    if lookup_rc == 2:
        assert result.returncode == 0, result.stderr + result.stdout
        assert "git branch -f release-publication-hold HEAD" in commands
    else:
        assert result.returncode != 0
        assert "refusing to treat it as absent" in result.stdout
        assert "git branch -f release-publication-hold HEAD" not in commands


@pytest.mark.parametrize(
    ("step_name", "lookup_rc"),
    [
        ("Clean up publication hold after failed cut preparation", 0),
        ("Clean up publication hold after failed cut preparation", 2),
        ("Clean up publication hold after failed cut preparation", 17),
        ("Clean up publication hold after failed release cut", 0),
        ("Clean up publication hold after failed release cut", 2),
        ("Clean up publication hold after failed release cut", 17),
        ("Re-establish release freeze after failed GA publication", 0),
        ("Re-establish release freeze after failed GA publication", 2),
        ("Re-establish release freeze after failed GA publication", 17),
    ],
    ids=(
        "cut-preparation-present",
        "cut-preparation-absent",
        "cut-preparation-lookup-error",
        "release-cut-present",
        "release-cut-absent",
        "release-cut-lookup-error",
        "ga-publication-present",
        "ga-publication-absent",
        "ga-publication-lookup-error",
    ),
)
@pytest.mark.posix_only
def test_release_publication_hold_cleanup_is_explicitly_fail_closed(
    tmp_path: Path, step_name: str, lookup_rc: int
) -> None:
    result = _run_hold_lookup_script(tmp_path, _hold_step_script(step_name), lookup_rc=lookup_rc)
    commands = (tmp_path / "hold-commands.log").read_text(encoding="utf-8")
    if lookup_rc == 0:
        assert result.returncode == 0, result.stderr + result.stdout
        assert "git push origin --delete refs/heads/release-publication-hold" in commands
    elif lookup_rc == 2:
        assert result.returncode == 0, result.stderr + result.stdout
        assert "release-publication-hold" in result.stdout
        assert "git push origin --delete refs/heads/release-publication-hold" not in commands
    else:
        assert result.returncode != 0
        assert "preserving unknown hold state" in result.stdout
        assert "git push origin --delete refs/heads/release-publication-hold" not in commands


def _promote_step() -> dict[str, Any]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    return steps[_step_names(steps).index("Promote to GA and lift freeze")]


@pytest.mark.posix_only
def test_release_promote_drives_every_gh_call_with_the_trusted_bot_token(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path, scenario="valid")
    assert result.returncode == 0, result.stderr + result.stdout
    token_log = Path(env["HARNESS_DIR"], "commands.log.tokens")
    tokens = {line for line in token_log.read_text(encoding="utf-8").splitlines() if line}
    assert tokens == {env["GH_TOKEN"]}


@pytest.mark.parametrize("token", [None, ""])
@pytest.mark.posix_only
def test_release_promote_fails_closed_without_the_release_pr_token(tmp_path: Path, token: str | None) -> None:
    result, _root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path, scenario="valid")
    guarded_env = dict(env)
    if token is None:
        guarded_env.pop("GH_TOKEN")
    else:
        guarded_env["GH_TOKEN"] = token
    script = _promote_step()["run"]
    result = subprocess.run(  # noqa: S603 - workflow text is repository-owned and test-controlled
        [shutil.which("bash") or "bash", "--noprofile", "--norc", "-c", script],
        cwd=env["ROOT"],
        env=guarded_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "MODULO_RELEASE_PR_TOKEN" in result.stdout + result.stderr


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
    release_script = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    release_step = next(
        step for step in release_script if step.get("name") == "Update CHANGELOG.md and GitHub Release for GA"
    )
    assert release_step["env"]["MERGED_SHA"] == "${{ steps.promote.outputs.promote_sha }}"
    assert 'gh release edit "$GA_TAG" --target "$MERGED_SHA"' in release_step["run"]
    assert 'gh release create "$GA_TAG" --target "$MERGED_SHA"' in release_step["run"]
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
    _git(root, "init", "--bare", str(origin))
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "origin", "main", "--tags")
    # A real release checkout has no local release-freeze branch: the marker
    # lives only on the remote until the promote step re-establishes it.
    _git(root, "push", "origin", f"{rc_sha}:refs/heads/release-freeze")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then printf 'gh version test\\n'; exit 0; fi
printf '%s\\n' "${GH_TOKEN:-}" >> "$HARNESS_DIR/commands.log.tokens"
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
          if git ls-remote --exit-code origin refs/heads/release-freeze >/dev/null 2>&1; then
            printf 'merge queue paused: release-freeze is active\\n' >&2
            exit 1
          fi
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
          target=""
          tag="${3:-}"
          while (($#)); do
            case "$1" in
              --title) title="$2"; shift 2 ;;
              --notes-file) notes_file="$2"; shift 2 ;;
              --target) target="$2"; shift 2 ;;
              *) shift ;;
            esac
          done
         if [ -z "$target" ]; then
           printf 'release target is required\n' >&2
           exit 1
         fi
         if [ -n "${EXPECTED_RELEASE_SHA:-}" ] && [ "$target" != "$EXPECTED_RELEASE_SHA" ]; then
           printf 'release target mismatch: %s != %s\n' "$target" "$EXPECTED_RELEASE_SHA" >&2
           exit 1
          fi
          printf 'tag=%s\ntitle=%s\ntarget=%s\n' "$tag" "$title" "$target" > "$HARNESS_DIR/release-metadata"
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
  git -C "$ROOT" rev-parse -q --verify refs/heads/release-freeze >/dev/null 2>&1 || {
    printf 'freeze missing for docker\\n' >&2
    exit 1
  }
  printf 'freeze-present:docker\\n' >> "$HARNESS_DIR/publication-markers"
}
freeze_marker
if [[ "${1:-}" == login ]]; then
  if [[ " $* " == *" --password-stdin "* ]]; then cat >/dev/null; fi
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
git -C "$ROOT" rev-parse -q --verify refs/heads/release-freeze >/dev/null 2>&1 || {
  printf 'freeze missing for helm\\n' >&2
  exit 1
}
printf 'freeze-present:helm\\n' >> "$HARNESS_DIR/publication-markers"
case "${1:-}" in
  registry)
    if [[ " $* " == *" --password-stdin "* ]]; then cat >/dev/null; fi
    printf 'helm login marker\\n' >> "$HARNESS_DIR/publication-markers" ;;
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
git -C "$ROOT" rev-parse -q --verify refs/heads/release-freeze >/dev/null 2>&1 || \\
  git -C "$ROOT" rev-parse -q --verify refs/heads/release-publication-hold >/dev/null 2>&1 || {
  printf 'freeze/hold missing for notes\\n' >&2
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
        "GH_TOKEN": "test-release-pr-token",
        "VERSION": "1.2.3",
        "SCENARIO": scenario,
        "ROOT": str(root),
        "RC_SHA": rc_sha,
        "BRANCH_SHA": rc_sha,
        "PR_SHA": rc_sha,
        "GIT_DIR": str(root / ".git"),
        "GITHUB_OUTPUT": str(root / "github-output.txt"),
        "HARNESS_DIR": str(tmp_path),
        "EXPECTED_RELEASE_SHA": "",
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
    # The promote step now removes release-freeze for the merge window, so only
    # re-publish it when the run left one behind (a successful promotion
    # re-establishes it at the exact GA commit).
    try:
        _git(root, "rev-parse", "-q", "--verify", "refs/heads/release-freeze")
    except subprocess.CalledProcessError:
        pass
    else:
        _git(root, "push", "--force", "origin", "refs/heads/release-freeze:refs/heads/release-freeze")
    return result, root, rc_sha, env["PR_SHA"], env


def _run_release_block(
    root: Path,
    env: dict[str, str],
    name: str,
    failure: str | None = None,
    *,
    expected_release_sha: str | None = None,
    omit_target: bool = False,
) -> subprocess.CompletedProcess[str]:
    steps = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    script = next(step["run"] for step in steps if step.get("name") == name)
    script = script.replace("${{ steps.promote.outputs.rc_tag }}", '"$RC_TAG"')
    script = script.replace("${{ steps.promote.outputs.ga_tag }}", '"$GA_TAG"')
    script = script.replace("${{ steps.promote.outputs.promote_sha }}", '"$PROMOTE_SHA"')
    if omit_target:
        script = script.replace(' --target "$MERGED_SHA"', "")
    block_env = {
        **env,
        "FAIL_PUBLICATION_BLOCK": "" if failure is None else failure,
        "GH_TOKEN": "test",
        "REGISTRY_USER": "test-user",
        "IMAGE_NAMESPACE": "example/repo",
        "RC_TAG": "v1.2.3-rc1",
        "GA_TAG": "v1.2.3",
        "PROMOTE_SHA": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "MERGED_SHA": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "EXPECTED_RELEASE_SHA": expected_release_sha or _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "HARNESS_DIR": env["HARNESS_DIR"],
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
            .replace("set -euo pipefail", "set -uo pipefail", 1)
        )
    recovery = _release_step("Re-establish release freeze after failed GA publication")["run"]
    lift = _freeze_lift_script()
    block_commands = [f"if bash -eo pipefail -c {shlex.quote(block)}; then :; else failure=1; fi" for block in blocks]
    sequence = "\n\n".join(
        [
            "failure=0",
            block_commands[0],
            f"if (( failure == 0 )); then\n{block_commands[1]}\nfi",
            f"if (( failure == 0 )); then\n{block_commands[2]}\nfi",
            f"if (( failure != 0 )); then\n{recovery}\nfi",
            f"if (( failure == 0 )); then\n{lift}\nfi",
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
        "MERGED_SHA": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "EXPECTED_RELEASE_SHA": _git(root, "rev-parse", "refs/remotes/origin/merge-result"),
        "HARNESS_DIR": env["HARNESS_DIR"],
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


@pytest.mark.posix_only
def test_release_promote_successfully_publishes_exact_tag_target_and_lifts_freeze(tmp_path: Path) -> None:
    result, root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path, scenario="valid")
    assert result.returncode == 0, result.stderr + result.stdout
    env["HARNESS_DIR"] = str(root.parent)
    env["EXPECTED_RELEASE_SHA"] = _git(root, "rev-parse", "refs/remotes/origin/merge-result")
    assert "GA tag v1.2.3 published at" in result.stdout
    assert "release-freeze remains active" in result.stdout
    markers_path = root.parent / "publication-markers"
    markers_path.write_text("", encoding="utf-8")
    publication = _run_publication_sequence(root, env)
    assert publication.returncode == 0, publication.stderr + publication.stdout
    markers = (Path(env["HARNESS_DIR"]) / "publication-markers").read_text(encoding="utf-8").splitlines()
    promote_sha = _git(root, "rev-parse", "refs/remotes/origin/merge-result")
    commands = (Path(env["HARNESS_DIR"]) / "release-metadata").read_text(encoding="utf-8")
    assert "tag=v1.2.3" in commands
    assert "title=Modulo v1.2.3" in commands
    assert f"target={promote_sha}" in commands
    assert "## [1.2.3]" in (root.parent / "release-notes").read_text(encoding="utf-8")
    assert "Release change" in (root.parent / "release-notes").read_text(encoding="utf-8")
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

    assert "freeze lifted" in publication.stdout
    remote_tag_object = _git(root, "ls-remote", "origin", "refs/tags/v1.2.3").split()[0]
    remote_ga_sha = _git(root, "cat-file", "tag", remote_tag_object).splitlines()[0].split()[1]
    assert remote_ga_sha == _git(root, "rev-parse", "v1.2.3^{commit}")
    assert not _git(root, "ls-remote", "origin", "refs/heads/release-freeze")


@pytest.mark.posix_only
def test_release_publication_rejects_missing_or_wrong_target(tmp_path: Path) -> None:
    result, root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path, scenario="valid")
    assert result.returncode == 0, result.stderr + result.stdout
    expected = _git(root, "rev-parse", "refs/remotes/origin/merge-result")
    missing = _run_release_block(
        root,
        env,
        "Update CHANGELOG.md and GitHub Release for GA",
        omit_target=True,
        expected_release_sha=expected,
    )
    assert missing.returncode != 0
    assert "release target is required" in missing.stderr
    wrong = _run_release_block(
        root,
        env,
        "Update CHANGELOG.md and GitHub Release for GA",
        expected_release_sha="0" * 40,
    )
    assert wrong.returncode != 0
    assert "release target mismatch" in wrong.stderr


@pytest.mark.posix_only
def test_release_promote_failure_reestablishes_freeze_without_lift(tmp_path: Path) -> None:
    for failure in ("retag", "helm", "notes"):
        result, root, _rc_sha, _pr_sha, env = _promote_harness(tmp_path / failure, scenario="valid")
        assert result.returncode == 0, result.stderr + result.stdout
        publication = _run_publication_sequence(root, env, failure=failure)
        assert publication.returncode == 0, publication.stderr + publication.stdout
        assert _git(root, "ls-remote", "origin", "refs/heads/release-freeze"), publication.stderr + publication.stdout
        assert "freeze lifted" not in publication.stdout


@pytest.mark.posix_only
def test_release_promote_rejects_wrong_version_heading(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="wrong-heading")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "exact generated release metadata" in output


@pytest.mark.posix_only
def test_release_promote_rejects_wrong_generated_release_body(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="wrong-body")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "exact generated release metadata" in output


@pytest.mark.posix_only
def test_release_promote_rejects_stale_reused_pr_head(tmp_path: Path) -> None:
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="stale")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "does not match release branch" in output


@pytest.mark.posix_only
def test_release_promote_rejects_old_ci_head(tmp_path: Path) -> None:
    old_ci_sha = "0" * 40
    result, _root, _rc_sha, _pr_sha, _env = _promote_harness(tmp_path, scenario="valid", ci_sha=old_ci_sha)
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "did not succeed for exact promote PR head" in output


@pytest.mark.posix_only
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
    # The promote step swaps release-freeze for release-publication-hold so the
    # merge queue can run, then re-establishes the freeze at the exact GA commit.
    assert "git push origin --delete refs/heads/release-freeze" in steps[promote_idx]["run"]
    assert "git branch -f release-freeze HEAD" in steps[promote_idx]["run"]
    assert "git push origin refs/heads/release-freeze --force" in steps[promote_idx]["run"]
    assert "release-publication-hold" in steps[promote_idx]["run"]
    assert "git push origin --delete refs/heads/release-freeze" in steps[lift_idx]["run"]
    assert "git push origin refs/heads/release-freeze --force" in steps[reestablish_idx]["run"]


def test_release_promote_has_no_bypass_or_silent_fallback() -> None:
    script = _promote_step()["run"]

    assert "--admin" not in script
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
    # Scope the parse to the merge wait loop: an earlier `sleep 10` from the CI
    # discovery loop must not be paired with the merge loop's MAX_POLLS (that
    # coincidence made this test pass while the real wait was 5400s).
    merge_wait = script[script.index('MERGED_SHA=""') :]
    max_polls = int(re.search(r"MAX_POLLS=(\d+)", merge_wait).group(1))
    poll_seconds = int(re.search(r"sleep (\d+)", merge_wait).group(1))
    release_job = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]

    assert max_polls * poll_seconds == 5400
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


# ---------------------------------------------------------------------------
# Freeze finalizer (FAR-2316)
#
# A failed cut/recut can strand `release-freeze`, and merge-queue.yml's freeze
# gate pauses EVERY merge while that branch exists. The finalizer is a
# dedicated job that decides from LIVE ref state after the whole publication
# chain - never from a step outcome, which cannot distinguish "never froze"
# from "pushed the freeze, then failed on the next write".
# ---------------------------------------------------------------------------

_FREEZE_CLEANUP_JOB_ID = "freeze-cleanup"
_FREEZE_CLEANUP_STEP_NAME = "Clear a stranded release freeze"
_FREEZE_CLEANUP_VERSION = "0.2.0"

_RC_TAG_OBJECT_SHA = "3" * 40
_RC_COMMIT_SHA = "4" * 40
_OTHER_RELEASE_FREEZE_SHA = "2" * 40


def _freeze_cleanup_workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8")))


def _freeze_cleanup_script(workflow: dict[str, Any] | None = None) -> str:
    source = _freeze_cleanup_workflow() if workflow is None else workflow
    steps = cast(list[dict[str, Any]], source["jobs"][_FREEZE_CLEANUP_JOB_ID]["steps"])
    script = steps[_step_names(steps).index(_FREEZE_CLEANUP_STEP_NAME)].get("run")
    assert isinstance(script, str), "the freeze finalizer must carry a shell run block"
    return script


def _rc_tag_listing(*peeled_commits: str) -> str:
    """Model `git ls-remote --tags` output for annotated RC tags.

    An annotated tag is listed twice: the tag object, then its peeled commit
    under a `^{}` suffix. The freeze branch points at a COMMIT, so only the
    peeled SHA can match it - this listing makes that distinction load-bearing.
    """
    lines: list[str] = []
    for index, commit in enumerate(peeled_commits, start=1):
        ref = f"refs/tags/v{_FREEZE_CLEANUP_VERSION}-rc{index}"
        lines.append(f"{_RC_TAG_OBJECT_SHA}\t{ref}")
        lines.append(f"{commit}\t{ref}^{{}}")
    return "\n".join(lines)


def _run_freeze_cleanup(
    tmp_path: Path,
    *,
    bundle_rc: int = 2,
    freeze_rc: int = 0,
    freeze_sha: str = _RC_COMMIT_SHA,
    rc_tags_rc: int = 0,
    rc_listing: str | None = None,
    delete_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "freeze-bin"
    bin_dir.mkdir()
    log = tmp_path / "freeze-commands.log"
    listing = _rc_tag_listing(_RC_COMMIT_SHA) if rc_listing is None else rc_listing
    _write_executable(
        bin_dir / "git",
        f"""#!/usr/bin/env bash
set -u
printf 'git %s\\n' "$*" >> {shlex.quote(str(log))}
if [ "$1" = "ls-remote" ]; then
  case "${{@: -1}}" in
    refs/tags/bundle-v*) exit {bundle_rc} ;;
    refs/heads/release-freeze)
      if [ {freeze_rc} -ne 0 ]; then exit {freeze_rc}; fi
      printf '%s\\t%s\\n' {shlex.quote(freeze_sha)} refs/heads/release-freeze
      exit 0
      ;;
    refs/tags/v*-rc*)
      if [ {rc_tags_rc} -ne 0 ]; then exit {rc_tags_rc}; fi
      cat <<'REFS'
{listing}
REFS
      exit 0
      ;;
  esac
  echo "unexpected ls-remote ref: $*" >&2
  exit 99
fi
if [ "$1" = "push" ]; then
  exit {delete_rc}
fi
echo "unexpected git call: $*" >&2
exit 99
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VERSION": _FREEZE_CLEANUP_VERSION,
    }
    return _run_bash(_freeze_cleanup_script(), cwd=tmp_path, env=env), log


def _delete_invocations(log: Path) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if "push origin --delete" in line]


@pytest.mark.posix_only
def test_freeze_cleanup_deletes_this_versions_freeze_after_a_failed_cut(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    commands = log.read_text(encoding="utf-8")
    # Non-vacuity: the whole decision chain ran, so the delete below is the
    # final step of a real sequence rather than a short-circuit.
    assert f"ls-remote --exit-code origin refs/tags/bundle-v{_FREEZE_CLEANUP_VERSION}" in commands
    assert "ls-remote --exit-code origin refs/heads/release-freeze" in commands
    assert f"ls-remote --tags origin refs/tags/v{_FREEZE_CLEANUP_VERSION}-rc*" in commands
    assert _delete_invocations(log) == ["git push origin --delete refs/heads/release-freeze"]
    assert "merge queue may resume" in result.stdout


@pytest.mark.posix_only
def test_freeze_cleanup_keeps_the_freeze_once_the_bundle_is_published(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path, bundle_rc=0)
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"bundle-v{_FREEZE_CLEANUP_VERSION} is published" in result.stdout
    assert not _delete_invocations(log)
    # A published bundle means the release completed: the freeze is deliberate,
    # so the identity check must never even be reached.
    assert "ls-remote --exit-code origin refs/heads/release-freeze" not in log.read_text(encoding="utf-8")


@pytest.mark.posix_only
def test_freeze_cleanup_leaves_another_releases_freeze_alone(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path, freeze_sha=_OTHER_RELEASE_FREEZE_SHA)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "belongs to another release" in result.stdout
    assert not _delete_invocations(log)


@pytest.mark.posix_only
def test_freeze_cleanup_is_a_no_op_when_no_freeze_exists(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path, freeze_rc=2)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "nothing to clear" in result.stdout
    assert not _delete_invocations(log)


@pytest.mark.posix_only
def test_freeze_cleanup_fails_closed_when_the_bundle_state_is_unknown(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path, bundle_rc=128)
    assert result.returncode != 0
    assert "Could not query bundle-v" in result.stdout + result.stderr
    assert not _delete_invocations(log)


@pytest.mark.posix_only
def test_freeze_cleanup_fails_closed_when_the_freeze_state_is_unknown(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path, freeze_rc=128)
    assert result.returncode != 0
    assert "Could not query release-freeze" in result.stdout + result.stderr
    assert not _delete_invocations(log)


@pytest.mark.parametrize("rc_tags_rc", [2, 128])
@pytest.mark.posix_only
def test_freeze_cleanup_fails_closed_when_the_rc_tag_state_is_unknown(tmp_path: Path, rc_tags_rc: int) -> None:
    result, log = _run_freeze_cleanup(tmp_path, rc_tags_rc=rc_tags_rc)
    assert result.returncode != 0
    assert "rc* tags" in result.stdout + result.stderr
    assert not _delete_invocations(log)


@pytest.mark.posix_only
def test_freeze_cleanup_fails_loudly_when_the_delete_is_refused(tmp_path: Path) -> None:
    result, log = _run_freeze_cleanup(tmp_path, delete_rc=1)
    assert result.returncode != 0
    assert "Could not delete refs/heads/release-freeze" in result.stdout + result.stderr
    assert _delete_invocations(log) == ["git push origin --delete refs/heads/release-freeze"]


def test_freeze_cleanup_is_a_dedicated_finalizer_job_after_publication() -> None:
    workflow = _freeze_cleanup_workflow()
    jobs = cast(dict[str, Any], workflow["jobs"])
    assert _FREEZE_CLEANUP_JOB_ID in jobs
    job = cast(dict[str, Any], jobs[_FREEZE_CLEANUP_JOB_ID])
    assert job["if"] == (
        "always() && (inputs.mode == 'cut' || inputs.mode == 'recut') "
        "&& needs.preflight.result == 'success' && failure()"
    )
    # The preflight discovery gate must succeed before the finalizer may clear
    # a freeze: a run whose gate never passed created no freeze of its own.
    assert "preflight" in job["needs"]
    assert "publish-release" in job["needs"]
    # A typo in `needs` would silently order the finalizer before publication.
    assert set(job["needs"]) <= set(jobs)
    assert job["permissions"] == {"contents": "write"}
    assert job["name"] == "Clear release freeze after a failed cut or recut"

    # The cleanup must NOT live inside `release`: a step there either cannot
    # observe a later publishing failure or infers the freeze from a step
    # outcome. Every freeze delete inside `release` stays promote-only.
    release_steps = cast(list[dict[str, Any]], jobs["release"]["steps"])
    assert _FREEZE_CLEANUP_STEP_NAME not in _step_names(release_steps)
    delete_cmd = "git push origin --delete refs/heads/release-freeze"
    deleting = [step for step in release_steps if delete_cmd in str(step.get("run", ""))]
    assert deleting, "the promote path must still lift the freeze"
    assert all("promote" in str(step.get("if", "")) for step in deleting)


def test_freeze_cleanup_script_is_real_and_never_swallows_errors() -> None:
    script = _freeze_cleanup_script()
    assert script.strip()
    assert "set -euo pipefail" in script
    assert "git push origin --delete refs/heads/release-freeze" in script
    assert "|| true" not in script


def test_freeze_cleanup_harness_reads_the_job_from_the_workflow() -> None:
    # Guard against a vacuous harness: drop the job from a copy of the workflow
    # and prove the extraction the runtime tests use can no longer find it.
    workflow = _freeze_cleanup_workflow()
    all_jobs = cast(dict[str, Any], workflow["jobs"])
    jobs = {job_id: job for job_id, job in all_jobs.items() if job_id != _FREEZE_CLEANUP_JOB_ID}
    without_finalizer = {**workflow, "jobs": jobs}
    assert _FREEZE_CLEANUP_JOB_ID not in without_finalizer["jobs"]
    with pytest.raises(KeyError):
        _freeze_cleanup_script(without_finalizer)


# ---------------------------------------------------------------------------
# Release candidate tag exposure (FAR: run 41655117202)
#
# The `release` job used to expose rc_tag as
#   ${{ steps.cut.outputs.rc_tag || steps.recut.outputs.rc_tag }}
# Across conditionally-skipped steps that produced an empty rc_tag for the
# macos-installer job while bundle received the correct value. The job output
# is now bound to a single always-run normaliser step, and these tests pin the
# wiring plus execute the step body.
# ---------------------------------------------------------------------------

_RC_OUTPUT_STEP_NAME = "Expose release candidate tag"


def _release_job() -> dict[str, Any]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    return cast(dict[str, Any], workflow["jobs"]["release"])


def test_release_rc_tag_output_is_bound_to_an_always_run_normaliser() -> None:
    job = _release_job()
    outputs = cast(dict[str, str], job["outputs"])

    assert outputs["rc_tag"] == "${{ steps.rc-output.outputs.rc_tag }}"
    assert "||" not in outputs["rc_tag"], "the rc_tag output must not short-circuit across skipped steps"

    steps = cast(list[dict[str, Any]], job["steps"])
    names = _step_names(steps)
    assert names[-1] == _RC_OUTPUT_STEP_NAME, "the rc_tag normaliser must be the final step of the release job"

    step = steps[names.index(_RC_OUTPUT_STEP_NAME)]
    assert step["id"] == "rc-output"
    assert step["if"] == "always()"
    env = cast(dict[str, str], step["env"])
    assert env["CUT_RC_TAG"] == "${{ steps.cut.outputs.rc_tag }}"
    assert env["RECUT_RC_TAG"] == "${{ steps.recut.outputs.rc_tag }}"

    run = cast(str, step["run"])
    assert 'echo "rc_tag=${rc_tag}" >> "$GITHUB_OUTPUT"' in run

    # Placement: after both mode steps, and it never reads the promote output,
    # so a promote run cannot surface a GA tag as the RC tag.
    assert names.index(_RC_OUTPUT_STEP_NAME) > names.index("Recut from current main")
    assert names.index(_RC_OUTPUT_STEP_NAME) > names.index("Promote to GA and lift freeze")
    assert "promote" not in run


def _run_rc_output_step(tmp_path: Path, *, cut: str, recut: str) -> tuple[dict[str, str], str]:
    script = _release_job_run(_RC_OUTPUT_STEP_NAME)
    output = tmp_path / "github-output"
    output.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "CUT_RC_TAG": cut,
        "RECUT_RC_TAG": recut,
    }
    result = _run_bash(script, cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr

    values: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values, result.stdout


@pytest.mark.parametrize(
    ("cut", "recut", "expected"),
    [
        ("", "v0.3.324-rc6", "v0.3.324-rc6"),
        ("v0.3.324-rc1", "", "v0.3.324-rc1"),
        ("", "", ""),
    ],
)
def test_rc_output_step_normalises_mode_outputs(tmp_path: Path, cut: str, recut: str, expected: str) -> None:
    values, stdout = _run_rc_output_step(tmp_path, cut=cut, recut=recut)
    assert values["rc_tag"] == expected
    assert f"Release candidate tag exposed to dependent jobs: '{expected}'" in stdout


def test_release_rc_tag_callers_read_the_release_job_output() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    for job_id in ("bundle", "macos-installer"):
        job = cast(dict[str, Any], workflow["jobs"][job_id])
        with_ = cast(dict[str, Any], job["with"])
        assert with_["rc_tag"] == "${{ needs.release.outputs.rc_tag }}", job_id


# ---------------------------------------------------------------------------
# Alpha scope: Windows native is Beta — include_windows gates the Windows path
# ---------------------------------------------------------------------------


def test_release_workflow_has_include_windows_input() -> None:
    """The unified release workflow must declare the include_windows input.

    Windows is Beta scope, so the default is false: the Alpha cut does not ship
    the signed Windows installer unless an operator explicitly opts in.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    # 'on' is a YAML boolean keyword; quoted key ensures correct parsing
    assert "on" in workflow
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert "include_windows" in inputs
    inp = inputs["include_windows"]
    assert inp["type"] == "boolean"
    assert inp["default"] is False
    assert "Beta" in inp["description"]


def test_windows_installer_job_gated_on_include_windows() -> None:
    """The windows-installer caller job must be gated on inputs.include_windows."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    win_job = workflow["jobs"]["windows-installer"]
    assert "inputs.include_windows" in win_job["if"]
    # The condition should require (cut or recut) AND include_windows
    assert "inputs.mode == 'cut' || inputs.mode == 'recut'" in win_job["if"]
    assert "inputs.include_windows" in win_job["if"]


def test_final_release_manifest_needs_windows_installer_when_in_scope() -> None:
    """final-release-manifest must depend on windows-installer (FAR-2338).

    The manifest downloads the ``windows-installer-<version>`` artefact when the
    Beta-only ``include_windows`` opt-in is true, so the producing job has to be
    a dependency. The job-level ``if`` is fail-closed: the Linux/macOS jobs must
    succeed, Windows must succeed when opted in, and Windows must be exactly
    ``skipped`` when it is out of scope.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    final_job = workflow["jobs"]["final-release-manifest"]
    assert "windows-installer" in final_job["needs"]
    assert "macos-installer" in final_job["needs"]
    # Folded scalars join most lines with spaces but keep a newline before the
    # more-indented continuation, so normalise whitespace before asserting.
    condition = " ".join(final_job["if"].split())
    assert "always()" in condition
    assert "inputs.mode == 'cut' || inputs.mode == 'recut'" in condition
    assert "needs.release.result == 'success'" in condition
    assert "needs.bundle.result == 'success'" in condition
    assert "needs.macos-installer.result == 'success'" in condition
    assert "inputs.include_windows && needs.windows-installer.result == 'success'" in condition
    assert "!inputs.include_windows && needs.windows-installer.result == 'skipped'" in condition


def test_publish_release_needs_excludes_windows_installer() -> None:
    """publish-release must not depend on windows-installer."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    publish_job = workflow["jobs"]["publish-release"]
    assert "windows-installer" not in publish_job["needs"]
    assert "macos-installer" in publish_job["needs"]
    assert "final-release-manifest" in publish_job["needs"]
    assert "attest-artefacts" in publish_job["needs"]


def test_final_release_manifest_downloads_windows_artifact_conditionally() -> None:
    """final-release-manifest must conditionally download the Windows artifact."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    download_steps = [step for step in final_steps if step.get("uses", "").startswith("actions/download-artifact@")]
    # Should have bundle, both architecture-suffixed macOS downloads, and conditional Windows.
    names = [step.get("with", {}).get("name") for step in download_steps]
    assert "bundle-artefacts-amd64" in names
    assert "macos-installer-${{ inputs.version }}-arm64" in names
    assert "macos-installer-${{ inputs.version }}-x86_64" in names
    # Windows download should be conditional on include_windows
    windows_downloads = [step for step in download_steps if "windows-installer" in step.get("with", {}).get("name", "")]
    assert len(windows_downloads) == 1
    win_step = windows_downloads[0]
    assert win_step.get("if") == "inputs.include_windows"


def test_final_release_manifest_builds_sha256sums_conditionally() -> None:
    """The manifest build script must conditionally include Windows in SHA256SUMS."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    final_names = _step_names(final_steps)
    build_idx = final_names.index("Build final signed manifest after every platform artifact")
    build_step = final_steps[build_idx]
    script = build_step["run"]
    # Should check INCLUDE_WINDOWS env var
    assert "INCLUDE_WINDOWS" in script
    # Should conditionally add windows exe to sha256sum
    assert 'if [ "$INCLUDE_WINDOWS" = "true" ]; then' in script
    assert "modulo-${VERSION}-windows-amd64.exe" in script
    # Should error if include_windows=true but artifact missing
    assert "include_windows=true but windows-installer" in script
    # Linux and both native macOS DMGs must always be included.
    assert "modulo-${VERSION}-linux-amd64.tar.gz" in script
    assert "Modulo-${VERSION}-macos-arm64.dmg" in script
    assert "Modulo-${VERSION}-macos-x86_64.dmg" in script


def test_final_release_manifest_builds_components_dict_conditionally() -> None:
    """The manifest components dict must conditionally include windows."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    final_names = _step_names(final_steps)
    build_idx = final_names.index("Build final signed manifest after every platform artifact")
    build_step = final_steps[build_idx]
    script = build_step["run"]
    # The manifest must describe both native macOS architectures as one
    # platform family, with Windows remaining conditional.
    assert 'components = {"linux": "amd64", "macos": "native"}' in script
    assert "if include_windows:" in script
    assert 'components["windows"] = "amd64"' in script
    # Both architecture-suffixed DMGs are unconditional manifest assets.
    assert (
        'names = [f"modulo-{version}-linux-amd64.tar.gz", '
        'f"Modulo-{version}-macos-arm64.dmg", '
        'f"Modulo-{version}-macos-x86_64.dmg"]'
    ) in script
    assert 'names.append(f"modulo-{version}-windows-amd64.exe")' in script


def test_publish_release_verifies_windows_asset_conditionally() -> None:
    """publish-release must conditionally verify and upload the Windows asset."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    publish_steps = workflow["jobs"]["publish-release"]["steps"]
    publish_names = _step_names(publish_steps)

    # Verify portable release artefacts step
    verify_idx = publish_names.index("Verify portable release artefacts")
    verify_script = publish_steps[verify_idx]["run"]
    assert "INCLUDE_WINDOWS" in verify_script
    assert 'if [ "$INCLUDE_WINDOWS" = "true" ]; then' in verify_script
    assert 'test -s "release-artefacts/modulo-${VERSION}-windows-amd64.exe"' in verify_script

    # Create GitHub Release with assets step
    create_idx = publish_names.index("Create GitHub Release with assets")
    create_script = publish_steps[create_idx]["run"]
    assert "INCLUDE_WINDOWS" in create_script
    assert 'if [ "$INCLUDE_WINDOWS" = "true" ]; then' in create_script
    assert 'ASSETS+=("release-artefacts/modulo-${VERSION}-windows-amd64.exe")' in create_script


def test_attest_artefacts_downloads_windows_conditionally() -> None:
    """attest-artefacts must conditionally download and assert the Windows artifact."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_steps = workflow["jobs"]["attest-artefacts"]["steps"]
    download_steps = [step for step in attest_steps if step.get("uses", "").startswith("actions/download-artifact@")]
    # Should have conditional windows download
    windows_downloads = [step for step in download_steps if "windows-installer" in step.get("with", {}).get("name", "")]
    assert len(windows_downloads) == 1
    win_step = windows_downloads[0]
    assert "inputs.include_windows" in win_step.get("if", "")

    # Should have assertion step for Windows exe
    assert_steps = [step for step in attest_steps if step.get("name") == "Assert the exact Windows exe is present"]
    assert len(assert_steps) == 1
    assert_step = assert_steps[0]
    assert "inputs.include_windows" in assert_step.get("if", "")


@pytest.mark.posix_only
def test_alpha_bundle_sha256sums_generation_succeeds_without_windows(tmp_path: Path) -> None:
    """Running the SHA256SUMS generation with INCLUDE_WINDOWS=false must succeed."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    final_steps = workflow["jobs"]["final-release-manifest"]["steps"]
    final_names = _step_names(final_steps)
    build_idx = final_names.index("Build final signed manifest after every platform artifact")
    build_step = final_steps[build_idx]
    build_step["run"]

    # Extract just the SHA256SUMS generation part (before the Python heredoc)
    # The script structure: cd release-artefacts -> sha256sum -> cd .. -> python heredoc -> sha256sum --check
    # We test the sha256sum generation logic directly
    artefacts_dir = tmp_path / "release-artefacts"
    artefacts_dir.mkdir()
    linux_tar = artefacts_dir / "modulo-0.3.324-linux-amd64.tar.gz"
    macos_arm64_dmg = artefacts_dir / "Modulo-0.3.324-macos-arm64.dmg"
    macos_x86_64_dmg = artefacts_dir / "Modulo-0.3.324-macos-x86_64.dmg"
    linux_tar.write_bytes(b"linux-bundle-content")
    macos_arm64_dmg.write_bytes(b"macos-arm64-dmg-content")
    macos_x86_64_dmg.write_bytes(b"macos-x86_64-dmg-content")

    # Run the conditional sha256sum generation (Alpha path: INCLUDE_WINDOWS=false)
    env = {**os.environ, "VERSION": "0.3.324", "INCLUDE_WINDOWS": "false"}
    sha256_script = """
set -euo pipefail
cd release-artefacts
if [ "$INCLUDE_WINDOWS" = "true" ]; then
  sha256sum \
    "modulo-${VERSION}-linux-amd64.tar.gz" \
    "Modulo-${VERSION}-macos-arm64.dmg" \
    "Modulo-${VERSION}-macos-x86_64.dmg" \
    "modulo-${VERSION}-windows-amd64.exe" > SHA256SUMS
else
  sha256sum \
    "modulo-${VERSION}-linux-amd64.tar.gz" \
    "Modulo-${VERSION}-macos-arm64.dmg" \
    "Modulo-${VERSION}-macos-x86_64.dmg" > SHA256SUMS
fi
cd ..
(cd release-artefacts && sha256sum --check SHA256SUMS)
"""
    result = _run_bash(sha256_script, cwd=tmp_path, env=env)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    # Verify SHA256SUMS contains Linux and both native macOS DMGs.
    sha256sums = (artefacts_dir / "SHA256SUMS").read_text(encoding="utf-8")
    expected_assets = {
        "modulo-0.3.324-linux-amd64.tar.gz",
        "Modulo-0.3.324-macos-arm64.dmg",
        "Modulo-0.3.324-macos-x86_64.dmg",
    }
    assert {line.split("  ", 1)[1] for line in sha256sums.strip().splitlines()} == expected_assets
    assert "windows-amd64.exe" not in sha256sums
    assert len(sha256sums.strip().splitlines()) == 3


@pytest.mark.posix_only
def test_beta_bundle_sha256sums_generation_succeeds_with_windows(tmp_path: Path) -> None:
    """Running the SHA256SUMS generation with INCLUDE_WINDOWS=true must succeed when Windows artifact exists."""
    artefacts_dir = tmp_path / "release-artefacts"
    artefacts_dir.mkdir()
    linux_tar = artefacts_dir / "modulo-0.3.324-linux-amd64.tar.gz"
    macos_arm64_dmg = artefacts_dir / "Modulo-0.3.324-macos-arm64.dmg"
    macos_x86_64_dmg = artefacts_dir / "Modulo-0.3.324-macos-x86_64.dmg"
    windows_exe = artefacts_dir / "modulo-0.3.324-windows-amd64.exe"
    linux_tar.write_bytes(b"linux-bundle-content")
    macos_arm64_dmg.write_bytes(b"macos-arm64-dmg-content")
    macos_x86_64_dmg.write_bytes(b"macos-x86_64-dmg-content")
    windows_exe.write_bytes(b"windows-exe-content")

    env = {**os.environ, "VERSION": "0.3.324", "INCLUDE_WINDOWS": "true"}
    sha256_script = """
set -euo pipefail
cd release-artefacts
if [ "$INCLUDE_WINDOWS" = "true" ]; then
  sha256sum \
    "modulo-${VERSION}-linux-amd64.tar.gz" \
    "Modulo-${VERSION}-macos-arm64.dmg" \
    "Modulo-${VERSION}-macos-x86_64.dmg" \
    "modulo-${VERSION}-windows-amd64.exe" > SHA256SUMS
else
  sha256sum \
    "modulo-${VERSION}-linux-amd64.tar.gz" \
    "Modulo-${VERSION}-macos-arm64.dmg" \
    "Modulo-${VERSION}-macos-x86_64.dmg" > SHA256SUMS
fi
cd ..
(cd release-artefacts && sha256sum --check SHA256SUMS)
"""
    result = _run_bash(sha256_script, cwd=tmp_path, env=env)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    # Verify SHA256SUMS includes Linux, both native macOS DMGs, and Windows.
    sha256sums = (artefacts_dir / "SHA256SUMS").read_text(encoding="utf-8")
    expected_assets = {
        "modulo-0.3.324-linux-amd64.tar.gz",
        "Modulo-0.3.324-macos-arm64.dmg",
        "Modulo-0.3.324-macos-x86_64.dmg",
        "modulo-0.3.324-windows-amd64.exe",
    }
    assert {line.split("  ", 1)[1] for line in sha256sums.strip().splitlines()} == expected_assets
    assert len(sha256sums.strip().splitlines()) == 4


@pytest.mark.posix_only
def test_beta_bundle_sha256sums_generation_fails_when_windows_missing(tmp_path: Path) -> None:
    """Running the SHA256SUMS generation with INCLUDE_WINDOWS=true must fail when Windows artifact is missing."""
    artefacts_dir = tmp_path / "release-artefacts"
    artefacts_dir.mkdir()
    linux_tar = artefacts_dir / "modulo-0.3.324-linux-amd64.tar.gz"
    macos_arm64_dmg = artefacts_dir / "Modulo-0.3.324-macos-arm64.dmg"
    macos_x86_64_dmg = artefacts_dir / "Modulo-0.3.324-macos-x86_64.dmg"
    linux_tar.write_bytes(b"linux-bundle-content")
    macos_arm64_dmg.write_bytes(b"macos-arm64-dmg-content")
    macos_x86_64_dmg.write_bytes(b"macos-x86_64-dmg-content")

    env = {**os.environ, "VERSION": "0.3.324", "INCLUDE_WINDOWS": "true"}
    sha256_script = """
set -euo pipefail
cd release-artefacts
if [ "$INCLUDE_WINDOWS" = "true" ]; then
  if [ ! -f "modulo-${VERSION}-windows-amd64.exe" ]; then
    echo "::error::include_windows=true but windows-installer-${VERSION} artifact is missing"
    exit 1
  fi
  sha256sum \
    "modulo-${VERSION}-linux-amd64.tar.gz" \
    "Modulo-${VERSION}-macos-arm64.dmg" \
    "Modulo-${VERSION}-macos-x86_64.dmg" \
    "modulo-${VERSION}-windows-amd64.exe" > SHA256SUMS
else
  sha256sum \
    "modulo-${VERSION}-linux-amd64.tar.gz" \
    "Modulo-${VERSION}-macos-arm64.dmg" \
    "Modulo-${VERSION}-macos-x86_64.dmg" > SHA256SUMS
fi
cd ..
(cd release-artefacts && sha256sum --check SHA256SUMS)
"""
    result = _run_bash(sha256_script, cwd=tmp_path, env=env)
    assert result.returncode != 0
    assert "include_windows=true but windows-installer" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Regression tests for Alpha scope gating fixes (FAR-2831)
# ---------------------------------------------------------------------------


def test_release_workflow_on_key_is_quoted_for_yaml_boolean() -> None:
    """The workflow trigger key must be quoted '"on":' because 'on' is a YAML boolean keyword."""
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    # 'on' is a YAML 1.1 boolean keyword; quoting prevents it being parsed as True
    assert '"on":' in workflow_text, "workflow trigger key must be quoted '\"on\":'"
    workflow = yaml.safe_load(workflow_text)
    assert "on" in workflow, "workflow must have 'on' key after YAML parsing"
    assert workflow["on"] is not True, "'on' key must not be parsed as boolean True"


def test_include_windows_referencing_steps_declare_env() -> None:
    """Every step that references INCLUDE_WINDOWS must declare it in env:."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if not isinstance(run, str):
                continue
            if "INCLUDE_WINDOWS" in run:
                env = step.get("env") or {}
                assert "INCLUDE_WINDOWS" in env, (
                    f"{job_id}: step '{step.get('name', 'unnamed')}' references INCLUDE_WINDOWS "
                    f"but does not declare it in env:"
                )
                # Verify it maps to the input
                assert env["INCLUDE_WINDOWS"] == "${{ inputs.include_windows }}", (
                    f"{job_id}: step '{step.get('name', 'unnamed')}' INCLUDE_WINDOWS env "
                    f"must be '${{ inputs.include_windows }}'"
                )


def test_windows_installer_gated_steps_use_parenthesised_condition() -> None:
    """The two attest-artefacts steps gated on Windows must use parenthesised condition."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_job = workflow["jobs"]["attest-artefacts"]

    for step in attest_job.get("steps", []):
        name = step.get("name", "")
        if name in ("Download the Windows installer artefact", "Assert the exact Windows exe is present"):
            condition = step.get("if", "")
            # Must be parenthesised: (cut || recut) && include_windows
            assert condition == "(inputs.mode == 'cut' || inputs.mode == 'recut') && inputs.include_windows", (
                f"Step '{name}' condition must be parenthesised, got: {condition}"
            )


def test_attest_artefacts_needs_includes_windows_installer() -> None:
    """attest-artefacts job must depend on windows-installer for the Windows artifact."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_job = workflow["jobs"]["attest-artefacts"]
    needs = attest_job.get("needs", [])
    assert "windows-installer" in needs, f"attest-artefacts.needs must include 'windows-installer', got: {needs}"
    # The job uses always() so a skipped windows-installer must not block it
    condition = attest_job.get("if", "")
    assert "always()" in condition, (
        f"attest-artefacts must use always() to not block on skipped windows-installer, got: {condition}"
    )


# ---------------------------------------------------------------------------
# OCI attestation verification fix (FAR-2832)
# ---------------------------------------------------------------------------


def test_attest_artefacts_verify_step_uses_oci_prefix() -> None:
    """The Verify image attestations step must use oci:// prefix for OCI subjects."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_job = workflow["jobs"]["attest-artefacts"]
    steps = attest_job["steps"]
    names = _step_names(steps)
    verify_idx = names.index("Verify image attestations")
    verify_step = steps[verify_idx]
    script = verify_step["run"]
    # The subject must be built with oci:// prefix
    assert 'subject="oci://ghcr.io/$image_name@$digest"' in script, (
        f"Verify step subject must use oci:// prefix, got: {script}"
    )
    # The gh attestation verify call must use the subject variable
    assert 'gh attestation verify "$subject" --repo "${{ github.repository }}"' in script


def test_attest_artefacts_job_timeout_is_30_minutes() -> None:
    """attest-artefacts timeout must be 30 minutes to accommodate artifact download variance."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_job = workflow["jobs"]["attest-artefacts"]
    # Run 41770073539 spent ~14.5 min downloading final-release-artefacts
    # and ~5 min on the macOS artifact before attesting. The 20-minute cap
    # left no headroom for artifact download variance.
    assert attest_job.get("timeout-minutes") == 30, (
        f"attest-artefacts timeout-minutes must be 30, got: {attest_job.get('timeout-minutes')}"
    )


@pytest.mark.posix_only
def test_attest_verify_step_executes_with_stub_gh_oci_form(tmp_path: Path) -> None:
    """Execute the verify step loop body with a stub gh and assert oci:// arguments."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_job = workflow["jobs"]["attest-artefacts"]
    steps = attest_job["steps"]
    names = _step_names(steps)
    verify_idx = names.index("Verify image attestations")
    verify_step = steps[verify_idx]
    script = verify_step["run"]

    # Extract the for-loop body (from 'for image_name' to the closing 'done')
    loop_start = script.index("for image_name in")
    loop_end = script.index("done", loop_start) + len("done")
    loop_body = script[loop_start:loop_end]
    # Replace GitHub Actions expression with literal value for test execution
    loop_body = loop_body.replace('"${{ github.repository }}"', '"farnalabs/modulo"')

    # Build a harness that runs the loop with stubbed gh
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.log"
    log.write_text("", encoding="utf-8")

    _write_executable(
        bin_dir / "gh",
        f"""#!/usr/bin/env bash
set -euo pipefail
# Log the exact call
printf '%s\\n' "$*" >> {shlex.quote(str(log))}
# Simulate successful verification
exit 0
""",
    )

    # Environment with dummy digests for all four images
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "IMAGE_NAMESPACE": "farnalabs/modulo",
        "ALL_IN_ONE_DIGEST": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "BACKEND_DIGEST": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "FRONTEND_DIGEST": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "RUNNER_OPENCODE_DIGEST": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
    }

    # Wrap the loop body to make it executable
    wrapper = f"""#!/usr/bin/env bash
set -euo pipefail
{loop_body}
"""
    _write_executable(tmp_path / "verify_loop.sh", wrapper)

    bash = which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - fixed bash executable and test-controlled workflow text
        [bash, "--noprofile", "--norc", "-c", f"source {shlex.quote(str(tmp_path / 'verify_loop.sh'))}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    # Parse the logged gh calls
    calls = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(calls) == 4, f"expected 4 gh attestation verify calls, got {len(calls)}: {calls}"

    # Each call must use oci:// prefix
    for call in calls:
        assert call.startswith("attestation verify oci://"), f"call must use oci:// prefix: {call}"
        assert "--repo farnalabs/modulo" in call, f"call must include --repo: {call}"

    # Verify all four images were attested
    subjects = [call.split()[2] for call in calls]  # attestation verify <subject> --repo ...
    expected_subjects = {
        "oci://ghcr.io/farnalabs/modulo@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "oci://ghcr.io/farnalabs/modulo/backend@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "oci://ghcr.io/farnalabs/modulo/frontend@sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "oci://ghcr.io/farnalabs/modulo/runner-opencode@sha256:4444444444444444444444444444444444444444444444444444444444444444",
    }
    assert set(subjects) == expected_subjects, f"unexpected subjects: {subjects}"


@pytest.mark.posix_only
def test_attest_verify_step_fails_closed_on_gh_nonzero(tmp_path: Path) -> None:
    """A non-zero gh exit must fail the verify step (fail closed)."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    attest_job = workflow["jobs"]["attest-artefacts"]
    steps = attest_job["steps"]
    names = _step_names(steps)
    verify_idx = names.index("Verify image attestations")
    verify_step = steps[verify_idx]
    script = verify_step["run"]

    loop_start = script.index("for image_name in")
    loop_end = script.index("done", loop_start) + len("done")
    loop_body = script[loop_start:loop_end]
    # Replace GitHub Actions expression with literal value for test execution
    loop_body = loop_body.replace('"${{ github.repository }}"', '"farnalabs/modulo"')

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.log"
    log.write_text("", encoding="utf-8")

    _write_executable(
        bin_dir / "gh",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {shlex.quote(str(log))}
# Simulate verification failure
exit 1
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "IMAGE_NAMESPACE": "farnalabs/modulo",
        "ALL_IN_ONE_DIGEST": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "BACKEND_DIGEST": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "FRONTEND_DIGEST": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "RUNNER_OPENCODE_DIGEST": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
    }

    wrapper = f"""#!/usr/bin/env bash
set -euo pipefail
{loop_body}
"""
    _write_executable(tmp_path / "verify_loop.sh", wrapper)

    bash = which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - fixed bash executable and test-controlled workflow text
        [bash, "--noprofile", "--norc", "-c", f"source {shlex.quote(str(tmp_path / 'verify_loop.sh'))}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    # The loop must fail on the first non-zero gh exit (set -e)
    assert result.returncode != 0, "verify step must fail when gh attestation verify fails"
    # Must have attempted at least one call
    calls = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(calls) >= 1, "at least one gh call must have been attempted"


# ---------------------------------------------------------------------------
# Publish-release gate: never skip publication silently + terminal post-condition
# ---------------------------------------------------------------------------

_PUBLISH_RELEASE_CONDITION = "${{ !cancelled() && (inputs.mode == 'cut' || inputs.mode == 'recut') }}"
_ASSERT_STEP_NAME = "Assert the release and bundle tag exist"


def test_publish_release_if_runs_despite_skipped_needs_and_never_after_cancel() -> None:
    """publish-release must still be evaluated when a need was skipped or failed, so
    the dependency guard can report it, and must not publish otherwise.

    Pinning the whole expression - not a literal prefix - is the contract: a switch
    back to always() would also publish after an operator cancelled the run, and a
    switch to the implicit success() would restore the silent skip that run
    41786063745 exposed.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["publish-release"]
    if_expr = re.sub(r"\s+", " ", str(job["if"])).strip()
    assert "always()" not in if_expr, f"publication must not run after cancellation: {if_expr}"
    assert if_expr == _PUBLISH_RELEASE_CONDITION, f"unexpected publish-release condition: {if_expr}"


def test_publish_release_guard_step_exists_and_validates_dependencies() -> None:
    """The first step must validate all four publication dependencies and fail
    closed with a specific error naming the offending job."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-release"]["steps"]
    names = _step_names(steps)
    assert "Validate publication dependencies" in names, "guard step missing"
    guard_idx = names.index("Validate publication dependencies")
    assert guard_idx == 0, "guard step must be the FIRST step (before checkout)"
    guard_step = steps[guard_idx]

    # Must declare the four env vars
    env = guard_step.get("env") or {}
    assert env.get("RELEASE_RESULT") == "${{ needs.release.result }}"
    assert env.get("MANIFEST_RESULT") == "${{ needs.final-release-manifest.result }}"
    assert env.get("ATTEST_RESULT") == "${{ needs.attest-artefacts.result }}"
    assert env.get("MACOS_RESULT") == "${{ needs.macos-installer.result }}"

    # Script must check all four and fail with ::error:: naming the job
    script = guard_step["run"]
    for job_name in ("release", "final-release-manifest", "attest-artefacts", "macos-installer"):
        assert job_name in script, f"guard must check {job_name}"
    assert "::error::" in script, "guard must emit ::error:: on failure"
    assert "Publication dependency" in script, "error must name the dependency"
    assert "refusing to publish" in script, "error must state refusal"


def _extract_publish_release_guard_script() -> str:
    """Extract the guard step's run script from the workflow."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-release"]["steps"]
    names = _step_names(steps)
    guard_idx = names.index("Validate publication dependencies")
    script = steps[guard_idx]["run"]
    assert isinstance(script, str)
    return script


def _extract_publish_release_step_script(name: str) -> str:
    """Extract any publish-release step's run script from the workflow."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-release"]["steps"]
    script = steps[_step_names(steps).index(name)]["run"]
    assert isinstance(script, str)
    return script


def _bundle_tag_script() -> str:
    return _extract_publish_release_step_script("Push the bundle tag")


def _bundle_tag_harness(
    tmp_path: Path,
    *,
    scenario: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    state = tmp_path / "pushed"
    target = "a" * 40
    other = "b" * 40
    _write_executable(
        bin_dir / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
case "$*" in
  'rev-parse HEAD') printf '%s\\n' {target} ;;
  'ls-remote --exit-code origin refs/tags/bundle-v0.1.0')
    case "$SCENARIO" in
      annotated|lightweight|different|lightweight-fallback)
        if [[ "$SCENARIO" == lightweight-fallback && ! -f {shlex.quote(str(state))} ]]; then
          exit 2
        fi
        tag_sha={target}
        [[ "$SCENARIO" == different ]] && tag_sha={other}
        printf '%s\\trefs/tags/bundle-v0.1.0\\n' "$tag_sha"
        exit 0
        ;;
      absent|push-mismatch|push-unknown|push-lightweight)
        if [[ -f {shlex.quote(str(state))} && "$SCENARIO" == push-lightweight ]]; then
          printf '%s\\trefs/tags/bundle-v0.1.0\\n' {target}; exit 0
        fi
        if [[ -f {shlex.quote(str(state))} && "$SCENARIO" == push-mismatch ]]; then
          printf '%s\\trefs/tags/bundle-v0.1.0\\n' {other}; exit 0
        fi
        if [[ -f {shlex.quote(str(state))} && "$SCENARIO" == push-lightweight ]]; then
          printf '%s\\trefs/tags/bundle-v0.1.0\\n' {target}; exit 0
        fi
        if [[ -f {shlex.quote(str(state))} && "$SCENARIO" == push-unknown ]]; then exit 128; fi
        exit 2
        ;;
      unknown) exit 128 ;;
    esac
    ;;
      'ls-remote --exit-code origin refs/tags/bundle-v0.1.0^{{}}')
    case "$SCENARIO" in
      annotated) printf '%s\\trefs/tags/bundle-v0.1.0^{{}}\\n' {target}; exit 0 ;;
      different) printf '%s\\trefs/tags/bundle-v0.1.0^{{}}\\n' {other}; exit 0 ;;
      push-lightweight) exit 2 ;;
      absent)
        if [[ -f {shlex.quote(str(state))} ]]; then printf '%s\\trefs/tags/bundle-v0.1.0^{{}}\\n' {target}; exit 0; fi
        exit 2
        ;;
      push-mismatch) exit 2 ;;
      push-unknown) exit 128 ;;
      *) exit 2 ;;
    esac
    ;;
  'tag -a bundle-v0.1.0 -m Bundle release bundle-v0.1.0') exit 0 ;;
  'push origin bundle-v0.1.0') touch {shlex.quote(str(state))}; exit 0 ;;
  *)
    if [[ "$*" == 'ls-remote --exit-code origin refs/tags/bundle-v0.1.0^{{}}' ]]; then
      if [[ -f {shlex.quote(str(state))} && "$SCENARIO" == absent ]]; then
        printf '%s\\trefs/tags/bundle-v0.1.0^{{}}\\n' {target}; exit 0
      fi
      exit 2
    fi
    exit 0
    ;;
esac
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VERSION": "0.1.0",
        "SCENARIO": scenario,
    }
    result = _run_bash(_bundle_tag_script(), cwd=tmp_path, env=env)
    return result, calls


def test_bundle_tag_existing_exact_annotated_target_is_idempotent(tmp_path: Path) -> None:
    result, calls = _bundle_tag_harness(tmp_path, scenario="annotated")
    assert result.returncode == 0, result.stderr
    assert "already published" in result.stdout
    assert "push origin bundle-v0.1.0" not in calls.read_text(encoding="utf-8")


def test_bundle_tag_existing_exact_lightweight_target_is_idempotent(tmp_path: Path) -> None:
    result, calls = _bundle_tag_harness(tmp_path, scenario="lightweight")
    assert result.returncode == 0, result.stderr
    assert "already published" in result.stdout
    assert "push origin bundle-v0.1.0" not in calls.read_text(encoding="utf-8")


def test_bundle_tag_existing_different_target_fails_without_push(tmp_path: Path) -> None:
    result, calls = _bundle_tag_harness(tmp_path, scenario="different")
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stdout
    assert "push origin bundle-v0.1.0" not in calls.read_text(encoding="utf-8")


def test_bundle_tag_remote_query_unknown_fails_without_push(tmp_path: Path) -> None:
    result, calls = _bundle_tag_harness(tmp_path, scenario="unknown")
    assert result.returncode != 0
    assert "state UNKNOWN" in result.stdout
    assert "push origin bundle-v0.1.0" not in calls.read_text(encoding="utf-8")


def test_bundle_tag_absent_push_and_exact_verification_succeed(tmp_path: Path) -> None:
    result, calls = _bundle_tag_harness(tmp_path, scenario="absent")
    assert result.returncode == 0, result.stderr
    assert "push origin bundle-v0.1.0" in calls.read_text(encoding="utf-8")


def test_bundle_tag_post_push_verification_mismatch_fails(tmp_path: Path) -> None:
    result, _ = _bundle_tag_harness(tmp_path, scenario="push-mismatch")
    assert result.returncode != 0
    assert "resolved to" in result.stdout
    assert "after push, not release target" in result.stdout
    assert "could not be verified after push" not in result.stdout


def test_bundle_tag_post_push_lightweight_fallback_succeeds_without_second_push(tmp_path: Path) -> None:
    """A lightweight tag uses the full-ref fallback and is verified once."""
    result, calls = _bundle_tag_harness(tmp_path, scenario="push-lightweight")
    assert result.returncode == 0, result.stderr
    assert "Pushed and verified bundle-v0.1.0" in result.stdout
    assert calls.read_text(encoding="utf-8").splitlines().count("push origin bundle-v0.1.0") == 1


def test_bundle_tag_post_push_verification_unknown_fails(tmp_path: Path) -> None:
    result, _ = _bundle_tag_harness(tmp_path, scenario="push-unknown")
    assert result.returncode != 0
    assert "could not be verified after push" in result.stdout


def test_bundle_tag_publication_has_no_overwrite_escape_hatches() -> None:
    script = _bundle_tag_script()
    assert "--force" not in script
    assert "--clobber" not in script
    assert 'git push origin "$BUNDLE_TAG"' in script


def test_publish_release_guard_covers_exactly_its_needs() -> None:
    """The set of jobs the guard checks must equal the set of ``needs``.

    A job added to ``needs`` without a matching check is a dependency whose
    failure (or skip) can no longer be reported by the guard, and a check left
    behind for a job that is no longer a need reads an empty result forever. The
    guard's ``for job in ...`` list is the single enumeration, so drifting it
    from ``needs`` fails here instead of silently weakening the gate.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["publish-release"]
    declared = job["needs"]
    needs = {declared} if isinstance(declared, str) else set(declared)

    script = _extract_publish_release_guard_script()
    match = re.search(r"for job in (?P<items>[^;\n]+); do", script)
    assert match is not None, "guard must enumerate its dependencies in one `for job in ...; do` list"
    guarded: dict[str, str] = {}
    for pair in shlex.split(match.group("items")):
        name, _, variable = pair.partition("=")
        assert name, f"guard entry {pair!r} must be name=$VAR"
        assert variable.startswith("$"), f"guard entry {pair!r} must map to an env variable"
        guarded[name] = variable.lstrip("$")
    assert set(guarded) == needs, f"guard and needs: drifted - needs={sorted(needs)} guard={sorted(guarded)}"

    env = job["steps"][0].get("env") or {}
    for name, variable in guarded.items():
        assert env.get(variable) == "${{ needs." + name + ".result }}", (
            f"guard variable {variable} must read needs.{name}.result"
        )


@pytest.mark.posix_only
def test_publish_release_guard_all_success_exits_zero(tmp_path: Path) -> None:
    """Guard script with all four dependencies as 'success' must exit 0."""
    script = _extract_publish_release_guard_script()
    env = {
        **os.environ,
        "RELEASE_RESULT": "success",
        "MANIFEST_RESULT": "success",
        "ATTEST_RESULT": "success",
        "MACOS_RESULT": "success",
    }
    result = _run_bash(script, cwd=tmp_path, env=env)
    assert result.returncode == 0, f"all success must exit 0: {result.stdout} {result.stderr}"
    assert "All publication dependencies succeeded" in result.stdout


@pytest.mark.posix_only
def test_publish_release_guard_final_manifest_failure_fails_and_names_it(tmp_path: Path) -> None:
    """Guard script with final-release-manifest=failure must exit non-zero and name it."""
    script = _extract_publish_release_guard_script()
    env = {
        **os.environ,
        "RELEASE_RESULT": "success",
        "MANIFEST_RESULT": "failure",
        "ATTEST_RESULT": "success",
        "MACOS_RESULT": "success",
    }
    result = _run_bash(script, cwd=tmp_path, env=env)
    assert result.returncode != 0, "failure must exit non-zero"
    assert "final-release-manifest" in result.stdout + result.stderr
    assert "failure" in result.stdout + result.stderr
    assert "refusing to publish" in result.stdout + result.stderr


@pytest.mark.posix_only
def test_publish_release_guard_attest_skipped_fails_and_names_it(tmp_path: Path) -> None:
    """Guard script with attest-artefacts=skipped must exit non-zero and name it."""
    script = _extract_publish_release_guard_script()
    env = {
        **os.environ,
        "RELEASE_RESULT": "success",
        "MANIFEST_RESULT": "success",
        "ATTEST_RESULT": "skipped",
        "MACOS_RESULT": "success",
    }
    result = _run_bash(script, cwd=tmp_path, env=env)
    assert result.returncode != 0, "skipped must exit non-zero"
    assert "attest-artefacts" in result.stdout + result.stderr
    assert "skipped" in result.stdout + result.stderr
    assert "refusing to publish" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Terminal post-condition: the run must not go green without publishing
# ---------------------------------------------------------------------------


def _post_condition_script() -> str:
    return _extract_publish_release_step_script(_ASSERT_STEP_NAME).replace('"${{ github.repository }}"', '"$GH_REPO"')


def _post_condition_env(tmp_path: Path, *, gh_body: str, gh_rc: int, git_rc: int) -> dict[str, str]:
    """Stub ``gh`` and ``git`` on PATH - both are external binaries here, so the
    script's real invocations observe the injected exit codes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "gh", f"#!/usr/bin/env bash\nprintf '%s\\n' {shlex.quote(gh_body)}\nexit {gh_rc}\n")
    _write_executable(bin_dir / "git", f"#!/usr/bin/env bash\nexit {git_rc}\n")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VERSION": "0.1.0",
        "TAG": "bundle-v0.1.0",
        "GH_REPO": "farnalabs/modulo",
    }


def test_publish_release_assertion_is_the_last_step_after_the_tag_push() -> None:
    """The verification must be terminal: after the release create and tag push,
    and the final step of the job so nothing after it can mask a failure."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    names = _step_names(workflow["jobs"]["publish-release"]["steps"])
    assert _ASSERT_STEP_NAME in names, "release/bundle-tag assertion step missing"
    assert names.index(_ASSERT_STEP_NAME) > names.index("Push the bundle tag")
    assert names[-1] == _ASSERT_STEP_NAME, "the assertion must be the job's final step"

    script = _post_condition_script()
    raw_script = _extract_publish_release_step_script(_ASSERT_STEP_NAME)
    assert 'gh release view "bundle-v${VERSION}" --repo "${{ github.repository }}"' in raw_script
    assert 'git ls-remote --exit-code origin "refs/tags/${BUNDLE_TAG}"' in script
    assert "::error::" in script, "the assertion must fail with an ::error:: annotation"
    assert "|| true" not in script, "the assertion must never swallow a failure"
    assert "::warning::" not in script, "absence must fail, not warn"


@pytest.mark.posix_only
def test_publish_release_assertion_exits_zero_when_release_and_tag_exist(tmp_path: Path) -> None:
    env = _post_condition_env(tmp_path, gh_body="v0.1.0-rc1", gh_rc=0, git_rc=0)
    result = _run_bash(_post_condition_script(), cwd=tmp_path, env=env)
    assert result.returncode == 0, f"present release and tag must pass: {result.stdout} {result.stderr}"
    assert "Publication verified" in result.stdout
    assert "bundle-v0.1.0" in result.stdout


@pytest.mark.posix_only
def test_publish_release_assertion_fails_naming_an_absent_release_and_tag(tmp_path: Path) -> None:
    env = _post_condition_env(tmp_path, gh_body="release not found", gh_rc=1, git_rc=2)
    result = _run_bash(_post_condition_script(), cwd=tmp_path, env=env)
    assert result.returncode != 0, "an absent release and tag must fail the job"
    assert "GitHub Release 'bundle-v0.1.0' is ABSENT" in result.stdout
    assert "Bundle tag 'bundle-v0.1.0' is ABSENT" in result.stdout
    assert "Publication post-condition failed" in result.stdout
    assert "Publication verified" not in result.stdout


@pytest.mark.posix_only
def test_publish_release_assertion_reports_an_unknown_state_not_absence(tmp_path: Path) -> None:
    """A transient query failure is not proof of absence: ls-remote rc 2 means no
    such ref, any other non-zero means the state is unknown, and both fail with a
    message that says which one happened."""
    env = _post_condition_env(tmp_path, gh_body="HTTP 502: Server Error", gh_rc=1, git_rc=128)
    result = _run_bash(_post_condition_script(), cwd=tmp_path, env=env)
    assert result.returncode != 0, "an unverifiable publication must fail the job"
    assert "could not be confirmed" in result.stdout
    assert "state UNKNOWN, not proof of absence" in result.stdout
    assert "is ABSENT" not in result.stdout
    assert "Publication post-condition failed" in result.stdout


# ---------------------------------------------------------------------------
# FAR-2338: Windows release packaging path
# ---------------------------------------------------------------------------


def _windows_installer_job(job_id: str) -> dict[str, Any]:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
    )
    return cast(dict[str, Any], workflow["jobs"][job_id])


def _step_by_name(job: dict[str, Any], name: str) -> dict[str, Any]:
    steps = cast(list[dict[str, Any]], job["steps"])
    return steps[_step_names(steps).index(name)]


def test_windows_artifacts_is_a_reusable_workflow_building_the_release_ref() -> None:
    """The payload producer must be callable and build the ref it is given.

    Without ``workflow_call`` the release cannot ask for a payload built from
    the RC tag, and a payload built from an unrelated main commit would ship
    binaries that do not match the release.
    """
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "windows-artifacts.yml").read_text(encoding="utf-8")
    )
    # PyYAML resolves the unquoted `on` key to boolean True (YAML 1.1).
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    inputs = triggers["workflow_call"]["inputs"]
    assert inputs["ref"]["required"] is True

    steps = cast(list[dict[str, Any]], workflow["jobs"]["download-and-verify"]["steps"])
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ inputs.ref || github.ref }}"


def test_windows_installer_build_consumes_the_native_payload_and_stages_with_version() -> None:
    """The build job must consume the payload and restage it with the version.

    ``modulo-runtime.json`` only carries a ``version`` when
    ``Invoke-WindowsBundleStaging -Version`` is passed, so a payload staged
    without it would ship a marker that cannot identify the release.
    """
    build = _windows_installer_job("build")

    download = _step_by_name(build, "Download native Windows payload")
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"]["name"] == "windows-native-artifacts"
    assert download["if"] == "${{ inputs.mode != 'dry-run' }}"

    stage = _step_by_name(build, "Stage native Windows payload")
    stage_script = cast(str, stage["run"])
    assert "Invoke-WindowsBundleStaging" in stage_script
    assert "-Version $env:VERSION_NUM" in stage_script
    assert "build-launcher.ps1" in stage_script

    compile_step = _step_by_name(build, "Compile Windows installer")
    compile_script = cast(str, compile_step["run"])
    assert "deploy/windows/modulo.iss" in compile_script
    assert '"/DVERSION_NUM=$env:VERSION_NUM"' in compile_script
    assert "'/DBUNDLE_ARCH=amd64'" in compile_script
    assert '"/DBUNDLE_DIR=$env:BUNDLE_DIR"' in compile_script


def test_windows_installer_sign_job_reuses_the_azure_trusted_signing_contract() -> None:
    """The sign job must reuse the existing signing flow and secret names.

    Signing a different file than the one built (for example an unsigned
    artefact that was never downloaded) would either fail closed on a missing
    file or ship an unsigned installer with a green job.
    """
    sign = _windows_installer_job("sign")
    names = _step_names(cast(list[dict[str, Any]], sign["steps"]))

    download = _step_by_name(sign, "Download unsigned Windows installer")
    assert download["with"]["name"] == "windows-installer-unsigned-${{ inputs.version }}"
    assert names.index("Download unsigned Windows installer") < names.index("Sign with Azure Trusted Signing")

    login = _step_by_name(sign, "Log in to Azure (OIDC)")
    assert login["uses"].startswith("azure/login@")
    assert login["with"] == {
        "client-id": "${{ secrets.AZURE_CLIENT_ID }}",
        "tenant-id": "${{ secrets.AZURE_TENANT_ID }}",
        "subscription-id": "${{ secrets.AZURE_SUBSCRIPTION_ID }}",
    }

    signing = _step_by_name(sign, "Sign with Azure Trusted Signing")
    assert signing["uses"].startswith("Azure/trusted-signing-action@")
    assert signing["with"]["signing-account-name"] == "farnasigning"
    assert signing["with"]["certificate-profile-name"] == "farna-public-trust"
    assert signing["with"]["files-folder"] == r"${{ runner.temp }}\\native-installer"

    upload = _step_by_name(sign, "Upload signed Windows installer")
    assert upload["with"]["name"] == "windows-installer-signed-${{ inputs.version }}"


def test_windows_installer_attests_and_publishes_the_versioned_installer() -> None:
    """The attested artefact name is what the release manifest downloads."""
    attest = _windows_installer_job("attest")
    steps = cast(list[dict[str, Any]], attest["steps"])

    assert _step_by_name(attest, "Download signed Windows installer")["with"]["name"] == (
        "windows-installer-signed-${{ inputs.version }}"
    )
    attest_step = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/attest-build-provenance@")
    )
    assert attest_step["with"]["subject-path"] == "release-artefacts/modulo-*-windows-amd64.exe"

    upload = _step_by_name(attest, "Upload attested Windows installer")
    assert upload["with"]["name"] == "windows-installer-${{ inputs.version }}"


def test_windows_installer_dry_run_skips_payload_build_and_publication() -> None:
    """Dry-run keeps its contract-only rehearsal and publishes nothing."""

    build = _windows_installer_job("build")
    for name in ("Download native Windows payload", "Stage native Windows payload", "Compile Windows installer"):
        assert _step_by_name(build, name)["if"] == "${{ inputs.mode != 'dry-run' }}"

    dry_run = _step_by_name(build, "Dry-run contract summary")
    assert dry_run["if"] == "${{ inputs.mode == 'dry-run' }}"
    assert "test-windows-installer.ps1 -RequireIscc" in cast(str, dry_run["run"])

    for job_id in ("sign", "attest"):
        assert "inputs.mode != 'dry-run'" in cast(dict[str, Any], _windows_installer_job(job_id))["if"]


def test_release_builds_the_windows_payload_before_the_installer() -> None:
    """release-unified must produce the payload from the RC tag first."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    payload_job = workflow["jobs"]["windows-native-artifacts"]
    assert payload_job["uses"] == "./.github/workflows/windows-artifacts.yml"
    assert payload_job["with"]["ref"] == "${{ needs.release.outputs.rc_tag }}"
    assert "inputs.include_windows" in payload_job["if"]

    assert "windows-native-artifacts" in workflow["jobs"]["windows-installer"]["needs"]


# ---------------------------------------------------------------------------
# Redis source download + sha256 verification (bundle-release.yml)
#
# Release runs 41813584406 / 41818199376 failed in "Build and sha256-verify
# Redis from pinned upstream source": the checksum manifest named
# ``downloads/redis-src.tar.gz`` while ``sha256sum --check`` ran INSIDE
# ``downloads/``, so it looked for ``downloads/downloads/redis-src.tar.gz`` and
# the step died with "No such file or directory". This harness EXECUTES the
# shipped step, so that class of defect fails here instead of in a release run.
# ---------------------------------------------------------------------------

_REDIS_SOURCE_STEP_NAME = "Build and sha256-verify Redis from pinned upstream source"


def _bundle_workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(BUNDLE_WORKFLOW.read_text(encoding="utf-8")))


def _bundle_step_run(name: str) -> str:
    steps = cast(list[dict[str, Any]], _bundle_workflow()["jobs"]["bundle"]["steps"])
    run = steps[_step_names(steps).index(name)].get("run")
    assert isinstance(run, str), f"{name!r} must contain a shell run block"
    return run


def _redis_source_tarball() -> bytes:
    """A real gzip tarball so the step's ``tar -xzf`` extraction is exercised."""
    payload = b"# pinned redis fixture\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("redis-8.0.2/README.md")
        member.size = len(payload)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


def _redis_step_harness(
    tmp_path: Path,
    *,
    tarball: bytes | None,
    pin_sha256: str,
) -> tuple[dict[str, str], Path]:
    """A repo root whose ``curl`` fetches ``tarball`` (or nothing when None).

    ``make`` and ``nproc`` are stubbed so the step's build tail never needs a
    compiler; ``sha256sum`` and ``tar`` are the real binaries, because the
    manifest-path contract is exactly what is under test.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "bundle-versions.env").write_text(
        "REDIS_VERSION=8.0.2\n"
        "REDIS_SOURCE_URL=https://download.redis.io/releases/redis-8.0.2.tar.gz\n"
        f"REDIS_SOURCE_SHA256={pin_sha256}\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture-redis-src.tar.gz"
    if tarball is not None:
        fixture.write_bytes(tarball)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "curl",
        f"""#!/usr/bin/env bash
set -u
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1" ;;
  esac
  shift
done
if [ -n "$out" ] && [ -f {shlex.quote(str(fixture))} ]; then
  cp {shlex.quote(str(fixture))} "$out"
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "nproc",
        """#!/usr/bin/env bash
printf '2\\n'
""",
    )
    _write_executable(
        bin_dir / "make",
        """#!/usr/bin/env bash
set -u
mkdir -p build/redis-install/bin
for binary in redis-server redis-cli; do
  printf '#!/usr/bin/env bash\\n' > "build/redis-install/bin/${binary}"
  chmod +x "build/redis-install/bin/${binary}"
done
exit 0
""",
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return env, root


def test_bundle_redis_step_verifies_a_matching_tarball_from_the_download_dir(tmp_path: Path) -> None:
    """The checksum passes only when the manifest name is valid where the check runs."""
    tarball = _redis_source_tarball()
    env, root = _redis_step_harness(tmp_path, tarball=tarball, pin_sha256=hashlib.sha256(tarball).hexdigest())

    result = _run_bash(_bundle_step_run(_REDIS_SOURCE_STEP_NAME), cwd=root, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "redis-src.tar.gz: OK" in result.stdout
    assert (root / "build" / "redis-install" / "bin" / "redis-server").is_file()


def test_bundle_redis_step_fails_on_a_digest_mismatch_and_never_extracts(tmp_path: Path) -> None:
    tarball = _redis_source_tarball()
    env, root = _redis_step_harness(tmp_path, tarball=tarball, pin_sha256="0" * 64)

    result = _run_bash(_bundle_step_run(_REDIS_SOURCE_STEP_NAME), cwd=root, env=env)

    assert result.returncode != 0, "a digest mismatch must fail the step"
    assert "FAILED" in result.stdout + result.stderr
    assert not (root / "redis-src").exists(), "a mismatched tarball must never be extracted"


def test_bundle_redis_step_fails_loudly_when_the_download_writes_no_file(tmp_path: Path) -> None:
    env, root = _redis_step_harness(tmp_path, tarball=None, pin_sha256="0" * 64)

    result = _run_bash(_bundle_step_run(_REDIS_SOURCE_STEP_NAME), cwd=root, env=env)

    assert result.returncode != 0, "an empty download must fail the step"
    assert "missing or empty" in result.stdout + result.stderr


def test_bundle_workflow_keeps_sha256sum_manifests_relative_to_the_download_dir() -> None:
    """Systematic guard: every ``sha256sum --check`` run from inside ``downloads/``
    must name its file relative to that directory, never ``downloads/``-prefixed."""
    offenders: list[str] = []
    workflow = _bundle_workflow()
    for job_id, job in workflow["jobs"].items():
        for step in cast(list[dict[str, Any]], job.get("steps", [])):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for line in run.splitlines():
                if "cd downloads" in line and "sha256sum --check" in line and "downloads/" in line:
                    offenders.append(f"{job_id}: {step.get('name', '')}: {line.strip()}")
    assert not offenders, (
        "sha256sum --check runs inside downloads/ but the manifest path is downloads/-prefixed: " + "; ".join(offenders)
    )
