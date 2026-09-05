"""Architecture tests: runtime-provider hub consumers stay on the ABC (FAR-587).

Two scanner-based contracts complement the import-linter rules:

1. **No concrete provider imports outside the package** — ``docker.py`` /
   ``e2b.py`` / ``local.py`` (and the legacy ``local_docker.py`` adapter) may
   only be imported from within ``core/runtime_provider/``. import-linter
   enforces the module-level rule; this test is the architecture-suite backstop.
2. **Hub consumers call only ABC-declared methods** — files that consume the
   hub or its resolved providers may only call methods declared on the ABC or
   the hub's public surface (import-linter cannot check call sites).

They run without a database.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"
PACKAGE = SRC / "core" / "runtime_provider"

_CONCRETE_MODULES = {
    "modulo.core.runtime_provider.docker",
    "modulo.core.runtime_provider.e2b",
    "modulo.core.runtime_provider.local",
    "modulo.core.runtime_provider.local_docker",
}

# The public surface hub consumers are allowed to call: the hub's own methods
# plus the RuntimeProvider ABC's declared methods.
_ABC_METHODS = {
    # RuntimeProviderHub
    "register",
    "unregister",
    "get",
    "list_providers",
    "resolve",
    "initialise",
    "aclose",
    # RuntimeProvider ABC
    "create_workspace",
    "exec_command",
    "destroy_workspace",
    "get_workspace_status",
    "close",
    "matches_provider_type",
}


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "migrations" not in p.parts]


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_concrete_providers_not_imported_outside_runtime_provider_package() -> None:
    violations: list[str] = []
    for path in _python_files():
        rel = path.relative_to(SRC).as_posix()
        in_package = PACKAGE in path.parents or path.parent == PACKAGE
        if in_package:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for module in _imported_modules(tree):
            if module in _CONCRETE_MODULES:
                violations.append(f"  {rel}: imports concrete provider module {module}")
    assert not violations, (
        "Concrete runtime-provider modules must only be imported inside core/runtime_provider.\n"
        "Resolve providers via RuntimeProviderHub.resolve() / build_hub() instead.\n" + "\n".join(violations)
    )


def test_hub_consumers_call_only_abc_methods() -> None:
    """Files importing the runtime_provider package call only the public surface.

    Receiver tracking is name-based: names bound (directly or via assignment)
    to the hub/provider objects are collected first, then only method calls on
    those receivers are checked against the ABC/hub surface.
    """
    violations: list[str] = []
    for path in _python_files():
        rel = path.relative_to(SRC).as_posix()
        in_package = PACKAGE in path.parents or path.parent == PACKAGE
        if in_package:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        imported = _imported_modules(tree)
        if not any(m.startswith("modulo.core.runtime_provider") for m in imported):
            continue

        # Seed with names imported from the runtime_provider package itself.
        provider_names: set[str] = set()
        prefix = "modulo.core.runtime_provider"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
                provider_names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(prefix):
                        provider_names.add((alias.asname or alias.name).split(".")[0])

        # Fixpoint: names assigned from provider objects or hub resolution.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not _is_provider_expr(node.value, provider_names):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in provider_names:
                        provider_names.add(target.id)
                        changed = True

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if not _is_provider_expr(node.func.value, provider_names):
                continue
            method = node.func.attr
            if method in _ABC_METHODS or method.startswith("_"):
                continue
            violations.append(f"  {rel}:{node.lineno}  .{method}() is not on the runtime-provider ABC/hub surface")
    assert not violations, (
        "Hub consumers must only call ABC-declared provider methods or RuntimeProviderHub's "
        "public surface (ADR 029).\n" + "\n".join(violations)
    )


def _is_provider_expr(node: ast.expr, names: set[str]) -> bool:
    """Whether *node* evaluates to a runtime-provider hub/provider object."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in names or func.id.lower().endswith("hub")
        if isinstance(func, ast.Attribute):
            # hub.resolve(...), hub.get(...), build_hub(...)-style factories
            return func.attr in {"resolve", "get", "list_providers"} and _is_provider_expr(func.value, names)
    return False
