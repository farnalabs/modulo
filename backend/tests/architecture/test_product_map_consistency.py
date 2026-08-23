"""Architecture test: the product map stays consistent with shipped routes.

``frontend/src/manifest.yaml`` is the single source of truth for the product
surface — every page, sidebar group and sidebar order, plus the breadcrumb /
parent / permission metadata that the frontend router, the ``/api/v1/manifest``
endpoint and Remy's documentation indexer all read from it. Drift between the
map and the real frontend router silently does two opposite things: a page
that exists (e.g. onboarding, system-admin pages) is invisible to the product
map, and a page the map advertises can be a dead redirect that no longer
renders.

This suite pins the invariants a healthy product map must keep:

- every route entry carries the core fields its consumers rely on
  (``name`` / ``breadcrumb`` / ``type`` / ``deprecated``);
- ``sidebar_group`` references resolve to a declared group;
- ``sidebar_order`` is unique among the visible (non-``detail_page``) items
  of a group, so the sidebar sort order is fully deterministic;
- every mapped route resolves to a rendered router page (not a redirect-only
  alias) and every non-auth router page has a map entry;
- every ``elements`` record carries ``testid`` / ``type``, is unique within
  its route, and its ``testid`` resolves to a real ``data-testid`` in the
  shipped frontend (ADR-008: no orphaned elements, every static testid exists
  in a template).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "manifest.yaml"
ROUTER_PATH = REPO_ROOT / "frontend" / "src" / "router" / "index.ts"

# Auth / public / dev / error plumbing that is intentionally not part of the
# product map (no breadcrumb or sidebar surface a user navigates to).
NON_PRODUCT_ROUTES = frozenset({"login", "auth-callback", "not-found", "dev-metrics"})

#: The frontend router file defines every route at a 6-space-indented ``path:``
#: key; everything after the routes array (``scrollBehavior``, guards) is
#: non-route and must not be parsed. A route is a page when its block carries a
#: ``component:`` and a pure alias when it only carries a ``redirect:``.
_PATH_INDENT = r"^      path: "
_ROUTE_BLOCK_START = re.compile(_PATH_INDENT + r"'[^']*'", re.MULTILINE)
_ROUTE_NAME = re.compile(r"^\s{6}name: '([^']+)'", re.MULTILINE)
_HAS_COMPONENT = re.compile(r"^\s{6}component:", re.MULTILINE)
_IS_REDIRECT = re.compile(r"^\s{6}redirect:", re.MULTILINE)

#: Static ``data-testid`` literals contributing to each element inventory are
#: qualified with the exact attribute form they must appear as in the sources.
_TESTID_LITERAL = re.compile(r"data-testid=\"([a-zA-Z0-9_-]+)\"")


def _routes_text() -> str:
    text = ROUTER_PATH.read_text(encoding="utf-8")
    return text.split("scrollBehavior", 1)[0]


def _named_routes() -> dict[str, dict[str, bool]]:
    """Map each router route name to whether it renders and/or redirects."""
    text = _routes_text()
    starts = list(_ROUTE_BLOCK_START.finditer(text))
    named: dict[str, dict[str, bool]] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.start() : end]
        name_match = _ROUTE_NAME.search(block)
        if name_match is None:
            continue
        named[name_match.group(1)] = {
            "renders": _HAS_COMPONENT.search(block) is not None,
            "redirects": _IS_REDIRECT.search(block) is not None,
        }
    return named


def _load_manifest() -> dict:
    with MANIFEST_PATH.open() as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise AssertionError("manifest.yaml root must be a mapping")
    if not isinstance(data.get("routes"), dict):
        raise AssertionError("manifest.yaml must declare a 'routes' mapping")
    return data


def test_manifest_route_entries_have_core_fields():
    routes = _load_manifest()["routes"]
    incomplete: dict[str, list[str]] = {}
    for path, entry in routes.items():
        if not isinstance(entry, dict):
            incomplete[path] = ["<entry is not a mapping>"]
            continue
        missing = sorted({"name", "breadcrumb", "type", "deprecated"} - set(entry))
        if missing:
            incomplete[path] = missing
    assert not incomplete, (
        "manifest routes missing core fields consumed by the router / sidebar / docs indexer:\n"
        + "\n".join(f"  {path} -> {fields}" for path, fields in sorted(incomplete.items()))
    )


def test_sidebar_group_references_resolve():
    data = _load_manifest()
    groups = set(data.get("sidebar_groups", {}))
    routes = data["routes"]
    dangling = {
        path: entry["sidebar_group"]
        for path, entry in routes.items()
        if isinstance(entry, dict) and entry.get("sidebar_group") and entry["sidebar_group"] not in groups
    }
    assert not dangling, "manifest routes reference undeclared sidebar_groups:\n" + "\n".join(
        f"  {path} -> {group}" for path, group in sorted(dangling.items())
    )


def test_sidebar_order_unique_within_group():
    routes = _load_manifest()["routes"]
    seen: dict[str, dict[int, str]] = {}
    conflicts: list[str] = []
    for path, entry in routes.items():
        if not isinstance(entry, dict):
            continue
        group = entry.get("sidebar_group")
        order = entry.get("sidebar_order")
        if not group or order is None or entry.get("type") == "detail_page":
            continue
        group_orders = seen.setdefault(group, {})
        if order in group_orders:
            conflicts.append(f"{group} order {order}: {group_orders[order]} and {path}")
        else:
            group_orders[order] = path
    assert not conflicts, "duplicate sidebar_order within a group leaves the sort order ambiguous:\n" + "\n".join(
        conflicts
    )


def test_mapped_routes_exist_in_router():
    routes = _load_manifest()["routes"]
    router_names = set(_named_routes())
    missing = {
        path: entry["name"]
        for path, entry in routes.items()
        if isinstance(entry, dict) and entry.get("name") and entry["name"] not in router_names
    }
    assert not missing, "manifest routes with no corresponding frontend router route:\n" + "\n".join(
        f"  {path} -> {name}" for path, name in sorted(missing.items())
    )


def test_mapped_routes_render_a_page_not_a_redirect():
    named = _named_routes()
    routes = _load_manifest()["routes"]
    redirect_only = {
        path: entry["name"]
        for path, entry in routes.items()
        if isinstance(entry, dict)
        and entry.get("name")
        and entry["name"] in named
        and not named[entry["name"]]["renders"]
    }
    assert not redirect_only, (
        "manifest advertises pages the router only redirects away from; drop the map entry or ship the page:\n"
        + "\n".join(f"  {path} -> {name}" for path, name in sorted(redirect_only.items()))
    )


def _load_elements() -> dict[str, list[dict]]:
    data = _load_manifest()
    elements = data.get("elements")
    assert isinstance(elements, dict), "manifest.yaml must declare an 'elements' mapping"
    return elements


def _static_testids_in_frontend() -> frozenset[str]:
    """Every static ``data-testid`` literal shipped anywhere in the frontend."""
    src = set()
    for path in (REPO_ROOT / "frontend" / "src").rglob("*"):
        if path.suffix not in {".vue", ".ts", ".js"}:
            continue
        try:
            src.update(_TESTID_LITERAL.findall(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return frozenset(src)


def test_element_entries_have_core_fields():
    elements = _load_elements()
    for path, items in elements.items():
        assert path in _load_manifest()["routes"], f"elements map an unregistered route: {path}"
        assert isinstance(items, list), f"elements for {path} must be a list"
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise AssertionError(f"elements[{path}][{index}] is not a mapping")
            missing = sorted({"testid", "type"} - set(item))
            assert not missing, f"elements[{path}][{index}] missing required fields: {missing}"


def test_element_testids_unique_within_route():
    elements = _load_elements()
    for path, items in elements.items():
        seen: dict[str, int] = {}
        for index, item in enumerate(items):
            testid = item.get("testid")
            if testid is None:
                continue
            if testid in seen:
                raise AssertionError(
                    f"duplicate element testid {testid!r} in {path} (entries {seen[testid]} and {index})"
                )
            seen[testid] = index


def test_element_testids_exist_in_frontend():
    elements = _load_elements()
    live = _static_testids_in_frontend()
    dangling = {
        testid: path for path, items in elements.items() for item in items if (testid := item.get("testid")) not in live
    }
    assert not dangling, "elements reference data-testids that do not exist in the frontend:\n" + "\n".join(
        f"  {path} -> {testid}" for testid, path in sorted(dangling.items())
    )


def test_every_non_auth_router_route_is_mapped():
    named = _named_routes()
    manifest_names = {
        entry["name"] for entry in _load_manifest()["routes"].values() if isinstance(entry, dict) and entry.get("name")
    }
    uncovered = sorted(
        name
        for name, info in named.items()
        if name not in manifest_names and name not in NON_PRODUCT_ROUTES and info["renders"]
    )
    assert not uncovered, (
        "frontend router pages with no product-map entry:\n"
        + "\n".join(f"  {name}" for name in uncovered)
        + "\nAdd each page to frontend/src/manifest.yaml or to NON_PRODUCT_ROUTES if it is auth/public/dev-only."
    )
