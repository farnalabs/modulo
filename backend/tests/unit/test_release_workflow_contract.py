"""Contract checks for the unified GitHub Release publication paths."""

import importlib.util
from pathlib import Path
from typing import Any

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
    assert matrix_start < pen_test_start


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
    assert 'published_sums="$RUNNER_TEMP/SHA256SUMS"' in matrix
    assert 'no checksum for $(basename "$downloaded_archive")' in matrix
    assert "checksum mismatch for published predecessor bundle asset" in matrix
    assert "does not derive old_tag from bundle_tag" not in matrix
