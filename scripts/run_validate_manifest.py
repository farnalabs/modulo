#!/usr/bin/env python3
"""Cross-platform validator for frontend/src/manifest.yaml.

Replaces `tools/validate-manifest.ps1`. Every route name matches the Vue
Router config, every static testid exists in a template, every product_map
ref resolves, every i18n_key exists in en-US.js, no orphaned elements, no
circular parent chains, and dynamic routes are fully specified.

Exit code: 0 = clean, 1 = issues found.

Flags (argparse):
  --ci         Print raw issue lines with no colors, exit 1 on the first error
               (matching the .ps1's Write-Err behavior).
  --repo-root  Override the repo root (for fixture-based testing).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# PowerShell's Select-String is case-insensitive by default, so the original
# `[a-z]` also matched uppercase router names (e.g. 'ModelBackendSetup'). Use
# re.IGNORECASE to reproduce that behaviour exactly.
_ROUTER_RE = re.compile(r"name:\s*'([a-z][a-z0-9-]*)'", re.IGNORECASE)
_I18N_RE = re.compile(r'^\s{2}"(\w+)":\s*\{', re.MULTILINE)


def _write_err(msg: str, errors: list[str], args: argparse.Namespace) -> None:
    """Record an issue and, in --ci mode, print raw + exit 1 immediately."""
    errors.append(msg)
    if args.ci:
        print(f"ERROR: {msg}")
        sys.exit(1)


def _route_items(routes: object) -> list[tuple[str, object]]:
    """Return [(path, route_dict)] regardless of whether routes is a dict."""
    if not isinstance(routes, dict):
        return []
    return list(routes.items())


def _element_items(elements: object) -> list[tuple[str, object]]:
    """Return [(route_path, elements)] regardless of whether elements is a dict."""
    if not isinstance(elements, dict):
        return []
    return list(elements.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate frontend/src/manifest.yaml integrity.")
    parser.add_argument("--ci", action="store_true", help="CI-friendly raw output, exit 1 on first error")
    parser.add_argument("--repo-root", help="Override the repo root (for fixture-based testing)")
    args = parser.parse_args(argv)

    repo_root = args.repo_root or REPO_ROOT
    manifest_path = os.path.join(repo_root, "frontend", "src", "manifest.yaml")

    if not os.path.isfile(manifest_path):
        print(f"ERROR: Manifest not found at {manifest_path}", file=sys.stderr)
        return 1

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)
    if manifest is None:
        manifest = {}

    routes = manifest.get("routes") or {}
    elements = manifest.get("elements") or {}

    route_items = _route_items(routes)
    element_items = _element_items(elements)
    errors: list[str] = []

    # ---- Rule 1: Every route.name matches a Vue Router route name ----
    if not args.ci:
        print("Rule 1: Route names match router", file=sys.stderr)
    router_names: set[str] = set()
    router_dir = os.path.join(repo_root, "frontend", "src", "router")
    if os.path.isdir(router_dir):
        for name in sorted(os.listdir(router_dir)):
            if not name.endswith(".ts"):
                continue
            with open(os.path.join(router_dir, name), encoding="utf-8") as fh:
                content = fh.read()
            router_names.update(_ROUTER_RE.findall(content))
    for path, value in route_items:
        route = value if isinstance(value, dict) else {}
        route_name = route.get("name")
        if route_name and route_name not in router_names:
            _write_err(f"Route '{route_name}' ({path}) not found in router", errors, args)

    # ---- Rule 2: Every static element.testid exists in Vue templates ----
    if not args.ci:
        print("Rule 2: Element testids exist in templates", file=sys.stderr)
    vue_contents: list[str] = []
    vue_root = os.path.join(repo_root, "frontend", "src")
    if os.path.isdir(vue_root):
        for dirpath, _dirnames, filenames in os.walk(vue_root):
            for fname in filenames:
                if not fname.endswith(".vue"):
                    continue
                with open(os.path.join(dirpath, fname), encoding="utf-8") as fh:
                    vue_contents.append(fh.read())
    for route_path, value in element_items:
        items = value if isinstance(value, list) else [value]
        for el in items:
            if not isinstance(el, dict):
                continue
            if el.get("dynamic_testid") is True:
                continue
            testid = el.get("testid")
            if not testid:
                continue
            found = False
            for content in vue_contents:
                if (
                    f'data-testid="{testid}"' in content
                    or f"setAttribute('data-testid', '{testid}')" in content
                    or f".dataset.testid = '{testid}'" in content
                ):
                    found = True
                    break
            if not found:
                _write_err(f"testid '{testid}' on route {route_path} not found in any template", errors, args)

    # ---- Rule 3: Every product_map references a file in docs/product-map/ ----
    if not args.ci:
        print("Rule 3: Product map refs exist", file=sys.stderr)
    product_map_files: set[str] = set()
    product_map_dir = os.path.join(repo_root, "docs", "product-map")
    if os.path.isdir(product_map_dir):
        for _dirpath, _dirnames, filenames in os.walk(product_map_dir):
            for fname in filenames:
                if fname.endswith(".md"):
                    product_map_files.add(os.path.splitext(fname)[0])
    for path, value in route_items:
        route = value if isinstance(value, dict) else {}
        pm = route.get("product_map")
        if pm and pm not in product_map_files:
            _write_err(f"product_map '{pm}' on {path} not found", errors, args)

    # ---- Rule 4: Every i18n_key exists in en-US.js ----
    if not args.ci:
        print("Rule 4: i18n keys exist", file=sys.stderr)
    top_level_keys: set[str] = set()
    i18n_file = os.path.join(repo_root, "frontend", "src", "locales", "en-US.js")
    if os.path.isfile(i18n_file):
        with open(i18n_file, encoding="utf-8") as fh:
            i18n_content = fh.read()
        top_level_keys.update(_I18N_RE.findall(i18n_content))
    for path, value in route_items:
        route = value if isinstance(value, dict) else {}
        ik = route.get("i18n_key")
        if ik:
            top_key = str(ik).split(".")[0]
            if top_key not in top_level_keys:
                _write_err(
                    f"i18n_key '{ik}' on {path}: top-level namespace '{top_key}' not found in en-US.js",
                    errors,
                    args,
                )

    # ---- Rule 5: No orphaned elements ----
    if not args.ci:
        print("Rule 5: No orphaned elements", file=sys.stderr)
    route_paths = {path for path, _value in route_items}
    for route_path, _value in element_items:
        if route_path not in route_paths:
            _write_err(f"Elements block for '{route_path}' has no matching route", errors, args)

    # ---- Rule 6: No circular parent chains ----
    if not args.ci:
        print("Rule 6: No circular parents", file=sys.stderr)
    routes_by_path = {path: (value if isinstance(value, dict) else {}) for path, value in route_items}

    def check_parent(path: str, chain: list[str]) -> None:
        if len(chain) > 20:
            _write_err(f"Circular parent chain: {' -> '.join(chain)}", errors, args)
            return
        route = routes_by_path.get(path)
        if not route or not route.get("parent"):
            return
        parent_val = str(route.get("parent"))
        if parent_val in chain:
            _write_err(f"Circular parent: {' -> '.join(chain)} -> {parent_val}", errors, args)
            return
        check_parent(parent_val, chain + [parent_val])

    for path, _value in route_items:
        check_parent(path, [path])

    # ---- Rule 7: Dynamic routes have pattern and dynamic_params ----
    if not args.ci:
        print("Rule 7: Dynamic routes complete", file=sys.stderr)
    for path, value in route_items:
        route = value if isinstance(value, dict) else {}
        if route.get("type") == "detail_page":
            if not route.get("pattern"):
                _write_err(f"Dynamic route '{path}' missing pattern", errors, args)
            if not route.get("dynamic_params"):
                _write_err(f"Dynamic route '{path}' missing dynamic_params", errors, args)

    if errors:
        if not args.ci:
            print(f"{len(errors)} validation errors", file=sys.stderr)
            for msg in errors:
                print(f"  {msg}", file=sys.stderr)
        return 1

    if not args.ci:
        print("All rules passed!", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
