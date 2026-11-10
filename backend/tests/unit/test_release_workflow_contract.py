"""Contract checks for the unified GitHub Release publication paths."""

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
RC_VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rc-validate.yml"
STAPLE_RETRY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-staple-retry.yml"
MACOS_INSTALLER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-installer.yml"
STAPLE_HELPER = REPO_ROOT / "scripts" / "macos" / "staple-and-verify.sh"

_MATRIX_START = "docker-upgrade-rollback:"
_PEN_TEST_START = "pen-test:"
_PREDECESSOR_START = "Resolve the previous published immutable bundle tag"
_MATRIX_RUN_START = "Run the Docker upgrade / failure / rollback matrix"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{name} module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_validator() -> Any:
    return _load_module("validate_release_tag", REPO_ROOT / "scripts" / "validate_release_tag.py")


def _render_helper() -> Any:
    return _load_module("render_validation_compose", REPO_ROOT / "scripts" / "render_validation_compose.py")


def _release_workflow_text() -> str:
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def _rc_validate_text() -> str:
    return RC_VALIDATE_WORKFLOW.read_text(encoding="utf-8")


def _rc_validate_matrix_text() -> str:
    workflow = _rc_validate_text()
    return workflow[workflow.index(_MATRIX_START) : workflow.index(_PEN_TEST_START)]


def _rc_validate_step(step_name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(_rc_validate_text())
    steps = workflow["jobs"]["docker-upgrade-rollback"]["steps"]
    matches = [step for step in steps if step.get("name") == step_name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {step_name!r} step, found {len(matches)}")
    return dict(matches[0])


_COMPOSE_VAR = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<modifier>:[-?])(?P<argument>[^}]*))?\}")


def _expand_compose_env(text: str, environ: dict[str, str]) -> str:
    """Resolve compose ``${VAR}``, ``${VAR:-default}`` and ``${VAR:?error}`` expressions."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group("name")
        modifier = match.group("modifier")
        value = environ.get(name)
        if modifier == ":-":
            return value or match.group("argument")
        if modifier == ":?":
            if not value:
                raise KeyError(match.group("argument"))
            return value
        if value is None:
            raise KeyError(name)
        return value

    return _COMPOSE_VAR.sub(substitute, text)


def _real_attestation_payload(sha256: str) -> list[dict[str, Any]]:
    """Mirror the real ``gh attestation verify --format json`` output shape.

    Each entry is the attested-artifact-processing result
    (``{"attestation": <bundle>, "verificationResult": ...}``, cli/cli
    ``pkg/cmd/attestation/verification.AttestationProcessingResult``) and the
    in-toto statement carrying the ``subject`` array lives at
    ``verificationResult.statement`` (sigstore-go ``verify.VerificationResult``).
    Live capture needs GHCR pull access, which this environment does not have,
    so this fixture encodes that real nesting directly from those structs.
    """
    return [
        {
            "attestation": {
                "bundle": {
                    "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                    "dsseEnvelope": {"payloadType": "application/vnd.in-toto+json"},
                },
                "bundle_url": "",
                "initiator": "farnalabs",
            },
            "verificationResult": {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [
                        {
                            "name": "ghcr.io/farnalabs/modulo",
                            "digest": {"sha256": sha256},
                        }
                    ],
                    "predicate": {
                        "buildDefinition": {
                            "buildType": "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
                        }
                    },
                },
                "signature": {
                    "certificate": {
                        "subjectAlternativeName": (
                            "https://github.com/farnalabs/modulo/.github/workflows/release-unified.yml@refs/tags/v0.1.0-rc1"
                        )
                    }
                },
                "verifiedTimestamps": [
                    {
                        "type": "Tlog",
                        "uri": "https://rekor.sigstore.dev",
                        "timestamp": "2026-11-08T00:00:00Z",
                    }
                ],
                "verifiedIdentity": {
                    "subjectAlternativeName": (
                        "https://github.com/farnalabs/modulo/.github/workflows/release-unified.yml@refs/tags/v0.1.0-rc1"
                    )
                },
            },
        }
    ]


def test_bundle_release_uses_product_title_without_changing_bundle_identity() -> None:
    workflow = _release_workflow_text()
    publication = workflow[workflow.index('RELEASE_TITLE="Modulo ${RC_TAG}"') :]
    existing_release, new_release = publication.split("          else\n", maxsplit=1)

    assert "TAG: bundle-v${{ inputs.version }}" in workflow
    assert 'RELEASE_TITLE="Modulo ${RC_TAG}"' in publication
    assert 'gh release edit "$TAG" --title "$RELEASE_TITLE"' in existing_release
    assert 'gh release upload "$TAG" "${ASSETS[@]}"' in existing_release
    assert 'gh release create "$TAG"' in new_release
    assert '--title "$RELEASE_TITLE"' in new_release
    assert '--title "$TAG"' not in publication
    assert 'release=f"bundle-v{version}"' in workflow


def test_ga_release_uses_product_title_while_tag_stays_machine_facing() -> None:
    workflow = _release_workflow_text()
    promotion = workflow[workflow.index('RELEASE_TITLE="Modulo ${GA_TAG}"') :]

    assert 'GA_TAG="v${VERSION}"' in workflow
    assert 'RELEASE_TITLE="Modulo ${GA_TAG}"' in promotion
    assert 'gh release edit "$GA_TAG" --target "$MERGED_SHA" --title "$RELEASE_TITLE"' in promotion
    assert 'gh release create "$GA_TAG" --target "$MERGED_SHA" --title "$RELEASE_TITLE"' in promotion
    assert '--title "$GA_TAG"' not in promotion


def test_rc_validation_runs_the_docker_acceptance_matrix_before_pen_test() -> None:
    workflow = _rc_validate_text()
    matrix_start = workflow.index(_MATRIX_START)
    pen_test_start = workflow.index(_PEN_TEST_START)
    matrix = workflow[matrix_start:pen_test_start]

    assert "needs: [clean-host-compose]" in matrix
    assert "needs: [clean-host-compose, docker-upgrade-rollback]" in workflow[pen_test_start:]
    assert "scripts/verify_compose_upgrade_rollback.py" in matrix
    assert '--old-tag "$OLD_TAG"' in matrix
    assert '--new-tag "$RC_TAG"' in matrix
    assert "alpha-docker-upgrade-rollback-${{ github.run_id }}" in matrix
    assert "scripts/render_validation_compose.py" in matrix
    assert "packaged compose asset is intentionally hard-pinned to RC_TAG" in matrix
    assert matrix_start < pen_test_start


def test_rc_validation_renders_matrix_compose_without_a_concrete_tag() -> None:
    step = _rc_validate_step("Render matrix compose image from the phase TAG")
    run = step["run"]

    assert "scripts/render_validation_compose.py" in run
    assert '--compose-file "$MATRIX_COMPOSE_FILE"' in run
    # The default selector path must be used: a concrete --tag would make every
    # matrix phase exercise one tag instead of the TAG each phase supplies.
    assert "--tag" not in run
    assert "TAG" in run


def test_rc_validation_matrix_renders_phase_tags_through_compose_substitution(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.prod.yml"
    compose_file.write_text(
        "services:\n  modulo:\n    image: ghcr.io/farnalabs/modulo:v1.1.0-rc1\n",
        encoding="utf-8",
    )
    helper = REPO_ROOT / "scripts" / "render_validation_compose.py"
    result = subprocess.run(  # noqa: S603 - fixed repository helper under test
        [sys.executable, str(helper), "--compose-file", str(compose_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    rendered = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    expression = rendered["services"]["modulo"]["image"]
    selector = "${TAG:?TAG is required - set the image tag for this matrix phase}"
    assert expression == f"ghcr.io/farnalabs/modulo:{selector}"

    # The selector must REQUIRE TAG: an unset variable has to fail rather than
    # silently render an empty tag that no registry can resolve.
    with pytest.raises(KeyError, match="TAG is required"):
        _expand_compose_env(expression, {})

    phases = {
        "clean-install-old": "v1.0.0",
        "upgrade-new": "v1.1.0-rc1",
        "failed-upgrade": "v1.1.0-rc1",
        "rollback-old": "v1.0.0",
    }
    rendered_images = {phase: _expand_compose_env(expression, {"TAG": tag}) for phase, tag in phases.items()}

    assert rendered_images == {
        "clean-install-old": "ghcr.io/farnalabs/modulo:v1.0.0",
        "upgrade-new": "ghcr.io/farnalabs/modulo:v1.1.0-rc1",
        "failed-upgrade": "ghcr.io/farnalabs/modulo:v1.1.0-rc1",
        "rollback-old": "ghcr.io/farnalabs/modulo:v1.0.0",
    }
    assert rendered_images["clean-install-old"] == rendered_images["rollback-old"]
    assert rendered_images["upgrade-new"] == rendered_images["failed-upgrade"]
    assert rendered_images["clean-install-old"] != rendered_images["upgrade-new"]


def test_render_helper_renders_a_concrete_tag_on_request(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.prod.yml"
    compose_file.write_text(
        "services:\n  modulo:\n    image: ghcr.io/farnalabs/modulo:v1.1.0-rc1\n",
        encoding="utf-8",
    )

    _render_helper().render_matrix_compose(compose_file, "v1.0.0")

    rendered = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert rendered["services"]["modulo"]["image"] == "ghcr.io/farnalabs/modulo:v1.0.0"


def test_render_helper_fails_when_no_modulo_image_line_exists(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.prod.yml"
    compose_file.write_text("services:\n  db:\n    image: postgres:16\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expected exactly one modulo image to render, found 0"):
        _render_helper().render_matrix_compose(compose_file)


def test_render_helper_refuses_multiple_modulo_image_lines(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.prod.yml"
    original = (
        "services:\n"
        "  modulo:\n"
        "    image: ghcr.io/farnalabs/modulo:v1.0.0\n"
        "  worker:\n"
        "    image: ghcr.io/farnalabs/modulo:v1.0.0\n"
    )
    compose_file.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="expected exactly one modulo image to render, found 2"):
        _render_helper().render_matrix_compose(compose_file)

    assert compose_file.read_text(encoding="utf-8") == original


def test_rc_validation_refuses_to_claim_docker_matrix_without_previous_release() -> None:
    matrix = _rc_validate_matrix_text()

    assert "no previous published bundle-v* release exists" in matrix
    assert "refusing to claim Docker upgrade/rollback coverage" in matrix
    assert "could not resolve a previous published bundle-v* release" in matrix


def test_rc_validation_resolves_the_previous_bundle_tag_from_the_current_release() -> None:
    workflow = _rc_validate_text()
    matrix = workflow[workflow.index(_PREDECESSOR_START) : workflow.index(_MATRIX_RUN_START)]

    assert 'current_bundle_tag="bundle-v${version}"' in matrix
    assert "scripts/validate_release_tag.py resolve-previous" in matrix
    assert '--current "$current_bundle_tag"' in matrix
    # The current bundle is published only after this validation is dispatched,
    # so the step must not require it in the release history.
    assert "release-unified publishes the current bundle AFTER it dispatches this" in matrix
    assert "the current tag itself is excluded" in matrix
    assert "bundle-v*) ;;" in matrix
    assert 'gh release download "$bundle_tag"' in matrix
    assert 'echo "OLD_TAG=$old_tag" >> "$GITHUB_ENV"' in matrix
    assert 'old_tag="bundle-v' not in matrix


def test_rc_validation_current_bundle_tag_selects_the_previous_published_tag() -> None:
    validator = _release_validator()
    releases = [
        {"tag_name": "bundle-v1.2.0", "draft": False, "prerelease": False},
        {"tag_name": "bundle-v1.3.0", "draft": False, "prerelease": False},
    ]

    assert validator.resolve_previous_release([releases], "bundle-v1.3.0") == "bundle-v1.2.0"


def test_rc_validation_resolves_predecessor_when_current_bundle_is_not_published() -> None:
    validator = _release_validator()
    releases = [
        [
            {"tag_name": "bundle-v1.1.0", "draft": False, "prerelease": False},
            {"tag_name": "bundle-v1.2.0", "draft": False, "prerelease": False},
        ]
    ]

    # The RC's own bundle release does not exist yet at dispatch time.
    assert validator.resolve_previous_release(releases, "bundle-v1.3.0") == "bundle-v1.2.0"


def test_rc_validation_predecessor_never_selects_current_or_a_newer_release() -> None:
    validator = _release_validator()

    assert (
        validator.resolve_previous_release(
            [[{"tag_name": "bundle-v1.3.0", "draft": False, "prerelease": False}]],
            "bundle-v1.3.0",
        )
        is None
    )
    assert (
        validator.resolve_previous_release(
            [[{"tag_name": "bundle-v1.4.0", "draft": False, "prerelease": False}]],
            "bundle-v1.3.0",
        )
        is None
    )


def test_rc_validation_predecessor_ignores_drafts_and_prereleases() -> None:
    validator = _release_validator()
    releases = [
        [
            {"tag_name": "bundle-v1.0.0", "draft": False, "prerelease": False},
            {"tag_name": "bundle-v1.1.0", "draft": True, "prerelease": False},
            {"tag_name": "bundle-v1.2.0", "draft": False, "prerelease": True},
        ]
    ]

    assert validator.resolve_previous_release(releases, "bundle-v1.3.0") == "bundle-v1.0.0"


def test_rc_validation_rejects_predecessor_checksum_and_version_mismatches() -> None:
    workflow = _rc_validate_text()
    matrix = workflow[workflow.index(_PREDECESSOR_START) : workflow.index(_MATRIX_RUN_START)]

    assert 'gh release download "$bundle_tag"' in matrix
    assert "--pattern SHA256SUMS" in matrix
    assert 'published_sums="$RUNNER_TEMP/SHA256SUMS"' in matrix
    assert "no SHA256SUMS artifact" in matrix
    assert 'no checksum for $(basename "$downloaded_archive")' in matrix
    assert "checksum mismatch for published predecessor bundle asset" in matrix
    assert 'BUNDLE_VERSION="$bundle_version"' in matrix
    assert "published predecessor bundle version" in matrix
    assert "including any RC suffix" in matrix


def test_image_digest_verification_accepts_real_attestation_verify_shape() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64
    attestation = _real_attestation_payload("a" * 64)

    assert validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, attestation) == digest


def test_image_digest_verification_rejects_subject_at_the_wrong_nesting() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64
    # A regression guard: the pre-fix reader looked for ``subject`` directly
    # under ``verificationResult`` and therefore never saw real attestations.
    attestation = {"verificationResult": {"subject": [{"digest": {"sha256": "a" * 64}}]}}

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, attestation)


def test_image_digest_verification_rejects_a_foreign_attested_subject() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64
    attestation = _real_attestation_payload("b" * 64)

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, attestation)


def test_image_digest_verification_accepts_a_bare_top_level_statement() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64
    attestation = {"subject": [{"digest": {"sha256": "a" * 64}}]}

    assert validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, attestation) == digest


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/attacker/modulo:v1.2.3",
        "ghcr.io/farnalabs/modulo-evil:v1.2.3",
        "docker.io/farnalabs/modulo:v1.2.3",
    ],
)
def test_image_digest_verification_rejects_foreign_images(image: str) -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64
    attestation = _real_attestation_payload("a" * 64)

    with pytest.raises(validator.ReleaseValidationError, match=re.escape("must be this project's GHCR reference")):
        validator.verify_image_digest(image, digest, attestation)


def test_image_digest_verification_fails_closed_for_missing_provenance() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, {})


def test_image_digest_verification_ignores_unrelated_nested_sha256_metadata() -> None:
    validator = _release_validator()
    requested = "sha256:" + "a" * 64
    attestation = _real_attestation_payload("b" * 64)
    attestation[0]["verificationResult"]["statement"]["predicate"] = {"metadata": {"sha256": "a" * 64}}

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", requested, attestation)


def test_image_digest_verification_rejects_malformed_subject_digest() -> None:
    validator = _release_validator()
    requested = "sha256:" + "a" * 64
    attestation = _real_attestation_payload("not-a-digest")

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", requested, attestation)


def test_image_digest_cli_accepts_attestation_json_and_rejects_missing_digest(
    tmp_path: Path,
) -> None:
    import json

    digest = "sha256:" + "a" * 64
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_text(json.dumps(_real_attestation_payload("a" * 64)), encoding="utf-8")
    script = REPO_ROOT / "scripts" / "validate_release_tag.py"
    command = [
        sys.executable,
        str(script),
        "verify-image-digest",
        "--image",
        "ghcr.io/farnalabs/modulo:v1.2.3",
        "--digest",
        digest,
        "--attestation-json",
        str(attestation_path),
    ]

    success = subprocess.run(  # noqa: S603 - fixed CLI under test
        command, capture_output=True, text=True, check=False, timeout=30
    )
    assert success.returncode == 0
    assert success.stdout.strip() == digest
    assert not success.stderr

    missing = subprocess.run(  # noqa: S603 - fixed CLI under test
        [*command[:-1], str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert missing.returncode == 1
    assert "cannot read GitHub API JSON" in missing.stderr


def test_rc_validation_uses_predecessor_bundle_image_tag_before_promotion() -> None:
    workflow = _rc_validate_text()
    matrix = workflow[workflow.index(_PREDECESSOR_START) : workflow.index(_MATRIX_RUN_START)]

    assert 'gh release download "$bundle_tag"' in matrix
    assert 'compose_file="$bundle_dir/docker-compose.prod.yml"' in matrix
    assert 'image = compose.get("services", {}).get("modulo", {}).get("image")' in matrix
    assert 'old_tag="$(COMPOSE_FILE="$compose_file" BUNDLE_VERSION="$bundle_version" \\' in matrix
    assert "uv run --no-project --no-sync --no-build --with pyyaml==6.0.3 python - <<'PY'" in matrix
    assert 'BUNDLE_VERSION="$bundle_version" python3' not in matrix
    assert 're.fullmatch(r"v[0-9]+\\.[0-9]+\\.[0-9]+(?:-rc[0-9]+)?", tag)' in matrix
    assert 'expected_version = os.environ["BUNDLE_VERSION"]' in matrix
    assert "published predecessor bundle version" in matrix
    assert "predecessor compose pins non-immutable or unexpected image tag" in matrix
    assert 'docker buildx imagetools inspect "$old_image"' in matrix
    assert 'gh attestation verify "$old_subject"' in matrix
    assert "verify-image-digest" in matrix
    assert "no verifiable build provenance" in matrix
    assert 'published_sums="$RUNNER_TEMP/SHA256SUMS"' in matrix
    assert 'no checksum for $(basename "$downloaded_archive")' in matrix
    assert "checksum mismatch for published predecessor bundle asset" in matrix
    assert "does not derive old_tag from bundle_tag" not in matrix


def test_rc_validation_does_not_export_a_dead_old_image_digest() -> None:
    text = _rc_validate_text()

    assert "OLD_IMAGE_DIGEST" not in text


def test_rc_validation_evidence_upload_tolerates_a_missing_directory() -> None:
    step = _rc_validate_step("Upload Docker acceptance evidence")

    assert step["with"]["if-no-files-found"] == "warn"
    assert step["with"]["path"] == "${{ runner.temp }}/compose-matrix-evidence"


# ---------------------------------------------------------------------------
# FAR-2480: retryable notarization stapling
#
# Apple CloudKit ticket delivery lags notarization, so the in-run staple retry
# can exhaust without meaning the notarization is lost. Stapling must move to a
# retryable, idempotent path, and the release must not fail because the ticket
# has not propagated yet. These are the structural pins for that split.
# ---------------------------------------------------------------------------


def _staple_retry_workflow() -> dict[str, Any]:
    return yaml.safe_load(STAPLE_RETRY_WORKFLOW.read_text(encoding="utf-8"))


def _workflow_triggers(workflow: dict[str, Any]) -> dict[Any, Any]:
    # PyYAML resolves the unquoted `on` key to boolean True (YAML 1.1).
    raw = workflow.get("on", workflow.get(True))
    assert isinstance(raw, dict), "workflow must declare at least one trigger"
    return raw


def _python_heredoc(script: str, marker: str) -> str:
    start = script.index("<<'" + marker + "'")
    body = script[start:].split("\n", 1)[1]
    return body[: body.index("\n" + marker)]


def test_macos_staple_retry_workflow_exists_and_is_dispatch_and_scheduled() -> None:
    assert STAPLE_RETRY_WORKFLOW.is_file(), "the FAR-2480 staple retry workflow must exist"
    workflow = _staple_retry_workflow()
    triggers = _workflow_triggers(workflow)

    assert "workflow_dispatch" in triggers, "the retry must be dispatchable on demand"
    assert "schedule" in triggers, "the retry must run on a schedule as the backstop"
    schedule = triggers["schedule"]
    assert isinstance(schedule, list)
    assert schedule
    assert any("cron" in entry for entry in schedule)

    dispatch_inputs = triggers["workflow_dispatch"]["inputs"]
    for name in ("version", "architecture", "release_tag", "run_id", "force"):
        assert name in dispatch_inputs, f"workflow_dispatch must declare the '{name}' input"
    assert set(dispatch_inputs["architecture"]["options"]) == {"all", "arm64", "x86_64"}


def test_macos_staple_retry_invokes_the_idempotent_helper() -> None:
    workflow = _staple_retry_workflow()
    run_scripts = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]
    combined = "\n".join(run_scripts)

    assert "scripts/macos/staple-and-verify.sh" in combined, "the workflow must run the staple helper"
    helper = STAPLE_HELPER.read_text(encoding="utf-8")
    # Idempotency: the guard must run BEFORE the staple action. A guard that is
    # removed or reordered lets a schedule re-staple an already-good DMG.
    guard = helper.index("xcrun stapler validate")
    action = helper.index("xcrun stapler staple")
    assert guard < action, "the already-stapled guard must precede the staple action"
    assert "record_result unchanged" in helper
    assert "record_result stapled" in helper


def test_macos_staple_helper_keeps_every_hard_gate() -> None:
    helper = STAPLE_HELPER.read_text(encoding="utf-8")

    # Every gate is fail-closed and none may be dropped.
    assert "codesign --verify" in helper
    assert "xcrun stapler staple" in helper
    assert "xcrun stapler validate" in helper
    assert "spctl --assess" in helper
    assert "hdiutil verify" in helper
    assert "set -euo pipefail" in helper


def _step(workflow: dict[str, Any], job_id: str, step_name: str) -> dict[str, Any]:
    for step in workflow["jobs"][job_id].get("steps", []):
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"job {job_id!r} has no step named {step_name!r}")


def _release_upload_command(workflow: dict[str, Any], job_id: str, step_name: str) -> str:
    """Return the full `gh release upload` invocation, continuations joined.

    Scoped to the named step on purpose: `gh release download ... --clobber`
    also appears in these workflows, and the completion marker has its own
    `gh release upload`, so a whole-file `"--clobber" in text` check passes even
    when `--clobber` is removed from the DMG upload.
    """
    script = _step(workflow, job_id, step_name)["run"]
    lines = script[script.index("gh release upload") :].splitlines()
    command: list[str] = []
    for line in lines:
        command.append(line.strip())
        if not line.rstrip().endswith("\\"):
            break
    return " ".join(command)


def test_macos_staple_retry_republishes_with_clobber() -> None:
    workflow = _staple_retry_workflow()
    upload = _release_upload_command(workflow, "publish-stapled", "Publish the stapled DMG(s) and refreshed manifest")

    assert "gh release upload" in upload, "the retry must attach the stapled DMG to the release"
    assert "--clobber" in upload.split(), "replacing the unstapled asset requires --clobber on the upload"
    # The retry regenerates the checksum file and signed manifest in the same job
    # as the clobber, so replacement never publishes stale hashes.
    text = STAPLE_RETRY_WORKFLOW.read_text(encoding="utf-8")
    assert "SHA256SUMS" in text
    assert "RELEASE_MANIFEST.json" in text
    assert "sha256sum --check SHA256SUMS" in text


def test_macos_staple_retry_republish_is_consistency_driven() -> None:
    workflow = _staple_retry_workflow()
    publish = workflow["jobs"]["publish-stapled"]
    condition = " ".join(str(publish["if"]).split())

    assert "needs.resolve-target.outputs.release_tag != ''" in condition
    # Regression pin: gating the publish job on the staple result is what left a
    # partial upload permanently unrepaired - the next run's staple is a no-op
    # `unchanged`, so nothing ever regenerated the stale metadata.
    assert "needs.staple.outputs.changed" not in condition, (
        "publication must be driven by metadata consistency, not by whether this run stapled"
    )

    detect = _step(workflow, "publish-stapled", "Detect whether the release metadata matches its artefacts")
    script = detect["run"]
    assert "needs_republish" in script
    assert "SHA256SUMS" in script
    assert "RELEASE_MANIFEST.json" in script
    # The decision is made from the release's ACTUAL bytes, not from the staple log.
    assert "sha256" in script
    for step_name in (
        "Regenerate SHA256SUMS and the signed release manifest",
        "Publish the stapled DMG(s) and refreshed manifest",
    ):
        step = _step(workflow, "publish-stapled", step_name)
        assert "needs_republish == 'true'" in str(step.get("if", "")), (
            f"{step_name!r} must only run when the metadata disagrees with the artefacts"
        )


def test_macos_staple_retry_verifies_the_published_metadata() -> None:
    workflow = _staple_retry_workflow()
    verify = _step(workflow, "publish-stapled", "Verify the published release metadata")
    script = verify["run"]

    assert "needs_republish == 'true'" in str(verify.get("if", ""))
    # Re-reads the metadata FROM the release - the local copies prove nothing
    # about what was published.
    assert "gh release download" in script
    assert "SHA256SUMS" in script
    assert "RELEASE_MANIFEST.json" in script
    # ...and compares it against the published asset digests, failing on mismatch.
    assert "releases/tags" in script
    assert "verify_signature" in script
    assert "raise SystemExit" in script


def test_macos_staple_retry_records_and_honours_a_completion_marker() -> None:
    workflow = _staple_retry_workflow()
    resolve = _step(workflow, "resolve-target", "Resolve version, release tag and architectures")
    resolve_script = resolve["run"]

    # The marker is honoured in the LINUX resolve job, so a completed release
    # never spawns a paid macOS runner on the 30-minute schedule.
    assert workflow["jobs"]["resolve-target"]["runs-on"] == "ubuntu-latest"
    assert "MACOS_STAPLED.json" in resolve_script
    assert 'completed="true"' in resolve_script
    assert "completed=%s" in resolve_script
    completed_output = workflow["jobs"]["resolve-target"].get("outputs", {}).get("completed")
    assert completed_output == "${{ steps.resolve.outputs.completed }}"

    # Regression pin for the short-circuit. `architectures="` alone is vacuous:
    # it also matches the initialiser at the top of the script, so it kept
    # passing after the completion-path reset was deleted - which silently
    # restored the paid-macos-job-every-30-minutes behaviour. Pin the reset
    # INSIDE the completion branch, and pin the job guard that consumes it.
    completion_branch = _completion_branch(resolve_script)
    for reset in ('version=""', 'release_tag=""', 'architectures=""', 'run_id=""'):
        assert reset in completion_branch, f"the completion branch must blank {reset}"
    staple_condition = " ".join(str(workflow["jobs"]["staple"]["if"]).split())
    assert "needs.resolve-target.outputs.completed != 'true'" in staple_condition, (
        "the staple job must gate on the resolve job's completed output"
    )
    assert "needs.resolve-target.outputs.architectures != ''" in staple_condition

    record = _step(workflow, "publish-stapled", "Record the verified staple state on the release")
    assert "gh release upload" in record["run"]
    assert "MACOS_STAPLED.json" in record["run"]
    # Only a successful staple may claim completion - otherwise a still-
    # propagating ticket would be recorded as finished - and the marker must
    # never be written after a failed verification (an explicit `if:` replaces a
    # step's implicit success() guard, so it is spelled out).
    record_condition = " ".join(str(record["if"]).split())
    assert "needs.staple.result == 'success'" in record_condition
    assert "success()" in record_condition


def _completion_branch(script: str) -> str:
    """Return only the `marker_state == "complete"` arm of the resolve script."""
    start = script.index('"$marker_state" == "complete"')
    end = script.index("elif", start)
    return script[start:end]


def _write_release_dmgs(tmp_path: Path, version: str, arm64: bytes, x86_64: bytes) -> None:
    root = tmp_path / "release-assets"
    root.mkdir(exist_ok=True)
    (root / f"Modulo-{version}-macos-arm64.dmg").write_bytes(arm64)
    (root / f"Modulo-{version}-macos-x86_64.dmg").write_bytes(x86_64)


def _record_marker(
    tmp_path: Path,
    version: str,
    verified_arches: str,
    prior: dict[str, Any] | None,
) -> dict[str, Any]:
    """Execute the record step's embedded Python the way the workflow does."""
    workflow = _staple_retry_workflow()
    record = _step(workflow, "publish-stapled", "Record the verified staple state on the release")
    source = _python_heredoc(record["run"], "PY")
    marker = tmp_path / "MACOS_STAPLED.json"
    prior_path = tmp_path / "prior.json"
    if prior is not None:
        prior_path.write_text(json.dumps(prior), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed interpreter running repository-owned workflow text
        [sys.executable, "-c", source, str(marker), str(prior_path)],
        cwd=tmp_path,
        env={**os.environ, "VERSION": version, "VERIFIED_ARCHES": verified_arches},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(marker.read_text(encoding="utf-8"))


def test_macos_staple_marker_records_only_the_architectures_this_run_verified(tmp_path: Path) -> None:
    version = "0.1.0"
    arm_bytes, x86_bytes = b"arm64-bytes", b"x86_64-bytes"
    _write_release_dmgs(tmp_path, version, arm_bytes, x86_bytes)

    # A single-architecture dispatch stapled/verified arm64 only.
    payload = _record_marker(tmp_path, version, "arm64", prior=None)

    by_name = {artefact["name"]: artefact for artefact in payload["artifacts"]}
    arm = by_name[f"Modulo-{version}-macos-arm64.dmg"]
    x86 = by_name[f"Modulo-{version}-macos-x86_64.dmg"]
    assert arm["staple_verified"] is True
    assert x86["staple_verified"] is False, (
        "a single-architecture run must never record the other DMG as staple-verified"
    )
    assert arm["sha256"] == hashlib.sha256(arm_bytes).hexdigest()
    assert x86["sha256"] == hashlib.sha256(x86_bytes).hexdigest()


def test_macos_staple_marker_carries_forward_a_prior_verification_only_for_unchanged_bytes(
    tmp_path: Path,
) -> None:
    version = "0.1.0"
    _write_release_dmgs(tmp_path, version, b"arm64-bytes", b"x86_64-bytes")
    first = _record_marker(tmp_path, version, "arm64", prior=None)

    # A later run that staples only x86_64 keeps arm64's verified claim: its
    # bytes are unchanged, so the prior verification still applies.
    second = _record_marker(tmp_path, version, "x86_64", prior=first)
    by_name = {artefact["name"]: artefact for artefact in second["artifacts"]}
    assert by_name[f"Modulo-{version}-macos-arm64.dmg"]["staple_verified"] is True
    assert by_name[f"Modulo-{version}-macos-x86_64.dmg"]["staple_verified"] is True

    # A prior claim about DIFFERENT bytes no longer applies to the rebuilt DMG.
    _write_release_dmgs(tmp_path, version, b"rebuilt-arm64-bytes", b"x86_64-bytes")
    third = _record_marker(tmp_path, version, "x86_64", prior=second)
    by_name = {artefact["name"]: artefact for artefact in third["artifacts"]}
    assert by_name[f"Modulo-{version}-macos-arm64.dmg"]["staple_verified"] is False


def _resolve_marker_state(
    tmp_path: Path,
    version: str,
    marker: dict[str, Any],
    release: dict[str, Any],
) -> str:
    """Execute the resolve step's embedded marker check the way the workflow does."""
    workflow = _staple_retry_workflow()
    resolve = _step(workflow, "resolve-target", "Resolve version, release tag and architectures")
    source = _python_heredoc(resolve["run"], "PY")
    marker_path = tmp_path / "MACOS_STAPLED.json"
    release_path = tmp_path / "release.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    release_path.write_text(json.dumps(release), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed interpreter running repository-owned workflow text
        [sys.executable, "-c", source, str(marker_path), str(release_path), version],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_macos_staple_short_circuit_requires_every_artefact_to_be_verified(tmp_path: Path) -> None:
    version = "0.1.0"
    arm_name = f"Modulo-{version}-macos-arm64.dmg"
    x86_name = f"Modulo-{version}-macos-x86_64.dmg"
    arm_bytes, x86_bytes = b"arm64-bytes", b"x86_64-bytes"
    arm_sha = hashlib.sha256(arm_bytes).hexdigest()
    x86_sha = hashlib.sha256(x86_bytes).hexdigest()
    release = {
        "assets": [
            {"name": arm_name, "digest": f"sha256:{arm_sha}", "size": len(arm_bytes)},
            {"name": x86_name, "digest": f"sha256:{x86_sha}", "size": len(x86_bytes)},
        ]
    }

    def artefact(name: str, sha: str, size: int, verified: bool | None) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": name, "sha256": sha, "size": size}
        if verified is not None:
            entry["staple_verified"] = verified
        return entry

    both_verified = {
        "version": version,
        "artifacts": [
            artefact(arm_name, arm_sha, len(arm_bytes), True),
            artefact(x86_name, x86_sha, len(x86_bytes), True),
        ],
    }
    assert _resolve_marker_state(tmp_path, version, both_verified, release) == "complete"

    # Matching digests alone are not enough: an unverified artefact is still work.
    partial = {
        "version": version,
        "artifacts": [
            artefact(arm_name, arm_sha, len(arm_bytes), True),
            artefact(x86_name, x86_sha, len(x86_bytes), False),
        ],
    }
    state = _resolve_marker_state(tmp_path, version, partial, release)
    assert state.startswith("stale:")
    assert "was never staple-verified" in state
    assert x86_name in state

    # A legacy marker with no verification flag must never be trusted.
    legacy = {
        "version": version,
        "artifacts": [
            artefact(arm_name, arm_sha, len(arm_bytes), None),
            artefact(x86_name, x86_sha, len(x86_bytes), None),
        ],
    }
    assert _resolve_marker_state(tmp_path, version, legacy, release).startswith("stale:")

    # Omitting an artefact outright is incomplete, not complete-by-omission.
    incomplete = {
        "version": version,
        "artifacts": [artefact(arm_name, arm_sha, len(arm_bytes), True)],
    }
    state = _resolve_marker_state(tmp_path, version, incomplete, release)
    assert state.startswith("stale:")
    assert x86_name in state

    # Verified but different live bytes still forces a re-staple.
    rebuilt = {
        "assets": [
            {"name": arm_name, "digest": f"sha256:{arm_sha}", "size": len(arm_bytes)},
            {"name": x86_name, "digest": f"sha256:{'0' * 64}", "size": len(x86_bytes)},
        ]
    }
    state = _resolve_marker_state(tmp_path, version, both_verified, rebuilt)
    assert state.startswith("stale:")
    assert x86_name in state


def test_release_unified_staple_dispatch_is_non_fatal() -> None:
    workflow = yaml.safe_load(_release_workflow_text())
    job = workflow["jobs"]["dispatch-staple-retry"]
    step = next(s for s in job["steps"] if "gh workflow run macos-staple-retry.yml" in str(s.get("run", "")))
    script = step["run"]

    assert "for attempt in 1 2 3" in script, "the dispatch must retry"
    assert "::warning::" in script, "a final dispatch failure must be reported, not silent"
    assert script.rstrip().endswith("exit 0"), "a dispatch failure must not fail a published release"
    # A failed post-publication dispatch must never be masked with a banned
    # `continue-on-error` (structural check - the script comments name the ban).
    assert not job.get("continue-on-error")
    for job_step in job["steps"]:
        assert not job_step.get("continue-on-error")


def test_macos_staple_retry_python_heredocs_compile() -> None:
    workflow = _staple_retry_workflow()
    scripts = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str) and "<<'PY'" in step["run"]
    ]
    assert scripts, "the staple retry workflow must embed its Python logic"
    for script in scripts:
        compile(_python_heredoc(script, "PY"), str(STAPLE_RETRY_WORKFLOW), "exec")


def test_macos_staple_retry_manifest_heredoc_compiles_as_python() -> None:
    workflow = _staple_retry_workflow()
    scripts = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str) and "build_release_manifest" in step["run"]
    ]
    assert scripts, "the republish job must regenerate the signed manifest"
    for script in scripts:
        compile(_python_heredoc(script, "PY"), str(STAPLE_RETRY_WORKFLOW), "exec")


def test_release_unified_dispatches_the_staple_retry() -> None:
    workflow = yaml.safe_load(_release_workflow_text())

    dispatch_jobs = [
        job_id
        for job_id, job in workflow["jobs"].items()
        if any("macos-staple-retry.yml" in str(step.get("run", "")) for step in job.get("steps", []))
    ]
    assert dispatch_jobs, "release-unified must consume the staple retry path"
    dispatch_job = workflow["jobs"][dispatch_jobs[0]]
    assert "publish-release" in dispatch_job["needs"]
    assert "actions" in dispatch_job.get("permissions", {})
    assert dispatch_job["permissions"]["actions"] == "write"
    text = _release_workflow_text()
    assert "gh workflow run macos-staple-retry.yml" in text


# ---------------------------------------------------------------------------
# FAR-2483: the final manifest must survive downloading every large installer,
# and publication must stay fail-closed when the manifest did not succeed.
# ---------------------------------------------------------------------------


def _release_workflow() -> dict[str, Any]:
    return yaml.safe_load(_release_workflow_text())


def _bash_executable() -> str:
    """Return a bash that can execute the workflow's POSIX guard script.

    Git for Windows ships bash, but the plain ``bash`` on PATH there is often
    the WSL launcher; prefer Git's own shell when it exists, then fall back to
    PATH (which is the only option on the Linux CI runner).
    """
    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        for candidate in (
            Path(program_files) / "Git" / "bin" / "bash.exe",
            Path(program_files) / "Git" / "usr" / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    found = shutil.which("bash")
    if found is None:
        pytest.skip("bash is required to execute the publication guard script")
    return found


def test_final_release_manifest_timeout_fits_the_large_macos_installers() -> None:
    workflow = _release_workflow()
    timeout = workflow["jobs"]["final-release-manifest"]["timeout-minutes"]

    # The job downloads the linux bundle plus BOTH ~400 MB macOS DMGs (~1 GB
    # total) before hashing and packaging them. Run 42606704332 had both
    # installers built and smoke-tested but ran out of time mid-download of the
    # x86_64 DMG at the old 15-minute cap, so the manifest was cancelled and
    # publication refused. Keep a floor well above the observed download time.
    assert isinstance(timeout, int)
    assert timeout >= 30


def test_final_release_manifest_hashes_each_artefact_without_loading_it_into_memory() -> None:
    workflow = _release_workflow()
    script = _step(workflow, "final-release-manifest", "Build final signed manifest after every platform artifact")[
        "run"
    ]

    # Reading a ~400 MB DMG with read_bytes() holds the whole blob in memory;
    # the manifest hashes them one at a time, streaming a single chunk.
    assert "read_bytes()" not in script
    assert "hashlib.file_digest" in script
    # The embedded Python must still compile so a broken edit fails the suite.
    assert "<<'PY'" in script
    compile(_python_heredoc(script, "PY"), str(RELEASE_WORKFLOW), "exec")


def test_publish_release_refuses_when_a_dependency_did_not_succeed(
    tmp_path: Path,
) -> None:
    workflow = _release_workflow()
    guard = _step(workflow, "publish-release", "Validate publication dependencies")["run"]
    script_path = tmp_path / "guard.sh"
    script_path.write_text(guard, encoding="utf-8", newline="\n")

    success_env = {
        "RELEASE_RESULT": "success",
        "MANIFEST_RESULT": "success",
        "ATTEST_RESULT": "success",
        "MACOS_RESULT": "success",
    }
    completed = subprocess.run(  # noqa: S603 - fixed interpreter running repository-owned workflow text
        [_bash_executable(), str(script_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, **success_env},
    )
    assert completed.returncode == 0
    assert "All publication dependencies succeeded." in completed.stdout


@pytest.mark.parametrize(
    ("env_var", "job_name", "result"),
    [
        ("MANIFEST_RESULT", "final-release-manifest", "cancelled"),
        ("MANIFEST_RESULT", "final-release-manifest", "failure"),
        ("RELEASE_RESULT", "release", "failure"),
        ("ATTEST_RESULT", "attest-artefacts", "skipped"),
        ("MACOS_RESULT", "macos-installer", "cancelled"),
    ],
)
def test_publish_release_blocks_publication_for_every_unsuccessful_dependency(
    env_var: str,
    job_name: str,
    result: str,
    tmp_path: Path,
) -> None:
    workflow = _release_workflow()
    guard = _step(workflow, "publish-release", "Validate publication dependencies")["run"]
    script_path = tmp_path / "guard.sh"
    script_path.write_text(guard, encoding="utf-8", newline="\n")

    env = {
        **os.environ,
        "RELEASE_RESULT": "success",
        "MANIFEST_RESULT": "success",
        "ATTEST_RESULT": "success",
        "MACOS_RESULT": "success",
        env_var: result,
    }
    completed = subprocess.run(  # noqa: S603 - fixed interpreter running repository-owned workflow text
        [_bash_executable(), str(script_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )

    assert completed.returncode == 1
    # GitHub Actions reads the `::error::` annotation from stdout, so the guard's
    # refusal is emitted there rather than on stderr.
    assert f"Publication dependency '{job_name}' did not succeed (result: {result})" in completed.stdout
    assert "refusing to publish" in completed.stdout


def test_release_unified_keeps_macos_hard_gates_while_consume_retry() -> None:
    workflow = yaml.safe_load(_release_workflow_text())

    # The publication dependency on macos-installer is unchanged: a hard gate
    # failure (codesign, Gatekeeper, hdiutil) still fails the release. Only
    # stapling is retried out of band.
    final_job = workflow["jobs"]["final-release-manifest"]
    condition = " ".join(str(final_job["if"]).split())
    assert "needs.macos-installer.result == 'success'" in condition
    assert "macos-installer" in workflow["jobs"]["publish-release"]["needs"]

    guard_names = [step.get("name") for step in workflow["jobs"]["publish-release"]["steps"]]
    assert "Validate publication dependencies" in guard_names
    guard_index = guard_names.index("Validate publication dependencies")
    guard_script = workflow["jobs"]["publish-release"]["steps"][guard_index]["run"]
    assert "macos-installer" in guard_script

    # The hard gates themselves still live in macos-installer.yml.
    installer = MACOS_INSTALLER_WORKFLOW.read_text(encoding="utf-8")
    assert "app_codesign_verify codesign --verify" in installer
    assert "mounted_app_spctl spctl --assess" in installer
    assert "dmg_verify hdiutil verify" in installer
    assert "dmg_staple_validate xcrun stapler validate" in installer


# ---------------------------------------------------------------------------
# FAR-2484: the release proves schema freshness on the EXACT commit itself.
#
# A release samples a main commit that may be seconds old, and merges land
# through the merge queue with GITHUB_TOKEN - which does not trigger push
# workflows - so a completed ci.yml run for that exact commit usually does not
# exist. Requiring one aborted a healthy cut (run 42673260700 on 5ea37c5, where
# the only run for the commit was an unrelated single-job re-run). The release
# workflow now dispatches ci.yml against a temporary tag pinned at the exact
# commit when no run proves the gate, waits (bounded) for the Schema freshness
# job, and always deletes the tag. These tests execute that proof logic.
# ---------------------------------------------------------------------------

_SCHEMA_PROOF_STEPS = {
    "schema-gate": "Ensure schema freshness CI on the exact commit",
    "release": "Assert schema-freshness gate green on exact cut/recut commit",
}
_SCHEMA_PROOF_TARGET = "d1c4e2b4a9f04f7a8e1c2d3b4a5f60798a9b0c1d"
_SCHEMA_GREEN_RUN = {"databaseId": 11, "status": "completed", "conclusion": "success"}
_SCHEMA_GREEN_JOBS = {"11": [{"name": "Schema freshness", "status": "completed", "conclusion": "success"}]}


def _schema_proof_script(job_id: str) -> str:
    return _step(_release_workflow(), job_id, _SCHEMA_PROOF_STEPS[job_id])["run"]


def _schema_proof_python(job_id: str) -> str:
    return _python_heredoc(_schema_proof_script(job_id), "PY")


def _schema_proof_module(tmp_path: Path) -> Any:
    path = tmp_path / "schema_freshness_proof.py"
    path.write_text(_schema_proof_python("release"), encoding="utf-8")
    return _load_module("schema_freshness_proof", path)


def _fake_gh(
    calls: list[list[str]],
    *,
    runs: list[dict[str, Any]],
    jobs: dict[str, list[dict[str, Any]]],
    runs_after_dispatch: list[dict[str, Any]] | None = None,
    jobs_after_dispatch: dict[str, list[dict[str, Any]]] | None = None,
    list_sequence: list[list[dict[str, Any]]] | None = None,
    list_returncode: int = 0,
    create_returncode: int = 0,
    existing_tags: list[str] | None = None,
) -> Any:
    """Stand-in for the ``gh`` CLI the proof shells out to.

    ``list_sequence`` (when supplied) is consumed one entry per ``gh run list``
    call, which is how a test models a run that changes state between polls.
    """
    dispatched = [False]
    sequence = list(list_sequence) if list_sequence is not None else None

    def gh(*args: str) -> Any:
        calls.append(list(args))
        if args[:3] == ("api", "-X", "POST"):
            if create_returncode != 0:
                return SimpleNamespace(returncode=create_returncode, stdout="", stderr="gh: HTTP 502")
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if args[:2] == ("api", "-X"):
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if args[0] == "api" and any("matching-refs" in arg for arg in args):
            payload = [[{"ref": "refs/tags/" + name} for name in (existing_tags or [])]]
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if args[:2] == ("workflow", "run"):
            dispatched[0] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ("run", "list", "--workflow"):
            if list_returncode != 0:
                return SimpleNamespace(returncode=list_returncode, stdout="", stderr="gh: API unavailable")
            if sequence is not None:
                payload = sequence.pop(0) if sequence else []
            elif dispatched[0] and runs_after_dispatch is not None:
                payload = runs_after_dispatch
            else:
                payload = runs
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if args[0] == "api" and "/actions/runs/" in args[1]:
            run_id = args[1].split("/actions/runs/")[1].split("/")[0]
            table = jobs_after_dispatch if dispatched[0] and jobs_after_dispatch is not None else jobs
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"jobs": table.get(run_id, [])}),
                stderr="",
            )
        raise AssertionError(f"unexpected gh invocation: {args}")

    return gh


def _run_schema_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: Any,
    *,
    max_wait_seconds: int = 2,
) -> tuple[int, str]:
    module = _schema_proof_module(tmp_path)
    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setenv("GITHUB_REPOSITORY", "farnalabs/modulo")
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    monkeypatch.setenv("SCHEMA_GATE_POLL_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("SCHEMA_GATE_MAX_WAIT_SECONDS", str(max_wait_seconds))
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        try:
            module.main([_SCHEMA_PROOF_TARGET])
        except SystemExit as exc:
            return (int(exc.code or 0), stdout.getvalue())
    return (0, stdout.getvalue())


def _tag_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:3] == ["api", "-X", "POST"]]


def _dispatches(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:2] == ["workflow", "run"]]


def test_release_schema_freshness_proof_is_wired_into_both_jobs() -> None:
    workflow = _release_workflow()
    discovery_script = _schema_proof_script("schema-gate")
    release_script = _schema_proof_script("release")

    # The exact-commit proof is a real, executable step in both the dedicated
    # schema-gate job and the release job, and both carry the SAME logic - a fix
    # applied to one cannot silently skip the other (the preflight gate and the
    # tagging gate).
    assert "python3 - \"$TARGET_SHA\" <<'PY'" in discovery_script
    assert "python3 - \"$TARGET_SHA\" <<'PY'" in release_script
    assert _schema_proof_python("schema-gate") == _schema_proof_python("release")
    compile(_schema_proof_python("schema-gate"), str(RELEASE_WORKFLOW), "exec")

    # Only the dedicated schema-gate job holds the write token the proof needs
    # to create/delete the temporary tag and dispatch ci.yml. The discovery gate
    # must never run with a write token.
    schema_gate_permissions = workflow["jobs"]["schema-gate"]["permissions"]
    assert schema_gate_permissions["actions"] == "write"
    assert schema_gate_permissions["contents"] == "write"
    preflight_permissions = workflow["jobs"]["preflight"]["permissions"]
    assert preflight_permissions.get("contents") == "read"
    # The discovery gate runs scripts/run_release_preflight.py, which queries the
    # Actions API (gh run list --workflow, gh api .../actions/runs/<id>/jobs).
    # GitHub sets unlisted permissions to `none`, so the key must be PRESENT and
    # exactly `read` -- an absent key is not a safe default here.
    assert preflight_permissions.get("actions") == "read"
    assert workflow["jobs"]["preflight"]["needs"] == ["schema-gate"]
    preflight_checkout = workflow["jobs"]["preflight"]["steps"][0]
    assert preflight_checkout["with"]["persist-credentials"] is False

    # The release job tags releases, so it already holds both.
    release_permissions = workflow["jobs"]["release"]["permissions"]
    assert release_permissions["actions"] == "write"
    assert release_permissions["contents"] == "write"

    step = _step(workflow, "schema-gate", _SCHEMA_PROOF_STEPS["schema-gate"])
    assert step["if"] == "inputs.mode != 'dry-run'"
    assert step["env"]["TARGET_SHA"] == "${{ github.sha }}"

    release_step = _step(workflow, "release", _SCHEMA_PROOF_STEPS["release"])
    assert release_step["id"] == "schema-freshness"
    assert release_step["env"]["MODE"] == "${{ inputs.mode }}"
    assert release_step["env"]["CUT_SHA"] == "${{ steps.cut-commit.outputs.cut_sha }}"


def test_release_schema_freshness_proof_dispatches_ci_for_the_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    gh = _fake_gh(
        calls,
        runs=[],
        jobs={},
        runs_after_dispatch=[_SCHEMA_GREEN_RUN],
        jobs_after_dispatch=_SCHEMA_GREEN_JOBS,
    )

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 0
    assert "Schema freshness passed on exact target " + _SCHEMA_PROOF_TARGET in stdout
    # A temporary ref is created AT the exact commit, ci.yml is dispatched on
    # it, and the ref is deleted again - all pinned to the commit being cut.
    [tag_create] = _tag_calls(calls)
    assert tag_create[3] == "repos/farnalabs/modulo/git/refs"
    assert any(arg.startswith("ref=refs/tags/ci-verify-" + _SCHEMA_PROOF_TARGET[:12]) for arg in tag_create)
    assert any(arg == "sha=" + _SCHEMA_PROOF_TARGET for arg in tag_create)
    [dispatch] = _dispatches(calls)
    assert dispatch[2] == "ci.yml"
    assert dispatch[4].startswith("ci-verify-" + _SCHEMA_PROOF_TARGET[:12])
    deletes = [call for call in calls if call[:3] == ["api", "-X", "DELETE"]]
    assert deletes, "the temporary verification tag must be deleted"
    assert "refs/tags/" + dispatch[4] in deletes[0][3]


def test_release_schema_freshness_proof_does_not_read_an_unrelated_run_as_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The observed failure shape: the commit's only ci.yml run is a re-run of
    # one unrelated failed job, so it has no Schema freshness job at all.
    # That run is not evidence the gate failed - the proof must dispatch a
    # real run instead of reporting "not found".
    unrelated_run = {"databaseId": 9, "status": "completed", "conclusion": "failure"}
    calls: list[list[str]] = []
    gh = _fake_gh(
        calls,
        runs=[unrelated_run],
        jobs={
            "9": [
                {
                    "name": "Trigger Branch Fixer on main CI failure",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        runs_after_dispatch=[_SCHEMA_GREEN_RUN],
        jobs_after_dispatch=_SCHEMA_GREEN_JOBS,
    )

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 0
    assert _dispatches(calls), "an unrelated run must trigger a real CI dispatch, not a failure"
    assert "Schema freshness passed on exact target " + _SCHEMA_PROOF_TARGET in stdout


def test_release_schema_freshness_proof_waits_for_a_pending_run_without_duplicating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_run = {"databaseId": 12, "status": "in_progress", "conclusion": None}
    calls: list[list[str]] = []
    gh = _fake_gh(
        calls,
        runs=[],
        jobs={
            "12": [{"name": "Schema freshness", "status": "in_progress", "conclusion": None}],
            **_SCHEMA_GREEN_JOBS,
        },
        list_sequence=[[pending_run], [_SCHEMA_GREEN_RUN], [_SCHEMA_GREEN_RUN]],
    )

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 0
    assert not _dispatches(calls), "an in-flight Schema freshness job must be waited for, not duplicated"
    assert "Schema freshness passed on exact target " + _SCHEMA_PROOF_TARGET in stdout


def test_release_schema_freshness_proof_fails_closed_on_a_red_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    gh = _fake_gh(
        calls,
        runs=[{"databaseId": 8, "status": "completed", "conclusion": "failure"}],
        jobs={"8": [{"name": "Schema freshness", "status": "completed", "conclusion": "failure"}]},
    )

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 1
    assert "::error::Schema freshness was 'failure' on exact target " + _SCHEMA_PROOF_TARGET in stdout
    assert "refusing to tag" in stdout
    assert not _tag_calls(calls), "a red gate must never create a verification tag"


def test_release_schema_freshness_proof_times_out_and_cleans_up_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    gh = _fake_gh(calls, runs=[], jobs={}, runs_after_dispatch=[], jobs_after_dispatch={})

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 1
    assert "::error::Timed out after 2s waiting for a green Schema freshness job" in stdout
    assert "refusing to tag" in stdout
    deletes = [call for call in calls if call[:3] == ["api", "-X", "DELETE"]]
    assert deletes, "a timed-out proof must still delete the temporary verification tag"


def test_release_schema_freshness_proof_fails_closed_when_the_query_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed query is never evidence of absence.
    calls: list[list[str]] = []
    gh = _fake_gh(calls, runs=[], jobs={}, list_returncode=1)

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 1
    assert "::error::Could not confirm a CI workflow run for exact target " + _SCHEMA_PROOF_TARGET in stdout
    assert "last error" in stdout
    assert not _dispatches(calls)


def test_release_schema_freshness_proof_requires_a_successful_run_not_only_a_green_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A green Schema freshness job inside a run that concluded red is not proof
    # the commit's CI passed: the whole run must be a success.
    calls: list[list[str]] = []
    gh = _fake_gh(
        calls,
        runs=[{"databaseId": 21, "status": "completed", "conclusion": "failure"}],
        jobs={"21": [{"name": "Schema freshness", "status": "completed", "conclusion": "success"}]},
    )

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 1
    assert "concluded 'failure', not success" in stdout
    assert "refusing to tag" in stdout
    assert not _tag_calls(calls), "a red run must never be turned into a verification tag"


def test_release_schema_freshness_proof_tolerates_a_lost_create_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = "ci-verify-" + _SCHEMA_PROOF_TARGET[:12] + "-4242"
    calls: list[list[str]] = []
    gh = _fake_gh(
        calls,
        runs=[],
        jobs={},
        runs_after_dispatch=[_SCHEMA_GREEN_RUN],
        jobs_after_dispatch=_SCHEMA_GREEN_JOBS,
        create_returncode=1,
        existing_tags=[tag],
    )

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 0, stdout
    assert "create response for " + tag + " was lost" in stdout
    assert _dispatches(calls), "the tag already exists, so ci.yml must still be dispatched"
    deletes = [call for call in calls if call[:3] == ["api", "-X", "DELETE"]]
    assert deletes, "a tag that existed must still be deleted"


def test_release_schema_freshness_proof_fails_when_the_tag_was_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    gh = _fake_gh(calls, runs=[], jobs={}, create_returncode=1, existing_tags=[])

    code, stdout = _run_schema_proof(tmp_path, monkeypatch, gh)

    assert code == 1
    assert "::error::Could not create the temporary CI-verification tag" in stdout
    assert not _dispatches(calls)


def test_release_schema_gate_cleanup_reclaims_this_runs_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _release_workflow()
    job = workflow["jobs"]["schema-gate-cleanup"]
    assert job["if"] == "always()"
    assert job["permissions"]["contents"] == "write"
    assert set(job["needs"]) == {"schema-gate", "release"}
    script = _step(workflow, "schema-gate-cleanup", "Delete any ci-verify-* tag this run created")["run"]
    module_path = tmp_path / "ci_verify_cleanup.py"
    module_path.write_text(_python_heredoc(script, "PY"), encoding="utf-8")
    module = _load_module("ci_verify_cleanup", module_path)

    calls: list[list[str]] = []

    def gh(*args: str) -> Any:
        calls.append(list(args))
        if any("matching-refs" in arg for arg in args):
            payload = [
                [
                    {"ref": "refs/tags/ci-verify-aaaaaaaaaaaa-4242"},
                    {"ref": "refs/tags/ci-verify-bbbbbbbbbbbb-4242"},
                    {"ref": "refs/tags/ci-verify-aaaaaaaaaaaa-9999"},
                ]
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setenv("GITHUB_REPOSITORY", "farnalabs/modulo")
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    with redirect_stdout(io.StringIO()):
        module.main()

    deleted = [call[3].rsplit("/", 1)[1] for call in calls if call[:3] == ["api", "-X", "DELETE"]]
    assert sorted(deleted) == ["ci-verify-aaaaaaaaaaaa-4242", "ci-verify-bbbbbbbbbbbb-4242"]


def test_changelog_base_resolution_ignores_helper_tags() -> None:
    # A leaked ci-verify-* helper tag must never become the changelog base.
    describes = [
        line
        for line in _release_workflow_text().splitlines()
        if "git describe --tags" in line and not line.lstrip().startswith("#")
    ]
    assert len(describes) >= 3
    for line in describes:
        assert "--match 'v*'" in line, line
