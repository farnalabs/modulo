"""Architecture test: no platform (``FLY_*``) env var is read in product code.

ADR 043 (Decision 5, FAR-1158/FAR-1194): platform awareness is a curation
layer, never a correctness dependency, and **no platform-specific env var may
be read for identity**. After FAR-1194 every instance-identity site routes
through the one exported resolver, ``modulo.settings.resolve_instance_identity()``
(platform hostname / ``socket.gethostname()``), so ``backend/src`` contains
zero ``FLY_*`` env reads — the coupling is grep-able and testable.

This lens flags any actual ``os.environ`` / ``os.getenv`` read of a ``FLY_*``
key anywhere under ``src/modulo`` outside an explicitly-declared platform
adapter. Docstring and comment mentions of ``FLY_*`` names (e.g. historical
notes in health.py) are not reads and are not flagged.

The adapter list is deliberately EMPTY today: ADR 043 Rule 6 defers the
platform declaration until a real curation consumer exists. When a platform
adapter is eventually introduced, add its repo-relative path here — that edit
is the declaration the ADR requires, and this test forces it to be explicit.

Raw grep equivalent (code lines only):
``grep -rn 'FLY_' backend/src --include='*.py' | grep environ``
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"

# Explicitly-declared platform adapters permitted to read FLY_* env vars,
# as POSIX-style paths relative to src/modulo (e.g. "core/platform/fly.py").
# Empty by design — ADR 043 Rule 6 defers platform adapters.
PLATFORM_ADAPTER_MODULES: frozenset[str] = frozenset()

_FLY_PREFIX = "FLY_"


def _is_environ_container(node: ast.AST) -> bool:
    """True for ``os.environ`` / ``environ`` (attribute or bare name)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    if isinstance(node, ast.Name):
        return node.id == "environ"
    return False


def _is_environ_getenv(func: ast.AST) -> bool:
    """True for ``os.getenv`` / ``getenv``."""
    if isinstance(func, ast.Attribute):
        return func.attr == "getenv"
    if isinstance(func, ast.Name):
        return func.id == "getenv"
    return False


def _fly_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(_FLY_PREFIX):
        return node.value
    return None


def _read_key(node: ast.AST) -> str | None:
    """The FLY_* key read by *node*, else None.

    Covers ``os.environ.get("FLY_X")``, ``os.environ["FLY_X"]``,
    ``os.environ["FLY_X"] = ...`` and ``os.getenv("FLY_X")`` — including
    through a non-Attribute environ alias.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if _is_environ_getenv(func):
            return _fly_literal(node.args[0]) if node.args else None
        if isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault") and _is_environ_container(func.value):
            return _fly_literal(node.args[0]) if node.args else None
        return None
    if isinstance(node, ast.Subscript) and _is_environ_container(node.value):
        return _fly_literal(node.slice)
    return None


def test_no_platform_env_reads_outside_declared_adapters():
    violations = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel in PLATFORM_ADAPTER_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            key = _read_key(node)
            if key is not None:
                violations.append(f"  {rel}:{getattr(node, 'lineno', '?')}  reads {key!r}")
    assert not violations, (
        f"Found {len(violations)} platform (FLY_*) env read(s) outside a declared platform adapter.\n"
        "Instance/platform identity must route through modulo.settings.resolve_instance_identity()\n"
        "(ADR 043 Decision 5); a genuinely platform-specific read belongs in an adapter whose path\n"
        "is declared in PLATFORM_ADAPTER_MODULES.\n" + "\n".join(violations)
    )
