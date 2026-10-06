#!/usr/bin/env python3
"""Audit-coverage ratchet — find mutating REST routes / MCP tools with no audit call.

FAR-1472, PR 1 of the audit sweep: most mutating routes emit no audit event.
The ratchet is a baseline file plus this scanner, so a NEW unannotated
mutating route (or MCP tool) fails ``tests/architecture/test_audit_coverage.py``
while the remaining gaps are closed annotation-by-annotation (annotate it,
regenerate the baseline).

Two surfaces, one baseline:

* **REST routes** — ``backend/src/modulo/api/routes/*.py``. A route is covered
  by an ``audited(...)`` / ``audited_system(...)`` call anywhere on its
  decorator list or parameter defaults (the ``dependencies=[Depends(...)]``
  shape or an ``@audited(...)`` decorator both qualify).
* **MCP tools** — ``backend/src/modulo/api/mcp_server.py``. MCP tools bypass
  FastAPI entirely, so they carry ``@mcp_audited(...)`` directly under
  ``@mcp.tool(...)``; a tool counts as covered when any decorator (or default)
  calls an accepted audit annotation.

Run from ``backend/``::

    uv run python scripts/audit_coverage.py            # check (exit 1 on violations)
    uv run python scripts/audit_coverage.py --update   # regenerate the baseline

Dependency-light on purpose: standard library only (``ast``), so it runs
anywhere — semgrep cannot (no Windows binary, and its pre-commit wrapper fails
open), which is why the ratchet is an architecture test rather than a rule.

Identity is ``<file name>:<function name>`` — stable across line moves and
reformatting; a rename is caught as a stale baseline entry.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROUTES_DIR = BACKEND_DIR / "src" / "modulo" / "api" / "routes"
MCP_SERVER_PATH = BACKEND_DIR / "src" / "modulo" / "api" / "mcp_server.py"
BASELINE_PATH = BACKEND_DIR / "tests" / "architecture" / "audit_coverage_baseline.txt"

#: HTTP methods that mutate state (``GET``/``HEAD``/``OPTIONS`` never do).
MUTATING_METHODS = frozenset({"post", "put", "patch", "delete"})

#: Audit annotations the ratchet accepts. ``audited`` is the route dependency
#: from ``modulo.core.audit_coverage``; ``audited_system`` is its actor-less
#: sibling for routes with no principal (system actor); ``mcp_audited`` is the
#: MCP tool decorator from ``modulo.api.mcp_audit``.
AUDITED_CALL_NAMES = frozenset({"audited", "audited_system", "mcp_audited"})

#: Verb prefixes that mark an MCP tool as mutating (the ``@mcp.tool`` surface
#: names its writers by verb, and its readers never carry these prefixes).
MUTATING_TOOL_PREFIXES = (
    "apply_",
    "bind_",
    "cancel_",
    "copy_",
    "create_",
    "delete_",
    "perform_",
    "purge_",
    "remove_",
    "restore_",
    "review_",
    "revoke_",
    "set_",
    "trigger_",
    "update_",
)

_BASELINE_HEADER = """\
# Audit-coverage baseline: mutating REST routes / MCP tools deliberately left
# WITHOUT an audited(...) dependency (FAR-1472). Generated, never hand-edited —
# regenerate with:  uv run python scripts/audit_coverage.py --update
# Delete a line as soon as the route/tool is annotated; the architecture test
# fails on a stale entry so this list can only shrink.
"""

_ROUTE_HINT = (
    'add dependencies=[Depends(audited("<event_type>", "<resource_type>", '
    "principal_dep=<the route's principal dependency>))] to the route, "
    "then regenerate the baseline with: uv run python scripts/audit_coverage.py --update"
)

_MCP_HINT = (
    'add @mcp_audited("<event_type>", "<resource_type>", fail_closed=...) directly under '
    "@mcp.tool(...) on the tool (fail-closed for destructive/credential tools), "
    "then regenerate the baseline with: uv run python scripts/audit_coverage.py --update"
)


def _called_name(func: ast.expr) -> str | None:
    """Return the trailing name of a call target (``Depends(x).y`` -> ``y``)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _mentions_audited(node: ast.AST) -> bool:
    """True when the subtree contains a call to an accepted audit annotation."""
    return any(isinstance(sub, ast.Call) and _called_name(sub.func) in AUDITED_CALL_NAMES for sub in ast.walk(node))


def _is_mutating_route_decorator(node: ast.expr) -> bool:
    """True for ``@router.post(...)``-style decorators (any receiver, any path)."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """True for ``@mcp.tool(...)``-style decorators (the FastMCP registration)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "tool":
        return False
    # Require the ``mcp`` receiver where it is a plain name, so an unrelated
    # ``x.tool(...)`` decorator on another object is never mistaken for one.
    receiver = node.func.value
    return not isinstance(receiver, ast.Name) or receiver.id == "mcp"


def _is_mutating_mcp_tool(name: str) -> bool:
    """True for an MCP tool whose name starts with a mutating verb prefix."""
    return name.startswith(MUTATING_TOOL_PREFIXES)


def _default_expressions(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """All parameter defaults (FastAPI puts ``Depends(...)`` in defaults)."""
    args = fn.args
    return [*args.defaults, *[d for d in args.kw_defaults if d is not None]]


def scan_source(source: str, filename: str) -> set[str]:
    """Return the unannotated mutating routes in one module's source.

    A route counts as covered when its audit call appears in ANY decorator on
    the handler — the route decorator's ``dependencies=[...]`` list or an
    ``@audited(...)`` decorator both qualify.
    """
    tree = ast.parse(source, filename=filename)
    file_name = Path(filename).name
    unannotated: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_mutating_route_decorator(d) for d in node.decorator_list):
            continue
        annotated = any(_mentions_audited(d) for d in node.decorator_list)
        if not annotated:
            annotated = any(_mentions_audited(d) for d in _default_expressions(node))
        if not annotated:
            unannotated.add(f"{file_name}:{node.name}")

    return unannotated


def scan_mcp_source(source: str, filename: str = MCP_SERVER_PATH.name) -> set[str]:
    """Return the unannotated mutating MCP tools in one module's source.

    A tool is in scope when it is registered with ``@mcp.tool(...)`` AND its
    name carries a mutating verb prefix; it counts as covered when any
    decorator (or parameter default) calls ``mcp_audited`` / ``audited`` /
    ``audited_system``.
    """
    tree = ast.parse(source, filename=filename)
    file_name = Path(filename).name
    unannotated: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
            continue
        if not _is_mutating_mcp_tool(node.name):
            continue
        annotated = any(_mentions_audited(d) for d in node.decorator_list)
        if not annotated:
            annotated = any(_mentions_audited(d) for d in _default_expressions(node))
        if not annotated:
            unannotated.add(f"{file_name}:{node.name}")

    return unannotated


def scan_mcp_tree(mcp_path: Path = MCP_SERVER_PATH) -> set[str]:
    """Scan the MCP server module and return all unannotated mutating tools."""
    try:
        source = mcp_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - unreadable file
        raise RuntimeError(f"cannot read MCP server module {mcp_path}: {exc}") from exc
    try:
        return scan_mcp_source(source, mcp_path.name)
    except SyntaxError as exc:  # pragma: no cover - a broken module
        raise RuntimeError(f"cannot parse MCP server module {mcp_path}: {exc}") from exc


def scan_tree(routes_dir: Path = ROUTES_DIR, mcp_path: Path = MCP_SERVER_PATH) -> set[str]:
    """Scan BOTH audited surfaces (routes + MCP tools) for unannotated writers."""
    unannotated: set[str] = set()
    for path in sorted(routes_dir.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - unreadable file
            raise RuntimeError(f"cannot read route module {path}: {exc}") from exc
        try:
            unannotated |= scan_source(source, str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken route module
            raise RuntimeError(f"cannot parse route module {path}: {exc}") from exc
    return unannotated | scan_mcp_tree(mcp_path)


def read_baseline(baseline_path: Path = BASELINE_PATH) -> set[str]:
    """Read the baseline, ignoring blank lines and ``#`` comments."""
    if not baseline_path.exists():
        return set()
    entries: set[str] = set()
    for line in baseline_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped)
    return entries


def render_baseline(entries: set[str]) -> str:
    """Render the baseline file body (header + sorted entries + trailing newline)."""
    lines = [_BASELINE_HEADER]
    lines.extend(f"{entry}\n" for entry in sorted(entries))
    return "".join(lines)


def compare(unannotated: set[str], baseline: set[str]) -> tuple[list[str], list[str]]:
    """Split the scan into (new violations, stale baseline entries)."""
    new_violations = sorted(unannotated - baseline)
    stale_entries = sorted(baseline - unannotated)
    return new_violations, stale_entries


def _hint_for(entry: str) -> str:
    """Per-surface remediation hint for one unannotated writer."""
    return _MCP_HINT if entry.startswith(MCP_SERVER_PATH.name + ":") else _ROUTE_HINT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate the baseline from the current tree (complete by construction)",
    )
    args = parser.parse_args(argv)

    unannotated = scan_tree()

    if args.update:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(render_baseline(unannotated), encoding="utf-8", newline="\n")
        print(f"audit-coverage: baseline written with {len(unannotated)} writer(s) -> {BASELINE_PATH}")
        return 0

    baseline = read_baseline()
    new_violations, stale_entries = compare(unannotated, baseline)

    route_violations = {v for v in unannotated if not v.startswith(MCP_SERVER_PATH.name + ":")}
    mcp_violations = set(unannotated) - route_violations
    total_routes = _count_mutating_routes()
    total_mcp_tools = _count_mutating_mcp_tools()
    print(
        f"audit-coverage: {total_routes} mutating route(s), "
        f"{total_routes - len(route_violations)} audited; "
        f"{total_mcp_tools} mutating MCP tool(s), {total_mcp_tools - len(mcp_violations)} audited; "
        f"baseline {len(baseline)}"
    )

    if stale_entries:
        print(f"\n{len(stale_entries)} baseline entr(ies) are now annotated — regenerate the baseline:")
        for entry in stale_entries:
            print(f"  STALE  {entry}")
        print("\n  uv run python scripts/audit_coverage.py --update")

    if new_violations:
        print(f"\n{len(new_violations)} mutating route(s)/MCP tool(s) have no audit call:")
        for entry in new_violations:
            print(f"  MISSING  {entry}")
        hints = sorted({_hint_for(entry) for entry in new_violations})
        for hint in hints:
            print(f"\n  either {hint}")
        return 1

    if stale_entries:
        return 1

    print("audit-coverage: OK — every mutating route and MCP tool is audited or explicitly baselined")
    return 0


def _count_mutating_routes(routes_dir: Path = ROUTES_DIR) -> int:
    """Total mutating route decorators in the tree (reporting only)."""
    total = 0
    for path in sorted(routes_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _is_mutating_route_decorator(d) for d in node.decorator_list
            ):
                total += 1
    return total


def _count_mutating_mcp_tools(mcp_path: Path = MCP_SERVER_PATH) -> int:
    """Total mutating ``@mcp.tool`` registrations (reporting only)."""
    try:
        tree = ast.parse(mcp_path.read_text(encoding="utf-8"), filename=str(mcp_path))
    except (SyntaxError, OSError, UnicodeDecodeError):  # pragma: no cover
        return 0
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_mutating_mcp_tool(node.name)
        and any(_is_mcp_tool_decorator(d) for d in node.decorator_list)
    )


if __name__ == "__main__":
    sys.exit(main())
