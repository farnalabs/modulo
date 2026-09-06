#!/usr/bin/env python3
"""E2E route coverage gate (FAR-637).

Reinstate of the retired check-e2e-coverage.ps1: every route declared in
``frontend/src/router/index.ts`` must be referenced by at least one Playwright
spec under ``frontend/tests/e2e/``. This is the guard against routes rotting
with zero e2e touching them (/pipelines/:id/editor, FAR-616).

How matching works (and its limitations):

- Route paths are extracted with the ``path: '([^']+)'`` regex from the router
  source. The catch-all ``/:pathMatch(.*)*`` is ignored entirely.
- Normalisation drops ``:param`` segments (including optional ones like
  ``:id?``) but keeps any static segments that follow a param, yielding the
  route's static prefix. Trailing slashes are stripped. Examples:
  ``/pipelines/:id/editor`` -> ``/pipelines/editor`` (static segment kept
  after the param) and ``/runs/:id`` -> ``/runs`` (nothing after the param).
- A route is "covered" when any spec file CONTAINS its static prefix as a
  plain substring. This is deliberately simple: a spec mentioning
  ``/runs/diff`` also covers ``/runs/:id``, and a spec containing
  ``/admin/costs/components`` also covers ``/admin/costs``. The gate is a
  rot-detector (does anything reference this route at all), not a proof that
  the route's behaviour is asserted.

  Fill-work trap (for FAR-638): matching is substring-on-the-static-prefix, so
  a spec that navigates ``/pipelines/<uuid>/editor`` does NOT satisfy
  ``/pipelines/editor`` and the route stays UNCOVERED even when exercised. The
  FAR-638 specs must include the literal static prefix (e.g. reference
  ``/pipelines/editor`` as a string, or reach the page via client-side
  navigation from a spec that does) or the gate stays red after the fill.

Verdict: exit 0 when every non-allowlisted route is referenced by at least one
spec; exit 1 with a ``::error::`` annotation listing the uncovered routes
otherwise. The per-route table always prints to stdout and, when present, to
``$GITHUB_STEP_SUMMARY``. A malformed router file or missing e2e directory
exits 2.

Known-uncovered routes are tracked for delivery in FAR-638; the allowlist
below is only for routes that are special flows, not navigable MVP pages.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_PATH = REPO_ROOT / "frontend" / "src" / "router" / "index.ts"
E2E_DIR = REPO_ROOT / "frontend" / "tests" / "e2e"

# ALLOWLIST - routes exempt from the gate, each with the reason:
#   /auth/callback, /oauth/authorize, /demo, /accept-invite - special auth /
#     handoff flows driven by backend redirects and URL fragments, not pages a
#     spec navigates to directly (login.spec.ts exercises login itself).
#   /admin/system/orgs, /admin/system/config - system-admin-only instance
#     administration (is_system_admin JWT claim); no test identity holds that
#     claim in e2e.
#   /admin/product-analytics, /dev/metrics - system-admin-only diagnostics
#     surfaces (Web Vitals, product analytics); same identity limitation.
ALLOWLISTED_ROUTES: frozenset[str] = frozenset(
    {
        "/auth/callback",
        "/oauth/authorize",
        "/demo",
        "/accept-invite",
        "/admin/system/orgs",
        "/admin/system/config",
        "/admin/product-analytics",
        "/dev/metrics",
    }
)

ROUTE_PATH_RE = re.compile(r"path:\s*'([^']+)'")
CATCH_ALL_PREFIX = "/:pathMatch"


def extract_route_paths(router_text: str) -> list[str]:
    """Return every ``path: '...'`` value in declaration order."""
    return ROUTE_PATH_RE.findall(router_text)


def normalise_route(route: str) -> str | None:
    """Reduce a router path to its static prefix, or None to skip it.

    ``:param`` segments (including optional ones like ``:id?``) are dropped
    and the result is joined back on '/', with trailing slashes stripped
    (the root route stays ``/``). The catch-all returns None.
    """
    if route.startswith(CATCH_ALL_PREFIX):
        return None
    static = [seg for seg in route.split("/") if seg and not seg.startswith(":")]
    normalised = "/" + "/".join(static)
    if normalised == "/" or not static:
        return "/"
    return normalised.rstrip("/")


def load_spec_texts() -> dict[str, str]:
    """Read every e2e spec file into {relative_path: content}."""
    if not E2E_DIR.is_dir():
        raise FileNotFoundError(f"e2e directory not found: {E2E_DIR}")
    specs: dict[str, str] = {}
    for spec in sorted(E2E_DIR.rglob("*.spec.ts")):
        specs[str(spec.relative_to(REPO_ROOT))] = spec.read_text(encoding="utf-8")
    if not specs:
        raise FileNotFoundError(f"no *.spec.ts files found under: {E2E_DIR}")
    return specs


def build_coverage(routes: list[str], specs: dict[str, str]) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """Return (per-route rows, uncovered route paths).

    Rows are ``(route_path, normalised_key, covering_spec_or_None)`` in router
    declaration order; two routes may share a normalised key.
    """
    rows: list[tuple[str, str, str | None]] = []
    uncovered: list[str] = []
    for route in routes:
        key = normalise_route(route)
        if key is None:
            continue  # catch-all: ignored entirely
        if key in ALLOWLISTED_ROUTES:
            rows.append((route, key, "(allowlisted)"))
            continue
        covering = next((name for name, text in specs.items() if key in text), None)
        rows.append((route, key, covering))
        if covering is None:
            uncovered.append(route)
    return rows, uncovered


def render_table(rows: list[tuple[str, str, str | None]]) -> str:
    if not rows:
        return "(no routes to display)"
    width = max(len(route) for route, _, _ in rows)
    lines = [f"{'route'.ljust(width)}  normalised prefix        covered by"]
    for route, key, covering in rows:
        mark = covering or "UNCOVERED"
        lines.append(f"{route.ljust(width)}  {key.ljust(24)}  {mark}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    del argv  # the gate takes no arguments; CWD-independent by design
    if not ROUTER_PATH.is_file():
        print(f"E2E route coverage gate: router file not found: {ROUTER_PATH}", file=sys.stderr)
        return 2
    try:
        router_text = ROUTER_PATH.read_text(encoding="utf-8")
        routes = extract_route_paths(router_text)
        if not routes:
            print(
                f"E2E route coverage gate: no 'path: ...' entries parsed from {ROUTER_PATH} - "
                "router file malformed or moved?",
                file=sys.stderr,
            )
            return 2
        specs = load_spec_texts()
    except (OSError, FileNotFoundError) as exc:
        print(f"E2E route coverage gate: input error: {exc}", file=sys.stderr)
        return 2

    rows, uncovered = build_coverage(routes, specs)
    table = render_table(rows)
    print(table)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as fh:
            fh.write("### E2E route coverage gate (FAR-637)\n")
            fh.write(f"{len(routes)} route paths parsed, {len(ALLOWLISTED_ROUTES)} allowlisted.\n\n")
            fh.write("```\n" + table + "\n```\n")

    if uncovered:
        listing = ", ".join(uncovered)
        print(f"::error::E2E route coverage gate failed: {len(uncovered)} uncovered routes: {listing}")
        return 1
    print(f"E2E route coverage gate passed: all {len(rows)} routes covered.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
