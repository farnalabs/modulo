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
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "rollback":
                continue
            # Check if we're inside an exception handler
            parent = _find_parent_except_handler(tree, node)
            if parent is not None:
                continue
            violations.append(f"  {path.relative_to(SRC)}:{node.lineno}  {ast.unparse(call)[:80]}")
    assert not violations, (
        f"Found {len(violations)} session.rollback() calls outside exception handlers.\n"
        "Use savepoint = await session.begin_nested() instead.\n"
        + "\n".join(violations)
    )


def _find_parent_except_handler(tree, node):
    for parent_node in ast.walk(tree):
        if isinstance(parent_node, ast.ExceptHandler):
            for child in ast.walk(parent_node):
                if child is node:
                    return parent_node
    return None
