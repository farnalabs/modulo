"""Contract checks for the unified GitHub Release publication paths."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"


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


def test_rc_validation_normalizes_previous_bundle_tag_for_docker_image_rollback() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "rc-validate.yml").read_text(encoding="utf-8")
    matrix = workflow[
        workflow.index("Resolve the previous published immutable bundle tag") : workflow.index(
            "Run the Docker upgrade / failure / rollback matrix"
        )
    ]

    assert "bundle_tag=\"${published_tags##*$'\\n'}\"" in matrix
    assert 'bundle-v*) old_tag="v${bundle_tag#bundle-v}" ;;' in matrix
    assert 'echo "OLD_TAG=$old_tag" >> "$GITHUB_ENV"' in matrix
    assert 'old_tag="bundle-v' not in matrix
