"""Architecture test: the product map feature registry stays consistent.

``frontend/src/manifest.yaml`` declares a ``product_map`` ``feat-*`` reference
for every shipped route (per ADR 008 — Core Shared Manifest). The registry of
allowed features lives in the same file under the top-level ``features:`` map.
Remy's ``search_documentation`` tool indexes each route's ``product_map`` refs,
so a route that drops its reference (or references a feature that no longer
exists) silently shrinks what the assistant can find, while a feature that is
never referenced is dead weight in the registry.

This suite pins the invariants a healthy product map must keep:

- every route carries a non-empty ``product_map`` reference list;
- every reference resolves against the ``features:`` registry;
- every registered feature is referenced by at least one route;
- the documentation indexer surfaces ``product_map`` refs in the index it
  builds, so features are searchable end to end.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "manifest.yaml"


def _load_manifest() -> dict:
    with MANIFEST_PATH.open() as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise AssertionError("manifest.yaml root must be a mapping")
    if not isinstance(data.get("routes"), dict):
        raise AssertionError("manifest.yaml must declare a 'routes' mapping")
    if not isinstance(data.get("features"), dict):
        raise AssertionError("manifest.yaml must declare a 'features' mapping")
    return data


def test_every_route_declares_product_map_reference():
    routes = _load_manifest()["routes"]
    missing = {}
    for path, entry in routes.items():
        if not isinstance(entry, dict):
            missing[path] = "<entry is not a mapping>"
            continue
        refs = entry.get("product_map")
        if not refs:
            missing[path] = "<product_map is empty>"
        elif isinstance(refs, (list, tuple)) and not refs:
            missing[path] = "<product_map is empty list>"
    assert not missing, "manifest routes must declare at least one product_map feature reference:\n" + "\n".join(
        f"  {path} -> {ref}" for path, ref in sorted(missing.items())
    )


def test_product_map_references_resolve_against_registry():
    data = _load_manifest()
    registry = set(data["features"])
    dangling = {}
    for path, entry in data["routes"].items():
        refs = entry.get("product_map") if isinstance(entry, dict) else None
        if not refs:
            continue
        if isinstance(refs, str):
            refs = [refs]
        unresolved = [r for r in refs if r not in registry]
        if unresolved:
            dangling[path] = unresolved
    assert not dangling, "manifest routes reference features missing from the 'features' registry:\n" + "\n".join(
        f"  {path} -> {refs}" for path, refs in sorted(dangling.items())
    )


def test_every_registered_feature_is_referenced_by_a_route():
    data = _load_manifest()
    referenced = {
        ref
        for entry in data["routes"].values()
        if isinstance(entry, dict)
        for ref in (entry.get("product_map") or [])
        if isinstance(ref, str)
    }
    unreferenced = sorted(set(data["features"]) - referenced)
    assert not unreferenced, "registered features with no route reference (dead registry entries):\n" + "\n".join(
        f"  {feat}" for feat in unreferenced
    )


def test_documentation_indexer_surfaces_product_map_features():
    from modulo.core.documentation_indexer import DocumentationIndex

    index = DocumentationIndex.build(MANIFEST_PATH)
    by_path = {entry.heading_path: entry for entry in index.entries}
    assert by_path, "docs index built from the manifest must not be empty"

    missing_features = [
        path for path, entry in by_path.items() if "features" in entry.__dataclass_fields__ and not entry.features
    ]
    assert not missing_features, "docs index entries must carry their route's product_map features:\n" + "\n".join(
        f"  {path}" for path in sorted(missing_features)
    )

    costs = by_path.get("/admin/costs")
    assert costs is not None
    assert "feat-costs" in costs.features
    assert "product_map=feat-costs" in costs.first_paragraph
