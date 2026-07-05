"""Architecture test: no module-level side effects in production code.

Module-level function calls (not wrapped in def, class, if __name__,
or conditional) execute at import time and can crash on cold bootstrap.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"


def test_no_module_level_side_effects():
    violations = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            # Skip imports, defs, classes, assignments, type aliases, decorators
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            # Skip if __name__ == '__main__' guard
            if isinstance(node, ast.If):
                test = node.test
                if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__":
                    continue
            # Skip expressions with decorators (which get processed by FunctionDef/ClassDef)
            if isinstance(node, ast.Expr):
                call = node.value
                if isinstance(call, ast.Call):
                    func = call.func
                    if isinstance(func, ast.Name):
                        # Allow known-good patterns: _validate_*, _check_* at module level
                        if func.id.startswith(("_validate", "_check", "_init_once")):
                            continue
                        violations.append(f"  {path.relative_to(SRC)}:{node.lineno}  Module-level call '{func.id}()'")
    assert not violations, (
        f"Found {len(violations)} module-level function call(s) that execute on import.\n"
        "Wrap in a lazy function or guard with if __name__ == '__main__'.\n"
        + "\n".join(violations)
    )
