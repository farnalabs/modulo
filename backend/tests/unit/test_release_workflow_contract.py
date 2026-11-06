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
