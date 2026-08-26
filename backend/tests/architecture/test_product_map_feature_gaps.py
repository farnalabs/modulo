"""Architecture test: no dangling product-map feature references anywhere.

The product map lives in two layers (ADR 008 + ``docs/product-map/README.md``):

- ``frontend/src/manifest.yaml`` - the machine-readable product surface. Its ``routes``
  carry ``product_map: [feat-*]`` refs that must resolve in the ``features:`` registry
  (already enforced by ``test_product_map.py``).
- ``docs/product-map/`` - the human-readable feature graph. Each behaviour-tracker
  entry is keyed by the same ``feat-*`` id in its YAML frontmatter and covers
  infra-only surfaces (e.g. ``feat-infra-health`` for the ``/healthz`` endpoints) that
  have no UI route and are therefore absent from the manifest ``features:`` registry.
  ``docs/security/incident-response-playbook.md`` and ``CONTRIBUTING.md`` link into this
  directory.

This suite enforces the *reverse* invariant - the one that lets feature references
drift silently: every ``feat-*`` literal used anywhere in the shipped code and tests
must resolve against the product map (the manifest ``features:`` registry merged with
the ``docs/product-map/`` entry ids). A feature used in code but missing from both
layers is invisible to Remy's ``search_documentation`` indexer and to the feature graph;
a feature-graph entry that goes stale, or a documented graph path that points nowhere,
is a dangling reference.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "manifest.yaml"
PRODUCT_MAP_DIR = REPO_ROOT / "docs" / "product-map"
GRAPH_INDEX = PRODUCT_MAP_DIR / "README.md"

#: Roots whose ``feat-*`` literals must resolve against the product map.
SCAN_ROOTS = (
    REPO_ROOT / "backend" / "src",
    REPO_ROOT / "backend" / "tests",
    REPO_ROOT / "frontend" / "src",
)

_TEXT_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".vue", ".js", ".yaml", ".yml"})

_FEAT_LITERAL = re.compile(r"feat-[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER_ID = re.compile(r"^---\n.*?^id:\s*(\S+)\s*$", re.MULTILINE | re.DOTALL)
_INDEX_LINK = re.compile(r"\]\(([A-Za-z0-9_./-]+\.md)\)")
_DOC_GRAPH_REF = re.compile(r"docs/product-map/([A-Za-z0-9_./-]*)")


def _manifest_features() -> set[str]:
    with MANIFEST_PATH.open() as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), "manifest.yaml root must be a mapping"
    assert isinstance(data.get("features"), dict), "manifest.yaml must declare 'features'"
    return set(data["features"])


def _frontmatter_id(path: Path) -> str | None:
    """Return the ``id`` value from a product-map entry's YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_ID.match(text)
    if match is None:
        return None
    return match.group(1)


def _entry_frontmatter(path: Path) -> dict:
    """Parse a behaviour-tracker entry's full YAML frontmatter block."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _product_map_entry_paths() -> list[Path]:
    """Every behaviour-tracker entry (``*.md`` except the graph index)."""
    if not PRODUCT_MAP_DIR.is_dir():
        return []
    return sorted(path for path in PRODUCT_MAP_DIR.rglob("*.md") if path.resolve() != GRAPH_INDEX.resolve())


def _product_map_entry_ids() -> set[str]:
    entries = _product_map_entry_paths()
    return {entry_id for entry_id in (_frontmatter_id(p) for p in entries) if entry_id}


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


def _markdown_files() -> list[Path]:
    files = [REPO_ROOT / "CONTRIBUTING.md"]
    docs = REPO_ROOT / "docs"
    if docs.is_dir():
        files.extend(sorted(docs.rglob("*.md")))
    return [path for path in files if path.is_file()]


def test_graph_index_exists():
    assert GRAPH_INDEX.is_file(), (
        f"product map graph index missing: {GRAPH_INDEX.relative_to(REPO_ROOT)} "
        "(restore docs/product-map/README.md; CONTRIBUTING.md and the incident-response "
        "playbook link into this directory)"
    )


def test_graph_index_links_resolve():
    """The ``docs/product-map/README.md`` index only links entries that exist."""
    assert GRAPH_INDEX.is_file()
    index_text = GRAPH_INDEX.read_text(encoding="utf-8")
    missing = sorted(
        target
        for target in {match.group(1) for match in _INDEX_LINK.finditer(index_text)}
        if not (PRODUCT_MAP_DIR / target).is_file()
    )
    assert not missing, "docs/product-map/README.md links to missing feature-graph entries:\n" + "\n".join(
        f"  {target}" for target in missing
    )


def test_every_graph_entry_reachable_from_index():
    """Every behaviour-tracker entry is linked from the graph index (no orphan nodes)."""
    assert GRAPH_INDEX.is_file()
    index_text = GRAPH_INDEX.read_text(encoding="utf-8")
    linked = {match.group(1) for match in _INDEX_LINK.finditer(index_text)}
    for entry in _product_map_entry_paths():
        relative = entry.relative_to(PRODUCT_MAP_DIR).as_posix()
        assert relative in linked, (
            f"docs/product-map entry {relative} is not linked from README.md "
            "(orphaned behaviour-tracker - unreachable from the product map)"
        )


def test_graph_entry_feature_ids_are_unique():
    """Product-map entries key on unique ``id`` frontmatter values."""
    seen: dict[str, Path] = {}
    for entry in _product_map_entry_paths():
        entry_id = _frontmatter_id(entry)
        assert entry_id is not None, f"docs/product-map entry has no frontmatter id: {entry}"
        assert _FEAT_LITERAL.fullmatch(entry_id), (
            f"docs/product-map entry id {entry_id!r} in {entry} is not a feat-* id"
        )
        assert entry_id not in seen, f"duplicate docs/product-map entry id {entry_id!r}"
        seen[entry_id] = entry


#: Behaviour-tracker frontmatter fields whose values are repo-relative file paths.
_CITATION_FIELDS = ("code", "unit-tests", "bdd", "adr")


def test_entry_file_references_resolve():
    """Every ``code:`` / ``unit-tests:`` / ``bdd:`` / ``adr:`` reference resolves.

    The feature-graph contract (``docs/product-map/README.md``) points these
    fields at the code paths, test files and BDD feature files that implement
    each behaviour. A value that names a file that does not exist is a stale
    citation: the entry either claims coverage someone deleted, or (the reverse
    drift the snapshot entries suffered) under-reports a surface that lives in a
    file it never names. A trailing-slash value names a directory. ``feat-*``
    and task ids appear only in ``depends-on``/``delivery-tasks``, which are not
    file fields and are deliberately not checked here.
    """
    missing: dict[str, list[str]] = {}
    for entry in _product_map_entry_paths():
        frontmatter = _entry_frontmatter(entry)
        for field in _CITATION_FIELDS:
            for ref in frontmatter.get(field) or []:
                if not isinstance(ref, str) or not ref.strip():
                    continue
                resolved = REPO_ROOT / ref.rstrip("/")
                if not resolved.is_file() and not resolved.is_dir():
                    missing.setdefault(entry.relative_to(REPO_ROOT).as_posix(), []).append(f"{field}: {ref}")
    assert not missing, (
        "docs/product-map entries cite paths that do not exist in the repo"
        " (stale code/bdd/unit-test coverage claims):\n"
        + "\n".join(f"  {entry} -> {refs}" for entry, refs in sorted(missing.items()))
    )


def test_feature_references_resolve():
    """Every ``feat-*`` literal in shipped code/tests resolves against the product map.

    Registry = the manifest ``features:`` registry merged with the
    ``docs/product-map/`` entry ids. A literal that resolves nowhere is a feature gap:
    the feature shipped (or its test documents a shipped behaviour) but no product-map
    surface references it, so it is invisible to Remy's ``search_documentation`` indexer
    and to the feature graph. Register the feature in ``frontend/src/manifest.yaml`` (if
    it has routes) or add/restore a behaviour-tracker entry in ``docs/product-map/``
    (infra-only surfaces).
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


def _feature_literals_in_docs() -> dict[str, list[str]]:
    """Every ``feat-*`` literal referenced anywhere under ``docs/``."""
    docs = REPO_ROOT / "docs"
    if not docs.is_dir():
        return {}
    found: dict[str, list[str]] = {}
    for path in sorted(docs.rglob("*.md")):
        if path.suffix != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for literal in sorted(_FEAT_LITERAL.findall(text)):
            found.setdefault(literal, []).append(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_documentation_feature_references_resolve():
    """Every ``feat-*`` literal in docs resolves against the product map.

    Docs are consumed by Remy's ``search_documentation`` indexer and the feature
    graph's behaviour trackers carry typed ``depends-on`` edges between ``feat-*``
    nodes. A ``feat-*`` id used in documentation but missing from the manifest
    ``features:`` registry and from every ``docs/product-map/`` entry id is a dangling
    graph edge: the reader (or the graph) points at a feature that has no node.
    Register infra-only features as behaviour-tracker entries (``docs/product-map/``);
    route-referenced features belong in the manifest registry.
    """
    resolver = _manifest_features() | _product_map_entry_ids()
    assert resolver, "product map must register at least one feature"

    dangling = {feat: docs for feat, docs in sorted(_feature_literals_in_docs().items()) if feat not in resolver}
    assert not dangling, (
        "feat-* references in docs that resolve against no product-map feature"
        " (not in frontend/src/manifest.yaml 'features:' and no docs/product-map entry):\n"
        + "\n".join(f"  {feat} -> {', '.join(docs)}" for feat, docs in sorted(dangling.items()))
    )


def test_documented_graph_paths_resolve():
    """Every ``docs/product-map/...`` path referenced in shipped docs resolves.

    The incident-response playbook and CONTRIBUTING.md link into this directory by path;
    a reference to a removed entry (or to the directory itself after it was dropped) is
    a dangling docs link. This is the regression guard that keeps the graph restorable
    and its links live.
    """
    missing: dict[str, list[str]] = {}
    for text_path in _markdown_files():
        text = text_path.read_text(encoding="utf-8")
        for target in {match.group(1) for match in _DOC_GRAPH_REF.finditer(text)}:
            if not target:
                continue
            resolved = PRODUCT_MAP_DIR
            if target.endswith(".md"):
                resolved = PRODUCT_MAP_DIR / target
            exists = resolved.is_file() if target.endswith(".md") else resolved.is_dir()
            if not exists:
                missing.setdefault(f"docs/product-map/{target}", []).append(text_path.relative_to(REPO_ROOT).as_posix())
    assert not missing, "docs reference docs/product-map/ files that do not exist:\n" + "\n".join(
        f"  {target} -> {', '.join(docs)}" for target, docs in sorted(missing.items())
    )
