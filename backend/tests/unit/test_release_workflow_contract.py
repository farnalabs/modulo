"""Contract checks for the unified GitHub Release publication paths."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
RC_VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rc-validate.yml"

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
    return matches[0]


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
