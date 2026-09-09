"""Architecture test: every run-blob read goes through the repo (FAR-583).

The ``run_node_outputs`` store is the single blob chokepoint; legacy
``runs`` blob columns (``outputs_json`` / ``node_telemetry_json`` /
``raw_output_markers``) remain IN THE DATABASE until B2b (migration 0194)
but their ORM mapping was cut at B1, so the only sanctioned touchpoints left
are raw-SQL surfaces. This lens guards the B1 contract cut: no production
code may reference the legacy columns EXCEPT the raw-SQL surfaces below.
Three scans over ``backend/src`` (migrations excluded):

1. AST visitor: zero ``Attribute`` references to the three column names, and
   zero non-locally-bound ``Name`` references, outside the allowlist. Local
   function parameters named ``outputs_json`` (the data-processing helpers in
   ``node_output_split`` / ``classify``) and the ``_RunStatusUpdate`` payload
   fields (crud.run) are NOT column references — the symtable/scope analysis
   distinguishes locally-bound names from free/global ones.
2. Zero ``getattr(<expr>, "<column>")`` patterns (the string form the
   compiler cannot trace).
3. RAW-TEXT scan for the three column names in string constants, with
   docstrings filtered (the ``params.py``-style prose mentions must not
   false-positive) — catches string-embedded SQL the AST identifier scan
   cannot. Comments never execute and are not scanned.

B1 state asserted: the dual-write chokepoint write lines, the reconciles'
legacy-column scan tuple, the marker helper's ORM attribute leg, and the
three ``_RUNS_LIST_DEFERRED_COLUMNS`` entries are GONE; the only remaining
column references are the repo module's own raw Core legacy-table legs and
the shape-key/comment surfaces that mirror the legacy names by contract.
B2a removes the sweep entries and makes this test fully unconditional.
"""

import ast
import symtable
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"

COLUMNS = ("outputs_json", "node_telemetry_json", "raw_output_markers")

# Whole-file allowlist — every reference in these files is sanctioned:
# (path-relative-to-src/modulo, reason).
_WHOLE_FILE_ALLOWLIST: dict[str, str] = {
    "db/crud/run_node_outputs.py": (
        "the repo module — the single blob chokepoint: the new-table columns "
        "(ORM columns, legit) + the raw Core legacy-table readers that serve "
        "the EMPTY/MISMATCH fallback, the fenced markers join, the catch-up "
        "sweep's selection, and the marker dual-write's legacy leg"
    ),
    "db/models/run_node_outputs.py": "the new-table model + its portable CHECK constraints",
    "core/analytics/maintenance.py": (
        "SQL-side facts formula — new-table column reads + the legacy coalesce"
        " fallback through the raw Core legacy table (documented parity, dropped at B2b)"
    ),
}

# Scoped allowlist — (file, innermost-enclosing-scope-name): the only
# non-allowlisted-file Attribute references permitted, each a NON-column
# in-memory field that merely shares the legacy column's name.
_SCOPED_ALLOWLIST: dict[tuple[str, str], str] = {
    ("api/routes/runs.py", "build_fixture_map"): "RunIOResponse wire-shape field, not the ORM column",
    ("api/routes/runs.py", "_build_messages"): "_MessageContext dataclass field, not the ORM column",
    ("db/crud/run.py", "update_run_status"): (
        "_RunStatusUpdate payload kwargs (the blobs write API surface), not the ORM column"
    ),
    ("db/crud/run.py", "_update_run_status_fenced"): (
        "_RunStatusUpdate payload fields (the blobs write API surface), not the ORM column"
    ),
}

# Raw-text allowlist — whole files whose string constants may carry the
# column names (shape keys / SQL inside sanctioned modules).
_RAW_TEXT_FILE_ALLOWLIST: dict[str, str] = {
    "db/crud/run_node_outputs.py": "the repo module (dict keys + upsert set_ kwargs)",
    "db/crud/run_retention.py": "export payload shape keys (the export format mirrors the legacy names by contract)",
    "db/models/run_daily_facts.py": "facts column comments (updated at B2b)",
    "db/models/run_node_outputs.py": "the model's portable CHECK constraint strings",
    "core/cost_controller/finalize.py": "finalize ledger/fact payload shape keys",
}


def _scope_tracked_nodes(tree: ast.AST) -> list[tuple[ast.Attribute, str]]:
    """Collect Attribute nodes annotated with their innermost enclosing
    function/class scope name (module scope = "")."""
    found: list[tuple[ast.Attribute, str]] = []
    stack: list[str] = [""]

    class Visitor(ast.NodeVisitor):
        def _scoped(self, node: ast.AST, name: str) -> None:
            stack.append(name)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scoped(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._scoped(node, node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scoped(node, node.name)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            found.append((node, stack[-1]))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _locally_bound_names(source: str, filename: str) -> set[str]:
    """Names bound as parameters/locals anywhere in the module (symtable)."""
    bound: set[str] = set()
    try:
        top = symtable.symtable(source, filename, "exec")
    except (SyntaxError, ValueError):
        return bound

    def walk(table: symtable.SymbolTable) -> None:
        for sym in table.get_symbols():
            if sym.is_local() or sym.is_parameter():
                bound.add(sym.get_name())
        for child in table.get_children():
            walk(child)

    walk(top)
    return bound


def _is_docstring(node: ast.Constant, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    if parent is None:
        return False
    if not isinstance(parent, ast.Expr):
        return False
    grandparent = parents.get(id(parent))
    if grandparent is None:
        return False
    if not isinstance(grandparent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    first = grandparent.body[0] if grandparent.body else None
    return first is parent


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def test_no_legacy_blob_reads_outside_the_repo():
    attr_violations: list[str] = []
    name_violations: list[str] = []
    getattr_violations: list[str] = []
    raw_text_violations: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "migrations" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        whole_file_allowed = rel in _WHOLE_FILE_ALLOWLIST
        bound = _locally_bound_names(source, str(path))
        parents = _parent_map(tree)

        for node, scope in _scope_tracked_nodes(tree):
            if node.attr not in COLUMNS:
                continue
            if whole_file_allowed:
                continue
            if (rel, scope) in _SCOPED_ALLOWLIST:
                continue
            attr_violations.append(f"  {rel}:{node.lineno}  .{node.attr} (scope {scope or '<module>'})")

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in COLUMNS and node.id not in bound and not whole_file_allowed:
                name_violations.append(f"  {rel}:{node.lineno}  bare name {node.id} ({type(node.ctx).__name__})")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in COLUMNS
            ):
                getattr_violations.append(f"  {rel}:{node.lineno}  getattr(..., '{node.args[1].value}')")

            # RAW-TEXT: string constants carrying a column name, docstrings
            # excluded; comments filtered via tokenize below.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and not whole_file_allowed
                and rel not in _RAW_TEXT_FILE_ALLOWLIST
                and not _is_docstring(node, parents)
                and any(col in node.value for col in COLUMNS)
            ):
                raw_text_violations.append(f"  {rel}:{node.lineno}  string {node.value[:72]!r}")

        if whole_file_allowed or rel in _RAW_TEXT_FILE_ALLOWLIST:
            continue

    problems: list[str] = []
    if attr_violations:
        problems.append("Attribute refs to legacy blob columns outside the allowlist:\n" + "\n".join(attr_violations))
    if name_violations:
        problems.append("Bare non-local name refs outside the allowlist:\n" + "\n".join(name_violations))
    if getattr_violations:
        problems.append("getattr(..., '<blob>') banned — call the repo reader:\n" + "\n".join(getattr_violations))
    if raw_text_violations:
        problems.append(
            "Raw-text refs to legacy blob columns outside the allowlist:\n" + "\n".join(raw_text_violations)
        )
    assert not problems, (
        "FAR-583 read-switch violated. Every run-blob read must go through\n"
        "modulo.db.crud.run_node_outputs (the legacy columns dual-write until\n"
        "B1 and drop at B2b); extend the allowlist ONLY for a sanctioned\n"
        "surface with a documented reason.\n\n" + "\n\n".join(problems)
    )
