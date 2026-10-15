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
does not duplicate it.

Exit 0 clean, 1 violations found.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_MANIFEST_PATH = str(Path("frontend") / "src" / "manifest.yaml")
_NAV_PATH = str(Path("frontend") / "src" / "config" / "navigation.ts")

#: Mapping keys whose duplicate entries are reported (nav-relevant scopes).
_DUP_SCOPES = frozenset({"routes", "sidebar_groups"})

#: Route-id keys inside ``routeConfigMap`` — two-space-indented ``key: {`` or
#: ``'key': {`` entries. Comment lines start with ``//`` and never match, and
#: the block is cut at ``groupLabelKeyMap`` so later entries are excluded.
_ROUTE_CONFIG_RE = re.compile(r"^\s{2}['\"]?([A-Za-z][\w-]*)['\"]?:\s*\{", re.MULTILINE)


def _read(path: str) -> str:
    try:
        with Path(path).open(encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _duplicate_key_problems(manifest_text: str) -> list[str]:
    """Report literal duplicate mapping keys under the nav-relevant scopes."""
    problems: list[str] = []

    def walk(node: object, path: tuple[str, ...]) -> None:
        if isinstance(node, MappingNode):
            counts: Counter[str] = Counter(key.value for key, _value in node.value if isinstance(key, ScalarNode))
            if path and path[0] in _DUP_SCOPES:
                for key, count in sorted(counts.items()):
                    if count > 1:
                        problems.append(
                            f"{_MANIFEST_PATH}: duplicate mapping key {key!r} under {'/'.join(path)} "
                            f"({count} occurrences — YAML keeps only the last)"
                        )
            for key_node, value_node in node.value:
                child = path + (key_node.value,) if isinstance(key_node, ScalarNode) else path
                walk(value_node, child)
        elif isinstance(node, SequenceNode):
            for item in node.value:
                walk(item, path)

    # yaml.compose returns the document's ROOT node directly (not a wrapper),
    # so walk it as-is; a None result means an empty document.
    root = yaml.compose(manifest_text)
    if root is not None:
        walk(root, ())
    return problems


def _route_config_keys(nav_text: str) -> set[str]:
    """Extract the route-id keys declared in navigation.ts's routeConfigMap."""
    if "const routeConfigMap" not in nav_text:
        return set()
    block = nav_text.split("const routeConfigMap", 1)[1]
    if "const groupLabelKeyMap" in block:
        block = block.split("const groupLabelKeyMap", 1)[0]
    return set(_ROUTE_CONFIG_RE.findall(block))


def _is_nav_entry(entry: object) -> bool:
    """True for routes ``buildSidebarGroups()`` renders into the sidebar."""
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("sidebar_group")) and entry.get("type") != "detail_page"


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

    # Referential sanity: every declared sidebar_group resolves to a group id.
    for path, entry in routes.items():
        if not isinstance(entry, dict):
            continue
        group = entry.get("sidebar_group")
        if group and group not in groups:
            problems.append(f"route {path!r} references undeclared sidebar_group {group!r}")

    # Presence + uniqueness on the entries the sidebar actually renders.
    seen_orders: dict[object, dict[object, str]] = {}
    for path, entry in routes.items():
        if not isinstance(entry, dict) or not _is_nav_entry(entry):
            continue
        order = entry.get("sidebar_order")
        if type(order) is not int:
            problems.append(
                f"nav entry {path!r} must carry an integer sidebar_order "
                f"(got {order!r} — navigation.ts drops entries whose order is not a number)"
            )
            continue
        group_orders = seen_orders.setdefault(entry.get("sidebar_group"), {})
        if order in group_orders:
            problems.append(
                f"duplicate sidebar_order {order} in group {entry.get('sidebar_group')!r}: "
                f"{group_orders[order]!r} and {path!r}"
            )
        else:
            group_orders[order] = path

    # Presence: each declared group carries the fields navigation.ts reads.
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


def _route_id_problems(manifest: object, nav_text: str) -> list[str]:
    """Deliverable 3: manifest route ids <-> navigation.ts routeConfigMap parity."""
    problems: list[str] = []
    routes = manifest.get("routes") if isinstance(manifest, dict) else None
    if not isinstance(routes, dict):
        return problems  # the structure validator already reported this

    config_keys = _route_config_keys(nav_text)
    manifest_names = {entry.get("name") for entry in routes.values() if isinstance(entry, dict) and entry.get("name")}

    for path, entry in routes.items():
        if not isinstance(entry, dict) or not _is_nav_entry(entry):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"nav entry {path!r} has no route 'name' (route id) to match routeConfigMap")
        elif name not in config_keys:
            problems.append(
                f"nav route {path!r} (name {name!r}) has no routeConfigMap entry in navigation.ts "
                "(the sidebar falls back to a generic icon and an unresolved 'nav.<name>' label)"
            )

    problems.extend(
        f"routeConfigMap entry {key!r} has no corresponding manifest route (dead icon entry)"
        for key in sorted(config_keys - manifest_names)
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

    problems = _duplicate_key_problems(manifest_text)
    try:
        manifest = yaml.safe_load(manifest_text)
    except yaml.YAMLError as exc:
        problems.append(f"{_MANIFEST_PATH}: invalid YAML — {exc}")
        manifest = None
    if manifest is not None:
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
    nav_count = sum(1 for entry in routes.values() if _is_nav_entry(entry))
    print(
        f"run_validate_nav_config: OK - {nav_count} nav entries across "
        f"{len(groups)} sidebar groups, {len(_route_config_keys(nav_text))} routeConfigMap entries",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
