"""Architecture test: no ``locals()`` magic-string variable lookups in production code.

``locals().get("name")`` / ``locals()["name"]`` reads a variable by its string
name at runtime instead of referencing it directly. The lookup is fragile under
rename/move (the string silently goes stale while a direct reference fails
loudly at compile time) and hides the unbound case behind a ``None``/default
instead of making it explicit. The lookup-hack pattern was eliminated from
``node_runner.py`` and ``api/routes/webhooks.py`` by pre-initializing the
variables and referencing them directly; this lens guards the class so it
cannot regress.

Deliberately out of scope: ``**locals()`` splatting into a call/dict literal,
which is a legitimate (if rare) idiom and is not a by-name variable read.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"


def _is_locals_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "locals"


def test_no_locals_lookup():
    violations = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "get" and _is_locals_call(node.value):
                violations.append(f"  {path.relative_to(SRC)}:{node.lineno}  locals().get(...)")
            elif isinstance(node, ast.Subscript) and _is_locals_call(node.value):
                violations.append(f"  {path.relative_to(SRC)}:{node.lineno}  locals()[...]")
    assert not violations, (
        f"Found {len(violations)} locals() magic-string variable lookup(s).\n"
        "Reference the variable directly — pre-initialize it before the try/except\n"
        "instead of probing locals() for a possibly-unassigned name.\n" + "\n".join(violations)
    )
