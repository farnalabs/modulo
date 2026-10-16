"""Shared parser + invariant helpers for the product-map sidebar nav (FAR-52).

The sidebar is defined by two files — ``frontend/src/manifest.yaml`` and
``frontend/src/config/navigation.ts`` — and the same invariants over them are
enforced in two places: the pre-commit / CI gate
``scripts/run_validate_nav_config.py`` and the architecture suite
``backend/tests/architecture/test_product_map_consistency.py``. Keeping two
independent parsers of the same files is a silent-failure hazard: the two
drift, and the looser reader keeps returning an empty problem set while the
stricter one still passes, so the offending PR merges green.

This module is the single implementation both consumers import. Its functions
are pure — they take already-read text or already-parsed mappings and return
findings; each consumer owns its own message formatting and exit behaviour.

Findings are returned as structured tuples (not pre-formatted strings) so the
gate can emit pytest-style diagnostics while the test keeps its
``path -> detail`` assertions without either dictating the other's wording.
"""

from __future__ import annotations

import re

#: Route-id keys inside navigation.ts's ``routeConfigMap`` — two-space-indented
#: ``key: { icon: '...', labelKey: '...' }`` entries. Comment lines start with
#: ``//`` and never match, and the block is cut at ``groupLabelKeyMap`` so later
#: entries are excluded.
_ROUTE_CONFIG_RE = re.compile(
    r"^\s{2}['\"]?([A-Za-z][\w-]*)['\"]?:\s*\{\s*icon: '([^']+)'\s*,\s*labelKey: '([^']+)'",
    re.MULTILINE,
)


def route_config_entries(nav_text: str) -> dict[str, tuple[str, str]]:
    """Map each ``routeConfigMap`` key in navigation.ts to ``(icon, labelKey)``."""
    if "const routeConfigMap" not in nav_text:
        return {}
    block = nav_text.split("const routeConfigMap", 1)[1]
    if "const groupLabelKeyMap" in block:
        block = block.split("const groupLabelKeyMap", 1)[0]
    return {key: (icon, label_key) for key, icon, label_key in _ROUTE_CONFIG_RE.findall(block)}


def route_config_keys(nav_text: str) -> set[str]:
    """The route-id keys declared in navigation.ts's ``routeConfigMap``."""
    return set(route_config_entries(nav_text))


def is_sidebar_nav_entry(entry: object) -> bool:
    """True for routes ``buildSidebarGroups()`` renders into the sidebar."""
    return isinstance(entry, dict) and bool(entry.get("sidebar_group")) and entry.get("type") != "detail_page"


def manifest_route_names(routes: dict) -> set[str]:
    """Every manifest route's declared ``name`` (its route id)."""
    return {entry.get("name") for entry in routes.values() if isinstance(entry, dict) and entry.get("name")}


def sidebar_group_references(routes: dict, groups: dict) -> list[tuple[str, str]]:
    """``(path, group)`` for routes whose declared sidebar_group is undeclared."""
    return [
        (path, entry["sidebar_group"])
        for path, entry in routes.items()
        if isinstance(entry, dict) and entry.get("sidebar_group") and entry["sidebar_group"] not in groups
    ]


def sidebar_order_conflicts(routes: dict) -> list[tuple[str, int, str, str]]:
    """``(group, order, first_path, second_path)`` for duplicate rendered orders.

    Only entries ``buildSidebarGroups()`` renders participate (``sidebar_group``
    set, ``type != detail_page``), and only integer orders — a non-integer order
    is a separate violation (navigation.ts drops the entry entirely).
    """
    conflicts: list[tuple[str, int, str, str]] = []
    seen: dict[str, dict[int, str]] = {}
    for path, entry in routes.items():
        if not is_sidebar_nav_entry(entry):
            continue
        order = entry.get("sidebar_order")
        if type(order) is not int:
            continue
        group = entry.get("sidebar_group")
        group_orders = seen.setdefault(group, {})
        if order in group_orders:
            conflicts.append((group, order, group_orders[order], path))
        else:
            group_orders[order] = path
    return conflicts


def nav_routes_missing_config(routes: dict, config_keys: set[str]) -> list[tuple[str, str]]:
    """``(path, name)`` for rendered nav routes with no routeConfigMap entry.

    A missing entry makes the sidebar fall back to a generic icon and an
    unresolved ``nav.<name>`` label key.
    """
    return [
        (path, entry["name"])
        for path, entry in routes.items()
        if is_sidebar_nav_entry(entry)
        and isinstance(entry.get("name"), str)
        and entry["name"]
        and entry["name"] not in config_keys
    ]


def dead_config_keys(routes: dict, config_keys: set[str]) -> list[str]:
    """routeConfigMap keys with no corresponding manifest route (dead entries)."""
    return sorted(config_keys - manifest_route_names(routes))
