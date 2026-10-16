#!/usr/bin/env python3
"""Pre-commit / CI gate for the sidebar nav structure in the product map (FAR-52).

Cross-checks ``frontend/src/manifest.yaml`` against
``frontend/src/config/navigation.ts`` — the two files that together define the
sidebar. Two validator groups:

**1. Group id / ``sidebar_order`` structure — uniqueness, presence,
referential sanity:**

- no duplicate mapping keys under ``routes`` / ``sidebar_groups`` (a duplicate
  YAML key silently overwrites, so a duplicated group id or route path would
  vanish from the parsed manifest without a trace);
- every route that declares a ``sidebar_group`` points at a group id declared
  in ``sidebar_groups``;
- every nav entry — ``sidebar_group`` set and ``type != detail_page``, exactly
  the entries ``buildSidebarGroups()`` renders — carries an integer
  ``sidebar_order`` (navigation.ts drops entries whose order is not a number,
  silently removing them from the sidebar);
- ``sidebar_order`` is unique within a group, so the sidebar sort is fully
  deterministic;
- every declared group carries ``label`` / ``order`` / ``default_expanded``,
  the fields navigation.ts reads unconditionally, with the right types.

**2. Route-id parity with navigation.ts:**

- every nav entry's route ``name`` has an entry in ``routeConfigMap`` —
  otherwise the sidebar falls back to a generic icon and an undefined
  ``nav.<name>`` label key;
- every ``routeConfigMap`` entry names a real manifest route — no dead icon
  entries.

Label-key/locale parity lives in ``run_validate_translations.py``; this gate
does not duplicate it. The navigation.ts / manifest parsers and the invariants
shared with ``backend/tests/architecture/test_product_map_consistency.py`` live
in ``scripts/nav_config_shared.py``, so the gate and the architecture suite can
never drift into enforcing different rules.

Exit 0 clean, 1 violations found.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


def _load_shared_module() -> ModuleType:
    """Load the sibling ``nav_config_shared`` module without touching ``sys.path``."""
    path = Path(__file__).resolve().with_name("nav_config_shared.py")
    spec = importlib.util.spec_from_file_location("nav_config_shared", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import-machinery contract
        raise ImportError(f"cannot load the shared nav helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_nav = _load_shared_module()

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_MANIFEST_PATH = str(Path("frontend") / "src" / "manifest.yaml")
_NAV_PATH = str(Path("frontend") / "src" / "config" / "navigation.ts")

#: Mapping keys whose duplicate entries are reported (nav-relevant scopes).
_DUP_SCOPES = frozenset({"routes", "sidebar_groups"})


def _read(path: str) -> str:
    try:
        with Path(path).open(encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _child_path(key_node: object, path: tuple[str, ...]) -> tuple[str, ...]:
    """Extend ``path`` with a scalar mapping key (non-scalar keys leave it)."""
    if isinstance(key_node, ScalarNode):
        return path + (key_node.value,)
    return path


def _duplicate_keys_under_scope(node: MappingNode, path: tuple[str, ...]) -> list[str]:
    """Duplicate scalar keys directly under one nav-relevant mapping scope."""
    counts: Counter[str] = Counter(key.value for key, _value in node.value if isinstance(key, ScalarNode))
    return [
        f"{_MANIFEST_PATH}: duplicate mapping key {key!r} under {'/'.join(path)} "
        f"({count} occurrences — YAML keeps only the last)"
        for key, count in sorted(counts.items())
        if count > 1
    ]


def _walk_duplicate_keys(node: object, path: tuple[str, ...], problems: list[str]) -> None:
    """Recursively collect duplicate-key problems from a composed YAML tree."""
    if isinstance(node, MappingNode):
        if path and path[0] in _DUP_SCOPES:
            problems.extend(_duplicate_keys_under_scope(node, path))
        for key_node, value_node in node.value:
            _walk_duplicate_keys(value_node, _child_path(key_node, path), problems)
    elif isinstance(node, SequenceNode):
        for item in node.value:
            _walk_duplicate_keys(item, path, problems)


def _duplicate_key_problems(root: object) -> list[str]:
    """Report literal duplicate mapping keys under the nav-relevant scopes.

    ``root`` is the composed document root (``yaml.compose`` returns the root
    node directly, not a wrapper — ``None`` means an empty document).
    """
    problems: list[str] = []
    if root is not None:
        _walk_duplicate_keys(root, (), problems)
    return problems


def _referential_problems(routes: dict, groups: dict) -> list[str]:
    """Every declared route sidebar_group resolves to a declared group id."""
    return [
        f"route {path!r} references undeclared sidebar_group {group!r}"
        for path, group in _nav.sidebar_group_references(routes, groups)
    ]


def _sidebar_order_problems(routes: dict) -> list[str]:
    """Presence + uniqueness of sidebar_order on the entries the sidebar renders."""
    problems: list[str] = []
    for path, entry in routes.items():
        if not _nav.is_sidebar_nav_entry(entry):
            continue
        order = entry.get("sidebar_order")
        if type(order) is not int:
            problems.append(
                f"nav entry {path!r} must carry an integer sidebar_order "
                f"(got {order!r} — navigation.ts drops entries whose order is not a number)"
            )
    problems.extend(
        f"duplicate sidebar_order {order} in group {group!r}: {first!r} and {second!r}"
        for group, order, first, second in _nav.sidebar_order_conflicts(routes)
    )
    return problems


def _group_field_problems(groups: dict) -> list[str]:
    """Each declared group carries the fields navigation.ts reads."""
    problems: list[str] = []
    for group_id, group in groups.items():
        if not isinstance(group, dict):
            problems.append(f"sidebar_group {group_id!r} must be a mapping")
            continue
        label = group.get("label")
        if not isinstance(label, str) or not label:
            problems.append(f"sidebar_group {group_id!r} must carry a non-empty 'label'")
        if type(group.get("order")) is not int:
            problems.append(f"sidebar_group {group_id!r} must carry an integer 'order'")
        if type(group.get("default_expanded")) is not bool:
            problems.append(f"sidebar_group {group_id!r} must carry a boolean 'default_expanded'")
    return problems


def _structure_problems(manifest: object) -> list[str]:
    """Deliverable 1: group-id / sidebar_order uniqueness, presence, sanity."""
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return [f"{_MANIFEST_PATH}: manifest root must be a mapping"]

    routes = manifest.get("routes")
    groups = manifest.get("sidebar_groups")
    if not isinstance(routes, dict):
        problems.append(f"{_MANIFEST_PATH}: missing or non-mapping 'routes'")
        routes = {}
    if not isinstance(groups, dict):
        problems.append(f"{_MANIFEST_PATH}: missing or non-mapping 'sidebar_groups'")
        groups = {}

    problems.extend(_referential_problems(routes, groups))
    problems.extend(_sidebar_order_problems(routes))
    problems.extend(_group_field_problems(groups))
    return problems


def _route_id_problems(manifest: object, nav_text: str) -> list[str]:
    """Deliverable 3: manifest route ids <-> navigation.ts routeConfigMap parity."""
    problems: list[str] = []
    routes = manifest.get("routes") if isinstance(manifest, dict) else None
    if not isinstance(routes, dict):
        return problems  # the structure validator already reported this

    config_keys = _nav.route_config_keys(nav_text)

    problems.extend(
        f"nav entry {path!r} has no route 'name' (route id) to match routeConfigMap"
        for path, entry in routes.items()
        if _nav.is_sidebar_nav_entry(entry) and not (isinstance(entry.get("name"), str) and entry["name"])
    )
    problems.extend(
        f"nav route {path!r} (name {name!r}) has no routeConfigMap entry in navigation.ts "
        "(the sidebar falls back to a generic icon and an unresolved 'nav.<name>' label)"
        for path, name in _nav.nav_routes_missing_config(routes, config_keys)
    )
    problems.extend(
        f"routeConfigMap entry {key!r} has no corresponding manifest route (dead icon entry)"
        for key in _nav.dead_config_keys(routes, config_keys)
    )
    return problems


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate manifest nav structure and route-id parity.")
    parser.parse_args(argv)

    manifest_text = _read(str(Path(REPO_ROOT) / _MANIFEST_PATH))
    if not manifest_text:
        print(f"FAIL: {_MANIFEST_PATH} not found — cannot validate the sidebar nav", file=sys.stderr)
        return 1
    nav_text = _read(str(Path(REPO_ROOT) / _NAV_PATH))
    if not nav_text:
        print(f"FAIL: {_NAV_PATH} not found — cannot validate route-id parity", file=sys.stderr)
        return 1

    # One parse, one error path: the duplicate-key check needs the composed node
    # tree and the structural checks need the loaded mapping, so both parses live
    # under this guard. Malformed YAML must surface the controlled "invalid YAML"
    # diagnostic, never a raw ParserError traceback.
    problems: list[str] = []
    manifest: object = None
    try:
        root = yaml.compose(manifest_text)
        manifest = yaml.safe_load(manifest_text)
    except yaml.YAMLError as exc:
        problems.append(f"{_MANIFEST_PATH}: invalid YAML — {exc}")
    else:
        # An empty / whitespace-only document loads as None; a scalar or list
        # loads as a non-mapping. Both must fail hard rather than fall open —
        # the missing-file path above already does.
        problems.extend(_duplicate_key_problems(root))
        problems.extend(_structure_problems(manifest))
        problems.extend(_route_id_problems(manifest, nav_text))

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        print(
            f"\nrun_validate_nav_config: FAILED - {len(problems)} problem(s) in {_MANIFEST_PATH} / {_NAV_PATH}",
            file=sys.stderr,
        )
        return 1

    routes = manifest.get("routes", {}) if isinstance(manifest, dict) else {}
    groups = manifest.get("sidebar_groups", {}) if isinstance(manifest, dict) else {}
    nav_count = sum(1 for entry in routes.values() if _nav.is_sidebar_nav_entry(entry))
    print(
        f"run_validate_nav_config: OK - {nav_count} nav entries across "
        f"{len(groups)} sidebar groups, {len(_nav.route_config_keys(nav_text))} routeConfigMap entries",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
