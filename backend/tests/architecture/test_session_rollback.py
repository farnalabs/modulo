"""Architecture test: session.rollback() misuse in production code.

In production code, session.rollback() destroys ALL uncommitted writes
including from concurrent operations. Use savepoint = await session.begin_nested()
for local rollback scopes. This test ensures it's not used outside of
try/except blocks in src/ code.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"


def test_no_bare_session_rollback():
    """Rollback outside of exception handlers suggests savepoint misuse."""
    violations = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        # Single-pass membership set: nodes that appear inside an ExceptHandler.
        inside_except = _nodes_within_except_handlers(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            call = node.value
            if isinstance(call, ast.Await):
                call = call.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "rollback":
                continue
            if node in inside_except:
                continue
            violations.append(f"  {path.relative_to(SRC)}:{node.lineno}  {ast.unparse(node.value)[:80]}")
    assert not violations, (
        f"Found {len(violations)} session.rollback() calls outside exception handlers.\n"
        "Use savepoint = await session.begin_nested() instead.\n" + "\n".join(violations)
    )


def _nodes_within_except_handlers(tree: ast.AST) -> set[ast.AST]:
    """Return every node (by identity) that appears inside an ExceptHandler.

    Computed once per file so the per-rollback lookup is O(1) instead of
    repeatedly walking the whole tree for every ``.rollback()`` call.
    """
    contained: set[ast.AST] = set()
    for parent in ast.walk(tree):
        if isinstance(parent, ast.ExceptHandler):
            contained.update(ast.walk(parent))
    return contained
