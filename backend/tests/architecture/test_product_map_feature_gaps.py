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
layers is invisible to Assistant's ``search_documentation`` indexer and to the feature graph;
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


#: Section markers of the graph root's two indexes (see README.md). The docs
#: convention forbids em dashes (U+2014) and normalises them to en dashes
#: (U+2013) or hyphens (U+002D), so the patterns accept any of the three rather
#: than pinning one dash character (the docs-improvement pipeline rewrites the
#: headings, which previously broke this guard while CI stayed otherwise green).
_REGISTRY_INDEX_DASH = "[-\u2013\u2014]"
_REGISTRY_INDEX_START_RE = re.compile(rf"^## Index\s+{_REGISTRY_INDEX_DASH}\s+manifest feature registry", re.MULTILINE)
_REGISTRY_INDEX_END_RE = re.compile(rf"^## Index\s+{_REGISTRY_INDEX_DASH}\s+feature graph entries", re.MULTILINE)
_REGISTRY_INDEX_TOKEN = "**{feature}**"

#: A registry-index line: ``- **feat-<id>** - <description> - routes: `a`, `b```.
_REGISTRY_ROUTE_LINE = re.compile(
    r"^- \*\*(?P<feature>feat-[a-z0-9-]+)\*\* - .*? - routes: (?P<routes>.+)$",
    re.MULTILINE,
)


def _registry_index_section() -> str:
    """Return the text between the graph root's two index headings.

    The docs-improvement pipeline normalises the headings' dash to an en dash (or
    hyphen), so match any dash form: this guard is about the section existing and
    being complete, not the cosmetic character between "Index" and its subject.
    """
    index_text = GRAPH_INDEX.read_text(encoding="utf-8")
    start = _REGISTRY_INDEX_START_RE.search(index_text)
    assert start is not None, "graph root must declare the 'Index — manifest feature registry' section"
    end = _REGISTRY_INDEX_END_RE.search(index_text, start.end())
    assert end is not None, "graph root must declare the 'Index — feature graph entries' section"
    return index_text[start.end() : end.start()]


def _registry_index_routes() -> dict[str, list[str]]:
    """Map each graph-root registry feature to the routes listed on its line."""
    section = _registry_index_section()
    return {
        match.group("feature"): re.findall(r"`([^`]+)`", match.group("routes"))
        for match in _REGISTRY_ROUTE_LINE.finditer(section)
    }


def _manifest_feature_routes() -> dict[str, list[tuple[str, str | None]]]:
    """Map each manifest feature to its ``(route, visibility)`` pairs."""
    with MANIFEST_PATH.open() as handle:
        data = yaml.safe_load(handle)
    routes = data.get("routes") if isinstance(data, dict) else None
    assert isinstance(routes, dict), "manifest.yaml must declare a 'routes' mapping"
    by_feature: dict[str, list[tuple[str, str | None]]] = {}
    for path, entry in routes.items():
        if not isinstance(entry, dict):
            continue
        for feature in entry.get("product_map") or []:
            by_feature.setdefault(feature, []).append((path, entry.get("visibility")))
    return by_feature


def test_graph_root_registry_index_enumerates_every_manifest_feature():
    """The graph root's "manifest feature registry" index lists every registered feature.

    ``docs/product-map/README.md`` is the root of the feature graph. Its first
    index is the human-readable mirror of the ``frontend/src/manifest.yaml``
    ``features:`` registry; the README itself promises "Fresh entries for these
    features are added to the graph below as behaviour trackers". A feature
    registered in the manifest but missing from that index is invisible to a
    reader navigating the graph — the product map ships it but its root does not
    point at it (the ``feat-router`` gap this guard closes). The manifest
    registry must be a strict subset of the index.
    """
    assert GRAPH_INDEX.is_file()
    index_section = _registry_index_section()
    missing = sorted(
        feature
        for feature in _manifest_features()
        if _REGISTRY_INDEX_TOKEN.format(feature=feature) not in index_section
    )
    assert not missing, (
        "manifest-registered features missing from the graph-root registry index "
        "(add each to the 'Index — manifest feature registry' section of "
        + GRAPH_INDEX.relative_to(REPO_ROOT).as_posix()
        + " so the graph root enumerates the full product surface):\n"
        + "\n".join(f"  {feature}" for feature in missing)
    )


def test_graph_root_registry_routes_match_manifest():
    """The registry index's ``routes:`` lists are a sound, complete public view.

    Each ``- **feat-<id>** ... - routes: ...`` line in the graph root's "manifest
    feature registry" index is the human-readable route surface for that feature.
    The manifest is the source of truth, but nothing pinned the two together, so
    the lists drifted in both directions after the FAR-591 D5 Runners-page rename
    and the FAR-760 library collections ship: the index still named
    ``/admin/environments``, ``/admin/sandbox-concurrency`` and
    ``/environment-profiles*`` (routes that no longer exist in the manifest) and
    omitted ``/admin/runners/*``, ``/accept-invite`` and
    ``/library/collections/*``. A reader (or Assistant's docs indexer) following the
    graph root to a dead route is a dangling edge.

    Two invariants keep the lists honest:

    - **soundness** — every route named in the index exists in the manifest and is
      tagged with the feature on that line (no stale/foreign routes);
    - **completeness** — every route the manifest exposes *publicly* (visibility is
      not ``private_preview``) is named on its feature's line. Features whose whole
      surface is ``private_preview`` (``feat-assistant``, ``feat-plugins``,
      ``feat-feedback``) still list their routes, so this only constrains public
      routes; deferred routes may legitimately be summarised in prose instead.
    """
    listing = _registry_index_routes()
    assert listing, "graph root registry index must list features with route lists"
    by_feature = _manifest_feature_routes()

    with MANIFEST_PATH.open() as handle:
        routes = yaml.safe_load(handle)["routes"]

    unknown: dict[str, list[str]] = {}
    misattributed: dict[str, list[str]] = {}
    for feature, paths in listing.items():
        tagged = {path for path, _ in by_feature.get(feature, [])}
        for path in paths:
            if path not in routes:
                unknown.setdefault(feature, []).append(path)
            elif path not in tagged:
                misattributed.setdefault(feature, []).append(path)
    assert not unknown, (
        "graph-root registry index names routes that do not exist in "
        + MANIFEST_PATH.relative_to(REPO_ROOT).as_posix()
        + " (stale route citations):\n"
        + "\n".join(f"  {feature} -> {', '.join(paths)}" for feature, paths in sorted(unknown.items()))
    )
    assert not misattributed, (
        "graph-root registry index names routes that are not tagged with the feature "
        "on that line (misattributed routes):\n"
        + "\n".join(f"  {feature} -> {', '.join(paths)}" for feature, paths in sorted(misattributed.items()))
    )

    missing: dict[str, list[str]] = {}
    for feature, entries in by_feature.items():
        listed = set(listing.get(feature, []))
        public = {path for path, visibility in entries if visibility != "private_preview"}
        gap = public - listed
        if gap:
            missing[feature] = sorted(gap)
    assert not missing, (
        "public manifest routes missing from their feature's graph-root registry line "
        "(add each to the 'Index — manifest feature registry' section):\n"
        + "\n".join(f"  {feature} -> {', '.join(paths)}" for feature, paths in sorted(missing.items()))
    )


def test_every_manifest_feature_has_a_behaviour_tracker():
    """Every manifest-registered feature resolves to a ``docs/product-map/`` entry.

    The graph root (``docs/product-map/README.md``) closes its "Known graph gaps"
    section by promising that "All registered manifest features now have a
    ``docs/product-map/`` behaviour-tracker entry. No untracked features remain."
    A registered feature without a tracker is an untracked node: its behaviours,
    coverage and known gaps have no home in the human-readable graph even though
    its routes advertise it. ``feat-apply`` (registered by FAR-681 with four
    ``product_map`` route references) had no entry until the 2026-09-10
    product-map walk — this guard keeps the graph root's promise true.
    """
    tracked = _product_map_entry_ids()
    missing = sorted(_manifest_features() - tracked)
    assert not missing, (
        "manifest-registered features with no docs/product-map/ behaviour-tracker "
        "entry (the graph root claims every registered feature is tracked; add an "
        "entry under docs/product-map/ keyed by the feature id, or drop the feature "
        "from the manifest registry):\n" + "\n".join(f"  {feature}" for feature in missing)
    )


#: Status values the two product-map layers are allowed to carry. A manifest
#: feature WITH an explicit ``status:`` must agree with its behaviour-tracker;
#: every manifest feature today carries one (32 ``covered`` + 4 ``partial``).
def test_graph_tracker_status_matches_manifest_registry():
    """A behaviour-tracker's ``status:`` must agree with its manifest registry status.

    Both product-map layers carry a ``status:`` for features that live in the
    manifest ``features:`` registry. The manifest is the machine-readable
    source of truth Assistant indexes from; the ``docs/product-map/`` tracker is
    the human-readable layer keyed by the same ``feat-*`` id. When they
    disagree, a reader of the graph gets the opposite coverage answer from the
    machine layer — exactly the drift the 2026-09-26 walks left behind:
    ``feat-guardrails`` / ``feat-license`` / ``feat-plugins`` /
    ``feat-product-analytics`` were sharpened to ``status: partial`` in the
    manifest (an unshipped sub-surface — cross-org guardrail inheritance,
    universal license gating, registry-API lifecycle management, in-product
    analytics export — stays tracked as an unchecked deferral) while each
    tracker's frontmatter still read ``status: covered`` despite its own QA
    note saying "Status stays ``partial``". Keep the two layers honest with
    each other. Infra-only entries that have no manifest feature are
    unconstrained — the manifest registry is the source of truth for trackers
    that reference it.
    """
    with MANIFEST_PATH.open() as handle:
        manifest_features = yaml.safe_load(handle)["features"]

    mismatches: dict[str, dict[str, object]] = {}
    for entry in _product_map_entry_paths():
        entry_id = _frontmatter_id(entry)
        spec = manifest_features.get(entry_id) if isinstance(manifest_features, dict) else None
        if entry_id is None or not isinstance(spec, dict):
            continue
        manifest_status = spec.get("status")
        tracker_status = (_entry_frontmatter(entry) or {}).get("status")
        if manifest_status == tracker_status:
            continue
        mismatches[entry_id] = {
            "manifest": manifest_status,
            "tracker": tracker_status,
            "entry": entry.relative_to(REPO_ROOT).as_posix(),
        }

    assert not mismatches, (
        "behaviour-tracker frontmatter 'status' disagrees with the manifest "
        "features registry for the same feat-* id — a reader of the graph gets "
        "the opposite coverage answer from the machine layer Assistant indexes "
        "(aligned for the 2026-09-29 walk; keep the two layers in step):\n"
        + "\n".join(
            f"  {feat} -> manifest={info['manifest']!r} tracker={info['tracker']!r} in {info['entry']}"
            for feat, info in sorted(mismatches.items())
        )
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
#: ``adr`` is deliberately excluded: ADRs were migrated out of this repo to
#: private tooling on 2026-09-02 (FAR-434), so ``adr:`` citations are no longer
#: repo-relative and cannot be file-resolved here.
_CITATION_FIELDS = ("code", "unit-tests", "bdd")

_BDD_ROOT = REPO_ROOT / "backend" / "tests" / "bdd"

#: Root under which any module may register ``scenarios(...)``. Step modules are
#: not confined to ``tests/bdd/``: a module that needs real-Postgres fixtures
#: lives in ``tests/integration/`` and loads a feature from ``tests/bdd/features/``
#: by relative path (e.g. ``tests/integration/test_hitl_reject_bdd.py``). Scanning
#: only ``_BDD_ROOT`` would miss those registrations and falsely report the
#: feature as orphaned.
_TESTS_ROOT = REPO_ROOT / "backend" / "tests"


def _resolve_dir_arg(text: str, module: Path, name: str) -> Path | None:
    """Resolve a variable that a step module passes to ``scenarios()`` as a directory.

    Handles the two forms seen in the suite:

    - a plain string literal, e.g. ``_features_dir = "tests/bdd/features/events"``
    - a ``Path(__file__).resolve().parent[.parent...] / "a" / "b"`` expression, e.g.
      ``_features_dir = str(Path(__file__).resolve().parent.parent / "features" / "events")``

    Returns the resolved directory, or ``None`` when the assignment cannot be
    resolved (so the caller simply skips it rather than producing a false negative).
    """
    assign = re.search(rf"\b{name}\s*=\s*([^\n;]+)", text)
    if assign is None:
        return None
    rhs = assign.group(1).strip()
    quoted = re.findall(r'["\']([^"\']+)["\']', rhs)
    if not quoted:
        return None
    if "__file__" in rhs or "Path(" in rhs:
        parents = len(re.findall(r"\.parent", rhs))
        directory = module
        for _ in range(parents):
            directory = directory.parent
    else:
        directory = module.parent
    for segment in quoted:
        directory = directory / segment
    return directory.resolve()


def _registered_bdd_features() -> set[Path]:
    """Every ``.feature`` file wired to a ``scenarios(...)`` load call.

    pytest-bdd feature files only execute when a step module loads them via
    ``scenarios("...")`` (string-literal feature path) or via a directory
    registration such as ``scenarios(_features_dir)`` (where ``_features_dir``
    points at a features directory - e.g. ``test_sse_event_bus.py`` loads every
    ``.feature`` under ``backend/tests/bdd/features/events/`` this way).

    Both forms are detected: string-literal paths resolve relative to the module
    that declares them, and directory arguments register every ``.feature`` file
    found beneath the resolved directory. Every test module under ``backend/tests/``
    is scanned - not just ``tests/bdd/`` - because a step module may live under
    ``tests/integration/`` when its scenarios need the real-Postgres fixtures
    there. This keeps the coverage assertion in
    ``test_bdd_citations_are_registered_coverage`` free of false positives for
    directory-loaded features and for step modules hosted outside ``tests/bdd/``.
    """
    registered: set[Path] = set()
    if not _TESTS_ROOT.is_dir():
        return registered
    for module in _TESTS_ROOT.rglob("*.py"):
        try:
            text = module.read_text(encoding="utf-8")
        except OSError:
            continue
        for ref in re.findall(r"scenarios\(\s*['\"]([^'\"]+\.feature)['\"]", text):
            target = (module.parent / ref).resolve()
            if target.is_file():
                registered.add(target)
        for name in re.findall(r"scenarios\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", text):
            if name == "scenarios":
                continue
            directory = _resolve_dir_arg(text, module, name)
            if directory is None or not directory.is_dir():
                continue
            for feature in directory.rglob("*.feature"):
                registered.add(feature.resolve())
    return registered


def test_entry_file_references_resolve():
    """Every ``code:`` / ``unit-tests:`` / ``bdd:`` reference resolves.

    (``adr:`` is not checked — ADRs were migrated out of this repo (FAR-434)
    and are not repo-relative paths.)

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


def test_bdd_citations_are_registered_coverage():
    """Every ``bdd:`` feature-file citation is actually wired to a step module.

    ``test_entry_file_references_resolve`` only proves a cited ``.feature`` file
    exists on disk. A feature file that still ships but is no longer loaded by
    any ``scenarios(...)`` call contributes nothing to the test run, yet keeps
    the product map claiming BDD coverage for it — the silent drift direction.
    This test makes the coverage claim strong: every ``bdd:`` citation pointing
    under ``backend/tests/bdd/features/`` must resolve to a feature file that a
    step module registers, so product-map BDD claims always describe tests that
    actually execute (the stale-claim failure mode the 2026-08-26
    snapshot-versioning pass fixed).
    """
    registered = _registered_bdd_features()
    assert registered, "no BDD feature files are registered by any step module"

    unregistered: dict[str, list[str]] = {}
    for entry in _product_map_entry_paths():
        frontmatter = _entry_frontmatter(entry)
        for ref in frontmatter.get("bdd") or []:
            if not isinstance(ref, str) or not ref.endswith(".feature"):
                continue
            resolved = (REPO_ROOT / ref).resolve()
            if not resolved.is_relative_to(_BDD_ROOT.resolve()):
                continue
            if resolved not in registered:
                unregistered.setdefault(entry.relative_to(REPO_ROOT).as_posix(), []).append(ref)
    assert not unregistered, (
        "bdd: citations under backend/tests/bdd/ that no step module loads — the"
        " product map claims BDD coverage for feature files that never execute:\n"
        + "\n".join(f"  {entry} -> {refs}" for entry, refs in sorted(unregistered.items()))
    )


#: BDD ``.feature`` files under ``backend/tests/bdd/features/`` that no step
#: module loads via ``scenarios(...)`` — each ships but never executes, so its
#: scenarios contribute nothing to the test run. These are the acknowledged,
#: consciously-deferred orphans from the 2026-09-07 product-map walk (stale
#: placeholder drafts that predate any step definitions). They are tracked here
#: so the set can only SHRINK: the way to remove an entry is to either wire the
#: feature file up from a step module (rooting it in the real coverage) or
#: delete the file. New orphans fail ``test_no_unregistered_bdd_feature_files``.
#:
#: Cleared by the 2026-09-16 product-map walk: ``swappable_binding.feature`` is
#: wired from ``steps/test_pipeline_connector_binding.py``,
#: ``validation.feature`` from ``steps/test_pipeline_graph_validation.py``, and
#: ``pipeline_config_validation.feature`` (a redundant duplicate of
#: ``validation.feature``) was deleted. No orphaned feature files remain — the
#: debt list is empty.
_ORPHANED_BDD_FEATURES: frozenset[str] = frozenset()


def test_no_unregistered_bdd_feature_files():
    """Every ``.feature`` file under ``backend/tests/bdd/features/`` executes.

    ``test_bdd_citations_are_registered_coverage`` only catches the drift where
    a cited feature file stops being loaded. The *reverse* accumulation is a
    shipped ``.feature`` file that no step module ever loads: its scenarios
    silently decay while the file keeps the build green. Fail CLOSED on any
    orphan outside the tracked set — the tracked set must shrink to zero over
    time, never grow.
    """
    registered = _registered_bdd_features()
    features_dir = _BDD_ROOT / "features"
    assert features_dir.is_dir(), "backend/tests/bdd/features/ must exist"

    orphans = {path.resolve() for path in features_dir.rglob("*.feature") if path.resolve() not in registered}
    tracked = {(REPO_ROOT / rel).resolve() for rel in _ORPHANED_BDD_FEATURES}

    new_orphans = orphans - tracked
    assert not new_orphans, (
        "unregistered BDD feature files — no step module loads them via "
        "scenarios(...), so their scenarios never execute:\n"
        + "\n".join(f"  {p.relative_to(_BDD_ROOT)}" for p in sorted(new_orphans))
        + "\nWire each up from a step module (and drop it from "
        + "_ORPHANED_BDD_FEATURES) or delete it."
    )

    redundant = tracked - orphans
    assert not redundant, (
        "tracked orphan entries that are no longer orphaned (already wired up or "
        "deleted) — shrink _ORPHANED_BDD_FEATURES so the debt list stays honest:\n"
        + "\n".join(f"  {p.relative_to(_BDD_ROOT)}" for p in sorted(redundant))
    )


def _feature_scenarios_fully_deselected(feature: Path) -> bool:
    """Return True when every scenario in a feature file is ``@awaiting-implementation``.

    A fully-deselected file registers scenarios that pytest's ``-m 'not
    awaiting-implementation'`` addopt skips, so the file contributes nothing to a
    run no matter how many step modules call ``scenarios(...)`` for it. Files with
    no scenarios (or at least one executing scenario) are not "fully deselected".
    """
    tags: set[str] = set()
    total = 0
    executing = 0
    for raw in feature.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            tags = {t for t in line.split() if t.startswith("@")}
            continue
        if line.startswith(("Scenario Outline", "Scenario:")):
            total += 1
            if "@awaiting-implementation" not in tags:
                executing += 1
            tags = set()
    return total > 0 and executing == 0


def test_feature_scenarios_fully_deselected_resets_tags_per_scenario(tmp_path: Path):
    """A per-scenario tag must not bleed into the following scenario.

    Here the first (pinned) scenario is deselected but the second, untagged
    scenario executes, so the file is not "fully deselected". A parser that
    accumulated tags across scenarios would misclassify it as fully deselected
    and wrongly let a ``bdd:`` citation to it pass the guard.
    """
    feature = tmp_path / "mixed.feature"
    feature.write_text(
        "@awaiting-implementation\n"
        "Scenario: pinned draft\n"
        "  Given a step\n"
        "\n"
        "Scenario: executing coverage\n"
        "  Given a step\n",
        encoding="utf-8",
    )
    assert _feature_scenarios_fully_deselected(feature) is False


def test_feature_scenarios_fully_deselected_when_every_scenario_pinned(tmp_path: Path):
    feature = tmp_path / "all_pinned.feature"
    feature.write_text(
        "@awaiting-implementation\n"
        "Scenario: first draft\n"
        "  Given a step\n"
        "\n"
        "@awaiting-implementation\n"
        "Scenario: second draft\n"
        "  Given a step\n",
        encoding="utf-8",
    )
    assert _feature_scenarios_fully_deselected(feature) is True


#: Heading that opens a behaviour-tracker's currently-acknowledged gap list.
_KNOWN_GAPS_HEADING = "## Known Gaps"
_KNOWN_GAPS_CLAIM_START = re.compile(r"^- ", re.MULTILINE)

#: Claim phrases in a Known Gaps bullet that assert a ``.feature`` file does not
#: execute. When such a bullet names a feature file that a step module actually
#: registers via ``scenarios(...)``, the entry has drifted stale — the exact
#: failure the 2026-09-08 feat-runs "dead BDD files" gap suffered: it was
#: recorded the day before ``steps/test_pipelines.py`` wired both files up.
#: Scanning is scoped to the Known Gaps section (not behaviour/QA prose, where a
#: file is named as positive coverage or as a historical record) and evaluated
#: per bullet, so a negation in one gap bullet cannot implicate a positively-cited
#: file in the next.
_DEAD_BDD_CLAIM = re.compile(
    r"(?:never\s+executes?|"
    r"no\s+step\s+module\s+registers|"
    r"does\s+not\s+register|"
    r"not\s+registered\s+via|"
    r"dead\s+BDD\s+files?|"
    r"unregistered\s+BDD)",
    re.IGNORECASE,
)


def _known_gap_bullets(entry: Path) -> list[str]:
    """Split an entry's ``## Known Gaps`` section into its bullets.

    Returns the text of each bullet (continuation lines included) or ``[]`` when
    the entry has no Known Gaps section.
    """
    text = entry.read_text(encoding="utf-8")
    if _KNOWN_GAPS_HEADING not in text:
        return []
    section = text.split(_KNOWN_GAPS_HEADING, 1)[1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
    starts = [match.start() for match in _KNOWN_GAPS_CLAIM_START.finditer(section)]
    if not starts:
        return []
    bullets = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(section)
        bullets.append(section[start:end])
    return bullets


def test_no_stale_dead_bdd_claims():
    """No Known Gaps bullet claims a registered BDD feature file never executes.

    ``test_no_unregistered_bdd_feature_files`` tracks orphaned ``.feature`` files
    that genuinely never run. The opposite drift is a behaviour-tracker entry whose
    Known Gaps prose asserts a feature file is dead ("never executes", "no step
    module registers it", "dead BDD") after a step module has wired that same file
    up via ``scenarios(...)`` — a stale claim that survives
    ``test_bdd_citations_are_registered_coverage`` because the file was never a
    ``bdd:`` citation. The 2026-09-08 feat-runs gap
    ("``run_lifecycle.feature`` / ``run_sequential.feature`` are dead BDD files")
    is that failure: both files shipped registered in ``steps/test_pipelines.py``
    the next day while the tracker kept claiming they never executed. Fail closed
    on any such mismatch so a walk that wires a file up also updates the tracker
    that documented the gap.
    """
    registered = _registered_bdd_features()
    assert registered, "no BDD feature files are registered by any step module"
    registered_rel = {path.relative_to(REPO_ROOT).as_posix() for path in registered}

    stale: dict[str, list[str]] = {}
    for entry in _product_map_entry_paths():
        for bullet in _known_gap_bullets(entry):
            if not _DEAD_BDD_CLAIM.search(bullet):
                continue
            for name_match in re.finditer(r"([A-Za-z0-9_./-]+\.feature)", bullet):
                name = name_match.group(1)
                registered_hit = next(
                    (rel for rel in registered_rel if rel.endswith((f"/{name}", name))),
                    None,
                )
                if registered_hit is None:
                    continue
                stale.setdefault(entry.relative_to(REPO_ROOT).as_posix(), []).append(
                    f"{name!r} (registered: {registered_hit})"
                )
    assert not stale, (
        "behaviour-tracker prose claims these .feature files never execute / are "
        "not registered, but a step module registers them via scenarios(...) — "
        "stale 'dead BDD' claims: update the entry to cite them as executing "
        "coverage and drop the gap:\n"
        + "\n".join(f"  {entry} -> {', '.join(files)}" for entry, files in sorted(stale.items()))
    )


def test_no_bdd_citations_for_fully_deselected_features():
    """No ``bdd:`` citation points at a feature file whose every scenario is deselected.

    ``test_bdd_citations_are_registered_coverage`` proves a cited ``.feature``
    file is *loaded* by a step module, but a file whose scenarios are all
    ``@awaiting-implementation`` is deselected at collection time (pyproject
    addopt ``-m 'not awaiting-implementation'``) and therefore never executes --
    the product map claims BDD coverage for behaviour CI does not run. This was
    exactly the stale ``ui/eval_dashboard.feature`` citation: registered via
    ``steps/test_ui.py`` but with all four scenarios pinned since 2026-08, so
    ``feat-evals`` advertised executing BDD coverage that never executed until the
    drafts were archived (2026-09-28 Improve Architecture walk). A ``bdd:``
    citation must name at least one executing scenario; fully-deselected drafts
    belong nowhere in the graph and must be archived/re-anchored instead.
    """
    deselected: dict[str, list[str]] = {}
    for entry in _product_map_entry_paths():
        frontmatter = _entry_frontmatter(entry)
        for ref in frontmatter.get("bdd") or []:
            if not isinstance(ref, str) or not ref.endswith(".feature"):
                continue
            resolved = (REPO_ROOT / ref).resolve()
            if not resolved.is_relative_to(_BDD_ROOT.resolve()) or not resolved.is_file():
                continue
            if _feature_scenarios_fully_deselected(resolved):
                deselected.setdefault(entry.relative_to(REPO_ROOT).as_posix(), []).append(ref)
    assert not deselected, (
        "bdd: citations that name a fully @awaiting-implementation-deselected "
        ".feature file — every scenario is skipped by '-m not awaiting-implementation', "
        "so the product map claims BDD coverage that never executes. Archive or "
        "re-anchor the draft (and drop the bdd: citation, or un-deselect at least "
        "one scenario) so the claim describes tests CI actually runs:\n"
        + "\n".join(f"  {entry} -> {refs}" for entry, refs in sorted(deselected.items()))
    )


def test_feature_references_resolve():
    """Every ``feat-*`` literal in shipped code/tests resolves against the product map.

    Registry = the manifest ``features:`` registry merged with the
    ``docs/product-map/`` entry ids. A literal that resolves nowhere is a feature gap:
    the feature shipped (or its test documents a shipped behaviour) but no product-map
    surface references it, so it is invisible to Assistant's ``search_documentation`` indexer
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

    Docs are consumed by Assistant's ``search_documentation`` indexer and the feature
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
