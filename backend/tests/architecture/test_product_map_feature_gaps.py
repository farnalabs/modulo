"""Architecture test: no dangling ``feat-*`` feature references anywhere in the repo.

The product map lives in two layers (ADR 008 + ``docs/product-map/README.md``):

- ``frontend/src/manifest.yaml`` — the machine-readable product surface. Its ``routes``
  carry ``product_map: [feat-*]`` refs that must resolve in the ``features:`` registry
  (already enforced by ``test_product_map.py``).
- ``docs/product-map/`` — the human-readable feature graph. Each behaviour-tracker
  entry is keyed by the same ``feat-*`` id in its YAML frontmatter and covers
  infra-only surfaces (e.g. ``feat-infra-health`` for the ``/healthz`` endpoints) that
  have no UI route and are therefore absent from the manifest ``features:`` registry.

This suite enforces the *reverse* invariant — the one that lets feature references
drift silently: every ``feat-*`` literal used anywhere in the shipped code and tests
must resolve against the product map (the manifest ``features:`` registry merged with
the ``docs/product-map/`` entry ids). A feature used in code but missing from both layers
is invisible to Remy's ``search_documentation`` indexer and to the feature graph; a
feature-graph entry that goes stale is dead weight.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "manifest.yaml"
PRODUCT_MAP_DIR = REPO_ROOT / "docs" / "product-map"

#: Roots whose ``feat-*`` literals must resolve against the product map.
SCAN_ROOTS = (
    REPO_ROOT / "backend" / "src",
    REPO_ROOT / "backend" / "tests",
    REPO_ROOT / "frontend" / "src",
)

_TEXT_SUFFIXES = frozenset((".py", ".ts", ".tsx", ".vue", ".js", ".yaml", ".yml"))

_FEAT_LITERAL = re.compile(r"feat-[a-z0-9]+(?:-[a-z0-9]+)*")


def _manifest_features() -> set[str]:
    with MANIFEST_PATH.open() as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), "manifest.yaml root must be a mapping"
    assert isinstance(data.get("features"), dict), "manifest.yaml must declare 'features'"
    return set(data["features"])


def _frontmatter_id(path: Path) -> str | None:
    """Return the ``id`` value from a product-map entry's YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    frontmatter, _sep, _rest = text.partition("\n---\n")
    meta = yaml.safe_load(frontmatter)
    if not isinstance(meta, dict):
        return None
    return meta.get("id")


def _product_map_entry_ids() -> set[str]:
    """Every ``feat-*`` entry node id declared in ``docs/product-map/``."""
    entry_ids = set()
    for path in PRODUCT_MAP_DIR.rglob("*.md"):
        entry_id = _frontmatter_id(path)
        if entry_id:
            entry_ids.add(entry_id)
    return entry_ids


def _feature_literals_in_root(root: Path) -> set[str]:
    """All ``feat-*`` literals referenced in *root*'s shipped text files."""
    found: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        found.update(_FEAT_LITERAL.findall(text))
    return found


def test_product_map_entry_links_resolve():
    """The ``docs/product-map/`` index only links entries that exist."""
    readme = PRODUCT_MAP_DIR / "README.md"
    if not readme.exists():
        raise AssertionError(f"product map index missing: {readme.relative_to(REPO_ROOT)}")
    links = {
        target: target
        for target in (
            match.group(1)
            for match in re.finditer(r"\[feat-[a-z0-9-]+\]\(([^)]+\.md)\)", readme.read_text(encoding="utf-8"))
        )
    }
    dangling = {target: (target.split("#")[0]) for target in links.values() if not (PRODUCT_MAP_DIR / target).exists()}
    assert not dangling, "docs/product-map/README.md links to missing feature-graph entries:\n" + "\n".join(
        f"  {target}" for target in sorted(dangling)
    )


def test_product_map_feature_references_resolve():
    """Every ``feat-*`` literal in shipped code/tests resolves against the product map.

    Registry = the manifest ``features:`` registry merged with the
    ``docs/product-map/`` entry ids. A literal that
    resolves nowhere is a feature gap: the feature shipped (or its test documents a
    shipped behaviour) but no product-map surface references it, so it is invisible to
    Remy's ``search_documentation`` indexer and to the feature graph. Register the
    feature in ``frontend/src/manifest.yaml`` (if it has routes) or add/restore a
    behaviour-tracker entry in ``docs/product-map/`` (infra-only surfaces).
    """
    resolver = _manifest_features() | _product_map_entry_ids()
    assert resolver, "product map must register at least one feature"

    dangling: dict[str, list[str]] = {}
    for root in SCAN_ROOTS:
        for literal in sorted(_feature_literals_in_root(root)):
            if literal not in resolver:
                dangling.setdefault(literal, []).append(str(root.relative_to(REPO_ROOT)))
    assert not dangling, (
        "feat-* references that resolve against no product-map feature"
        " (not in frontend/src/manifest.yaml 'features:' and no docs/product-map entry):\n"
        + "\n".join(f"  {feat} -> {', '.join(roots)}" for feat, roots in sorted(dangling.items()))
    )
