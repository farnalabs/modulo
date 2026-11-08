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


def _release_validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "validate_release_tag", REPO_ROOT / "scripts" / "validate_release_tag.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("release validator module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_workflow_text() -> str:
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def _render_matrix_compose_image(compose_text: str, tag: str) -> str:
    rendered, count = re.subn(
        r"(?m)^(\s*image:\s*ghcr\.io/farnalabs/modulo:)[^\r\n]+$",
        rf"\1{tag}",
        compose_text,
        count=1,
    )
    if count != 1:
        raise AssertionError(f"expected one modulo image selector, found {count}")
    compose = yaml.safe_load(rendered)

    return compose["services"]["modulo"]["image"]


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
    workflow = (REPO_ROOT / ".github" / "workflows" / "rc-validate.yml").read_text(encoding="utf-8")
    matrix_start = workflow.index("docker-upgrade-rollback:")
    pen_test_start = workflow.index("pen-test:")
    matrix = workflow[matrix_start:pen_test_start]

    assert "needs: [clean-host-compose]" in matrix
    assert "needs: [clean-host-compose, docker-upgrade-rollback]" in workflow[workflow.index("pen-test:") :]
    assert "scripts/verify_compose_upgrade_rollback.py" in matrix
    assert '--old-tag "$OLD_TAG"' in matrix
    assert '--new-tag "$RC_TAG"' in matrix
    assert "alpha-docker-upgrade-rollback-${{ github.run_id }}" in matrix
    assert 'replacement = "${TAG:?TAG is required - set the image tag for this matrix phase}"' in matrix
    assert "Rendered matrix compose image selector" in matrix
    assert "packaged compose asset is intentionally hard-pinned to RC_TAG" in matrix
    assert matrix_start < pen_test_start


def test_rc_validation_matrix_selects_the_image_for_each_phase() -> None:
    compose_text = """
services:
  modulo:
    image: ghcr.io/farnalabs/modulo:v1.1.0-rc1
"""
    phases = {
        "clean-install-old": "v1.0.0",
        "upgrade-new": "v1.1.0-rc1",
        "failed-upgrade": "v1.1.0-rc1",
        "rollback-old": "v1.0.0",
    }

    rendered_images = {phase: _render_matrix_compose_image(compose_text, tag) for phase, tag in phases.items()}

    assert rendered_images == {
        "clean-install-old": "ghcr.io/farnalabs/modulo:v1.0.0",
        "upgrade-new": "ghcr.io/farnalabs/modulo:v1.1.0-rc1",
        "failed-upgrade": "ghcr.io/farnalabs/modulo:v1.1.0-rc1",
        "rollback-old": "ghcr.io/farnalabs/modulo:v1.0.0",
    }
    assert rendered_images["clean-install-old"] == rendered_images["rollback-old"]
    assert rendered_images["upgrade-new"] == rendered_images["failed-upgrade"]
    assert rendered_images["clean-install-old"] != rendered_images["upgrade-new"]


def test_rc_validation_refuses_to_claim_docker_matrix_without_previous_release() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "rc-validate.yml").read_text(encoding="utf-8")
    matrix = workflow[workflow.index("docker-upgrade-rollback:") : workflow.index("pen-test:")]

    assert "no previous published bundle-v* release exists" in matrix
    assert "refusing to claim Docker upgrade/rollback coverage" in matrix


def test_rc_validation_resolves_the_previous_bundle_tag_from_the_current_release() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "rc-validate.yml").read_text(encoding="utf-8")
    matrix = workflow[
        workflow.index("Resolve the previous published immutable bundle tag") : workflow.index(
            "Run the Docker upgrade / failure / rollback matrix"
        )
    ]

    assert 'current_bundle_tag="bundle-v${version}"' in matrix
    assert "scripts/validate_release_tag.py resolve-n-minus-1" in matrix
    assert '--current "$current_bundle_tag"' in matrix
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

    assert validator.resolve_n_minus_1([releases], "bundle-v1.3.0") == "bundle-v1.2.0"


def test_rc_validation_rejects_predecessor_checksum_and_version_mismatches() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "rc-validate.yml").read_text(encoding="utf-8")
    matrix = workflow[
        workflow.index("Resolve the previous published immutable bundle tag") : workflow.index(
            "Run the Docker upgrade / failure / rollback matrix"
        )
    ]

    assert 'gh release download "$bundle_tag"' in matrix
    assert "--pattern SHA256SUMS" in matrix
    assert 'published_sums="$RUNNER_TEMP/SHA256SUMS"' in matrix
    assert "no SHA256SUMS artifact" in matrix
    assert 'no checksum for $(basename "$downloaded_archive")' in matrix
    assert "checksum mismatch for published predecessor bundle asset" in matrix
    assert 'BUNDLE_VERSION="$bundle_version"' in matrix
    assert "published predecessor bundle version" in matrix
    assert "including any RC suffix" in matrix


def test_image_digest_verification_accepts_only_published_provenance() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64
    attestation = {"verificationResult": {"subject": [{"digest": {"sha256": "a" * 64}}]}}

    assert validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, attestation) == digest


def test_image_digest_verification_fails_closed_for_missing_provenance() -> None:
    validator = _release_validator()
    digest = "sha256:" + "a" * 64

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", digest, {})


def test_image_digest_verification_ignores_unrelated_nested_sha256_metadata() -> None:
    validator = _release_validator()
    requested = "sha256:" + "a" * 64
    subject = "b" * 64
    attestation = {
        "verificationResult": {
            "subject": [{"digest": {"sha256": subject}}],
            "predicate": {"metadata": {"sha256": "a" * 64}},
        }
    }

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", requested, attestation)


def test_image_digest_verification_rejects_malformed_subject_digest() -> None:
    validator = _release_validator()
    requested = "sha256:" + "a" * 64
    attestation = {"verificationResult": {"subject": [{"digest": {"sha256": "not-a-digest"}}]}}

    with pytest.raises(validator.ReleaseValidationError, match="does not contain resolved digest"):
        validator.verify_image_digest("ghcr.io/farnalabs/modulo:v1.2.3", requested, attestation)


def test_image_digest_cli_accepts_attestation_json_and_rejects_missing_digest(
    tmp_path: Path,
) -> None:
    digest = "sha256:" + "a" * 64
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_text(
        '{"verificationResult":{"subject":[{"digest":{"sha256":"' + "a" * 64 + '"}}]}}',
        encoding="utf-8",
    )
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
    workflow = (REPO_ROOT / ".github" / "workflows" / "rc-validate.yml").read_text(encoding="utf-8")
    matrix = workflow[
        workflow.index("Resolve the previous published immutable bundle tag") : workflow.index(
            "Run the Docker upgrade / failure / rollback matrix"
        )
    ]

    assert 'gh release download "$bundle_tag"' in matrix
    assert 'compose_file="$bundle_dir/docker-compose.prod.yml"' in matrix
    assert 'image = compose.get("services", {}).get("modulo", {}).get("image")' in matrix
    assert 'old_tag="$(COMPOSE_FILE="$compose_file" BUNDLE_VERSION="$bundle_version" python3 - <<\'PY\'' in matrix
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
