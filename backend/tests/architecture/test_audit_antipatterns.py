"""Architecture test: audit anti-pattern guards (FAR-1538 residuals).

Two anti-patterns were found and fixed by hand during the FAR-1472 sweep; this
test makes each of them structurally impossible to reintroduce.

Guard 1 — a route that hard-deletes its organisation must not carry ``audited()``
---------------------------------------------------------------------------
``audited()`` appends its coarse event POST-COMMIT on a FRESH session (see
``modulo.core.audit_coverage``), while ``audit_events.organisation_id`` FKs
``organisations.id``. On a handler that hard-deletes the organisation, that
append can never satisfy the FK, so it fails open and records nothing. FAR-1517
moved ``admin.confirm_org_deletion``, ``admin.delete_org_immediate`` and
``admin_orgs.admin_delete_org`` to the in-transaction
``system_audit_logger.append_system_audit_event`` and dropped ``audited()``.

Detection is principled, not a list of function names: a handler is flagged
when the call graph reachable from it executes a hard delete of an
``Organisation`` row — ``session.delete(<x>)`` where ``<x>`` is statically
bound to an Organisation, or a core ``delete(Organisation)``. Bindings are
resolved by a small intra-procedural fixed point (the RHS mentions the
``Organisation`` model, comes from a call whose return annotation mentions
``Organisation``, or flows from an earlier org-typed binding), and callees are
resolved through their real imports against a lazily-parsed module index of
``backend/src/modulo`` — so ``delete_organisation`` / ``confirm_org_deletion``
are recognised from what they DO, and a synthetically-named helper that deletes
an org is caught all the same.

Guard 2 — the coarse ``audited()`` event must not reuse an inline event type
---------------------------------------------------------------------------
A route carrying ``audited("<type>")`` that ALSO emits its own rich inline
``append_audit_event(event_type="<type>")`` appends two events of one type per
action. The coarse event takes the secondary ``api_access_<verb>`` namespace
instead (PR #1339), leaving the rich domain event alone.

Detection: for each route handler, the intersection of (a) the event types
passed to ``audited`` / ``audited_system`` on its decorators or parameter
defaults, and (b) the event types that REACH an inline append from that
handler — its own body plus the same module's helpers it calls (depth ≤ 4),
including wrappers that forward their ``event_type`` parameter into
``append_audit_event`` and module-level string constants.

Why an architecture test and not semgrep: same reasoning as
``tests/architecture/test_audit_coverage.py`` — the rule has to run on every
platform the suite runs on.

Documented limitations (both guards)
------------------------------------
* Guard 1 flags any hard-deleted Organisation, not only the caller's own: at
  AST level the runtime target of ``org_id`` is not knowable, and FAR-1517
  removed ``audited()`` from the cross-org admin route too. Conservative in
  the safe direction (over-flags, never under-flags the known pattern).
* Guard 1 resolves module-level functions and their local imports only. A
  deletion reached through an instance method (``service.purge_org()``) or
  dynamic dispatch is not followed; today both real org-delete sites are
  module-level functions, so this costs nothing yet. Call-graph depth is
  capped at ``_MAX_CALL_DEPTH`` hops, and a nested function's body is counted
  as part of its enclosing function (over-approximation).
* Guard 1 covers REST route modules only — not MCP tools, the CLI, or cron
  paths that might delete an org without an ``audited()`` dependency.
* Guard 2 resolves reachability WITHIN a route module (handlers plus the
  module's own helpers). An inline emission made in a helper the handler calls
  in ANOTHER module (e.g. ``db/crud``) is out of scope; no such collision
  exists today, and widening to a cross-module call graph is a separate step.
* Guard 2 reads event types from literals, module-level string constants and
  parameter forwarding; a computed event type (f-string, subscript) resolves
  to nothing and is silently skipped — under-approximation, never a false
  flag.
* ``append_system_audit_event`` is deliberately NOT an inline emitter here: it
  writes the separate org-independent ledger, so the FAR-1517 pattern
  (``audited()`` + system-ledger append) is legitimate and must not flag.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BACKEND_DIR / "src" / "modulo"
ROUTES_DIR = SRC_DIR / "api" / "routes"

#: Decorator attributes that register a FastAPI route handler.
_ROUTE_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})

#: The coarse audit dependency factories from ``modulo.core.audit_coverage``.
_AUDITED_CALL_NAMES = frozenset({"audited", "audited_system"})

#: Inline chain appends from ``modulo.core.audit_logger``. Deliberately excludes
#: ``append_system_audit_event`` — that writes the org-independent ledger.
_INLINE_APPEND_NAMES = frozenset({"append_audit_event", "append_audit_event_isolated"})

#: Secondary namespace the coarse event must use when the route also emits a
#: rich inline event of its own (PR #1339).
_COARSE_RENAME_PREFIX = "api_access_"

#: Call-graph bound for both guards (handler -> helper -> crud -> delete).
_MAX_CALL_DEPTH = 4

#: The SQLAlchemy model whose row must never be gone by the time ``audited()``
#: fires its post-commit append.
_ORG_MODEL_NAME = "Organisation"

#: Org-model suffix that identifies a resolved symbol as the Organisation class
#: (``modulo.db.models.organisation.Organisation``).
_ORG_MODEL_SUFFIX = "." + _ORG_MODEL_NAME


@dataclass
class _ModuleInfo:
    """Everything the guards need from one parsed module."""

    name: str
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    imports: dict[str, str]
    constants: dict[str, str]


#: Synthetic modules under analysis (the teeth tests), keyed by module name.
_SYNTHETIC: dict[str, _ModuleInfo] = {}


# ---------------------------------------------------------------------------
# Module index — lazily parsed, cached
# ---------------------------------------------------------------------------


def _module_name_for_path(path: Path) -> str:
    """``.../api/routes/admin.py`` -> ``modulo.api.routes.admin``."""
    parts = list(path.relative_to(SRC_DIR).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("modulo", *parts))


def _collect_module(tree: ast.Module, name: str) -> _ModuleInfo:
    """Index one module: top-level functions, imports, module-level str constants."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    imports: dict[str, str] = {}
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = alias.name
                else:
                    imports.setdefault(alias.name.split(".")[0], alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            for alias in node.names:
                if alias.name != "*":
                    imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None or not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    constants.setdefault(target.id, value.value)
    return _ModuleInfo(name=name, functions=functions, imports=imports, constants=constants)


def _parse_source(source: str, filename: str, module_name: str) -> _ModuleInfo:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:  # pragma: no cover - only a malformed caller source
        raise RuntimeError(f"cannot parse {filename}: {exc}") from exc
    return _collect_module(tree, module_name)


@functools.cache
def _load_from_disk(module_name: str) -> _ModuleInfo | None:
    """Parse ``module_name`` on demand; ``None`` when no such module exists."""
    parts = module_name.split(".")
    if parts[0] != "modulo":
        return None
    base = SRC_DIR.joinpath(*parts[1:])
    candidates = [base.with_suffix(".py"), base / "__init__.py"] if parts[1:] else [base / "__init__.py"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"cannot read {path}: {exc}") from exc
        return _parse_source(source, str(path), module_name)
    return None


def _load_module(module_name: str) -> _ModuleInfo | None:
    """Module index lookup: synthetic registration first, then the real tree."""
    registered = _SYNTHETIC.get(module_name)
    if registered is not None:
        return registered
    return _load_from_disk(module_name)


def _clear_caches() -> None:
    """Fresh analysis state for each scan (synthetic names are per-test)."""
    _SYNTHETIC.clear()
    _load_from_disk.cache_clear()
    _function_deletes_organisation.cache_clear()
    _reaches_org_delete.cache_clear()


# ---------------------------------------------------------------------------
# Symbol / call resolution
# ---------------------------------------------------------------------------


def _trailing_name(func: ast.expr) -> str | None:
    """Trailing identifier of a call target (``Depends(x).audited`` -> ``audited``)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _function_imports(fn: ast.FunctionDef | ast.AsyncFunctionDef, mi: _ModuleInfo) -> dict[str, str]:
    """Module imports plus the function's own (shadowing) local imports."""
    local: dict[str, str] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    local[alias.asname] = alias.name
                else:
                    local.setdefault(alias.name.split(".")[0], alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            for alias in node.names:
                if alias.name != "*":
                    local[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return {**mi.imports, **local}


def _resolve_name(name: str, mi: _ModuleInfo, imports: Mapping[str, str]) -> str | None:
    """Fully-qualified symbol a bare name refers to, when statically known."""
    resolved = imports.get(name)
    if resolved is not None:
        return resolved
    if name in mi.functions:
        return f"{mi.name}.{name}"
    return None


def _resolve_call(func: ast.expr, mi: _ModuleInfo, imports: Mapping[str, str]) -> str | None:
    """Fully-qualified symbol a call target names (``crud.org.delete_thing``)."""
    attrs: list[str] = []
    node: ast.expr = func
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    base = _resolve_name(node.id, mi, imports)
    if base is None:
        return None
    if not attrs:
        return base
    return ".".join((base, *reversed(attrs)))


@functools.cache
def _lookup_function(fq: str) -> tuple[str, str] | None:
    """Locate ``fq`` (longest module prefix first) -> ``(module_name, function)``."""
    parts = fq.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        function_name = ".".join(parts[split:])
        mi = _load_module(module_name)
        if mi is not None and function_name in mi.functions:
            return module_name, function_name
    return None


# ---------------------------------------------------------------------------
# Guard 1 — org hard-delete analysis
# ---------------------------------------------------------------------------


def _symbol_is_org_model(symbol: str) -> bool:
    return symbol.endswith(_ORG_MODEL_SUFFIX)


@functools.cache
def _returns_organisation(fq: str) -> bool:
    """True when ``fq``'s return annotation names the Organisation model."""
    located = _lookup_function(fq)
    if located is None:
        return False
    mi = _load_module(located[0])
    if mi is None:  # pragma: no cover - index inconsistency
        return False
    fn = mi.functions.get(located[1])
    if fn is None or fn.returns is None:
        return False
    return _ORG_MODEL_NAME in ast.unparse(fn.returns)


def _is_org_model_expr(node: ast.expr, mi: _ModuleInfo, imports: Mapping[str, str]) -> bool:
    """True when an expression IS (or names) the Organisation model class."""
    if isinstance(node, ast.Attribute):
        return node.attr == _ORG_MODEL_NAME
    if isinstance(node, ast.Name):
        symbol = _resolve_name(node.id, mi, imports)
        return bool(symbol) and _symbol_is_org_model(symbol)
    return False


def _rhs_is_org(value: ast.expr, org_names: set[str], mi: _ModuleInfo, imports: Mapping[str, str]) -> bool:
    """True when an assignment RHS yields an Organisation row (fixed-point step)."""
    for node in ast.walk(value):
        if isinstance(node, ast.Name):
            if node.id in org_names:
                return True
            symbol = _resolve_name(node.id, mi, imports)
            if symbol is None:
                continue
            if _symbol_is_org_model(symbol) or _returns_organisation(symbol):
                return True
        elif isinstance(node, ast.Attribute) and node.attr == _ORG_MODEL_NAME:
            return True
    return False


def _org_typed_names(fn: ast.FunctionDef | ast.AsyncFunctionDef, mi: _ModuleInfo) -> set[str]:
    """Names bound to Organisation rows inside ``fn`` (parameters + fixed point)."""
    imports = _function_imports(fn, mi)
    org_names: set[str] = set()
    for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
        if arg.annotation is not None and _ORG_MODEL_NAME in ast.unparse(arg.annotation):
            org_names.add(arg.arg)
    assignments: list[tuple[str, ast.expr]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            assignments.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assignments.append((node.target.id, node.value))
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            assignments.append((node.target.id, node.iter))
    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            if name in org_names:
                continue
            if _rhs_is_org(value, org_names, mi, imports):
                org_names.add(name)
                changed = True
    return org_names


def _fn_deletes_organisation(fn: ast.FunctionDef | ast.AsyncFunctionDef, mi: _ModuleInfo) -> bool:
    """True when ``fn`` itself executes a hard delete of an Organisation row."""
    org_names = _org_typed_names(fn, mi)
    if not org_names:
        return False
    imports = _function_imports(fn, mi)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        # ORM hard delete: session.delete(<org row>)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "delete":
            if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in org_names:
                return True
        # Core delete: delete(Organisation)
        elif _trailing_name(node.func) == "delete" and any(_is_org_model_expr(arg, mi, imports) for arg in node.args):
            return True
    return False


@functools.cache
def _function_deletes_organisation(fq: str) -> bool:
    """Org-delete knowledge for a resolved cross-module symbol."""
    located = _lookup_function(fq)
    if located is None:
        return False
    mi = _load_module(located[0])
    if mi is None:  # pragma: no cover - index inconsistency
        return False
    fn = mi.functions.get(located[1])
    if fn is None:  # pragma: no cover - index inconsistency
        return False
    return _fn_deletes_organisation(fn, mi)


@functools.cache
def _reaches_org_delete(module_name: str, function_name: str, depth: int) -> bool:
    """True when ``function_name`` (or anything it calls, within ``depth`` hops)
    hard-deletes an Organisation row."""
    mi = _load_module(module_name)
    if mi is None:
        return False
    fn = mi.functions.get(function_name)
    if fn is None:
        return False
    if _fn_deletes_organisation(fn, mi):
        return True
    if depth <= 0:
        return False
    imports = _function_imports(fn, mi)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        fq = _resolve_call(node.func, mi, imports)
        if fq is None:
            continue
        if _function_deletes_organisation(fq):
            return True
        located = _lookup_function(fq)
        if located is not None and _reaches_org_delete(located[0], located[1], depth - 1):
            return True
    return False


def _has_audited_annotation(fn: ast.FunctionDef | ast.AsyncFunctionDef, mi: _ModuleInfo) -> bool:
    """True when the handler's decorators or parameter defaults call ``audited(...)``."""
    exprs: list[ast.expr] = [*fn.decorator_list, *fn.args.defaults]
    exprs.extend(d for d in fn.args.kw_defaults if d is not None)
    return any(
        isinstance(node, ast.Call) and _trailing_name(node.func) in _AUDITED_CALL_NAMES
        for expr in exprs
        for node in ast.walk(expr)
    )


def _is_route_handler(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when a decorator registers the function as a FastAPI route."""
    return any(
        isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in _ROUTE_METHODS
        for dec in fn.decorator_list
    )


# ---------------------------------------------------------------------------
# Guard 2 — same-type double audit
# ---------------------------------------------------------------------------


def _coarse_event_expr(call: ast.Call) -> ast.expr | None:
    """Event type of an ``audited(...)`` call (positional first arg or kwarg)."""
    for kw in call.keywords:
        if kw.arg == "event_type":
            return kw.value
    return call.args[0] if call.args else None


def _append_event_expr(call: ast.Call) -> ast.expr | None:
    """Event type of an inline append — keyword-only on both append helpers."""
    for kw in call.keywords:
        if kw.arg == "event_type":
            return kw.value
    return None


def _resolve_string(value: ast.expr, known: Mapping[str, str]) -> str | None:
    """Resolve a literal / known-name string expression; ``None`` when dynamic.

    ``known`` maps names to STRINGS already (constants, call-env, resolved
    locals), so a name lookup is the only hop needed.
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        return known.get(value.id)
    return None


def _known_strings(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    env: Mapping[str, str],
    mi: _ModuleInfo,
) -> dict[str, str]:
    """String-valued names visible inside ``fn``: constants + call env + locals."""
    known: dict[str, str] = {**mi.constants, **env}
    assignments: list[tuple[str, ast.expr]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            assignments.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assignments.append((node.target.id, node.value))
    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            if name in known:
                continue
            resolved = _resolve_string(value, known)
            if resolved is not None:
                known[name] = resolved
                changed = True
    return known


def _audited_event_types(fn: ast.FunctionDef | ast.AsyncFunctionDef, mi: _ModuleInfo) -> set[str]:
    """Coarse event types declared on the handler's audit dependency."""
    exprs: list[ast.expr] = [*fn.decorator_list, *fn.args.defaults]
    exprs.extend(d for d in fn.args.kw_defaults if d is not None)
    found: set[str] = set()
    for expr in exprs:
        for node in ast.walk(expr):
            if not (isinstance(node, ast.Call) and _trailing_name(node.func) in _AUDITED_CALL_NAMES):
                continue
            event_expr = _coarse_event_expr(node)
            if event_expr is None:
                continue
            text = _resolve_string(event_expr, mi.constants)
            if text is not None:
                found.add(text)
    return found


def _is_inline_append(fq: str | None, trailing: str | None) -> bool:
    """True when a call writes to the org-scoped audit chain."""
    if fq is not None:
        return fq.rsplit(".", 1)[-1] in _INLINE_APPEND_NAMES
    return trailing in _INLINE_APPEND_NAMES if trailing is not None else False


def _forwarding_params(fn: ast.FunctionDef | ast.AsyncFunctionDef, mi: _ModuleInfo) -> set[str]:
    """Parameters of ``fn`` whose value flows into an inline append's ``event_type``."""
    params = {arg.arg for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
    imports = _function_imports(fn, mi)
    forwarded: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not _is_inline_append(_resolve_call(node.func, mi, imports), _trailing_name(node.func)):
            continue
        event_expr = _append_event_expr(node)
        if isinstance(event_expr, ast.Name) and event_expr.id in params:
            forwarded.add(event_expr.id)
    return forwarded


def _call_argument(call: ast.Call, fn: ast.FunctionDef | ast.AsyncFunctionDef, param: str) -> ast.expr | None:
    """Argument a call site passes for ``param`` (keyword, else positional slot)."""
    for kw in call.keywords:
        if kw.arg == param:
            return kw.value
    positional = [*fn.args.posonlyargs, *fn.args.args]
    for index, arg in enumerate(positional):
        if arg.arg == param:
            return call.args[index] if index < len(call.args) else None
    return None


def _call_environment(
    call: ast.Call,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    known: Mapping[str, str],
) -> dict[str, str]:
    """Eagerly-resolved parameter values for entering a same-module callee."""
    env: dict[str, str] = {}
    positional = [*fn.args.posonlyargs, *fn.args.args]
    for index, arg in enumerate(positional):
        if index < len(call.args):
            resolved = _resolve_string(call.args[index], known)
            if resolved is not None:
                env[arg.arg] = resolved
    for kw in call.keywords:
        if kw.arg is None:
            continue
        resolved = _resolve_string(kw.value, known)
        if resolved is not None:
            env[kw.arg] = resolved
    return env


def _same_module_callee(fq: str | None, mi: _ModuleInfo) -> str | None:
    """Name of a same-module function a resolved call target refers to."""
    if fq is None:
        return None
    prefix = f"{mi.name}."
    if fq.startswith(prefix):
        callee = fq[len(prefix) :]
        if callee in mi.functions:
            return callee
    return None


def _emitted_event_types(mi: _ModuleInfo, start: str) -> set[str]:
    """Event types that reach an inline append from ``start`` (same module, depth ≤ 4)."""
    emitted: set[str] = set()
    visited: set[str] = set()
    queue: list[tuple[str, dict[str, str], int]] = [(start, {}, 0)]
    while queue:
        name, env, depth = queue.pop()
        if name in visited:
            continue
        visited.add(name)
        fn = mi.functions.get(name)
        if fn is None:
            continue
        imports = _function_imports(fn, mi)
        known = _known_strings(fn, env, mi)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            fq = _resolve_call(node.func, mi, imports)
            trailing = _trailing_name(node.func)
            if _is_inline_append(fq, trailing):
                event_expr = _append_event_expr(node)
                if event_expr is not None:
                    text = _resolve_string(event_expr, known)
                    if text is not None:
                        emitted.add(text)
                continue
            callee = _same_module_callee(fq, mi)
            if callee is None:
                continue
            callee_fn = mi.functions[callee]
            # Extract this call site's forwarded event types even when the
            # callee was already walked: each call site passes its own value.
            for param in _forwarding_params(callee_fn, mi):
                argument = _call_argument(node, callee_fn, param)
                if argument is None:
                    continue
                text = _resolve_string(argument, known)
                if text is not None:
                    emitted.add(text)
            if callee not in visited and depth < _MAX_CALL_DEPTH:
                queue.append((callee, _call_environment(node, callee_fn, known), depth + 1))
    return emitted


# ---------------------------------------------------------------------------
# Public scan API (used by the tree tests AND the teeth tests)
# ---------------------------------------------------------------------------


def org_self_delete_violations(source: str, filename: str, module_name: str | None = None) -> set[str]:
    """Guard 1 over one module's source: handlers that delete an org AND carry ``audited()``."""
    _clear_caches()
    name = module_name or f"synthetic.{Path(filename).stem}"
    mi = _parse_source(source, filename, name)
    _SYNTHETIC[name] = mi
    return _org_self_delete_in_module(mi, Path(filename).name)


def double_audit_violations(source: str, filename: str, module_name: str | None = None) -> set[str]:
    """Guard 2 over one module's source: handlers where coarse type == inline type."""
    _clear_caches()
    name = module_name or f"synthetic.{Path(filename).stem}"
    mi = _parse_source(source, filename, name)
    _SYNTHETIC[name] = mi
    return _double_audit_in_module(mi, Path(filename).name)


def _route_modules() -> list[Path]:
    return sorted(ROUTES_DIR.glob("*.py"))


def _org_self_delete_in_module(mi: _ModuleInfo, file_label: str) -> set[str]:
    violations: set[str] = set()
    for name, fn in mi.functions.items():
        if not _is_route_handler(fn):
            continue
        if not _has_audited_annotation(fn, mi):
            continue
        if _reaches_org_delete(mi.name, name, _MAX_CALL_DEPTH):
            violations.add(f"{file_label}:{name}")
    return violations


def _double_audit_in_module(mi: _ModuleInfo, file_label: str) -> set[str]:
    violations: set[str] = set()
    for name, fn in mi.functions.items():
        if not _is_route_handler(fn):
            continue
        coarse = _audited_event_types(fn, mi)
        if not coarse:
            continue
        emitted = _emitted_event_types(mi, name)
        for event_type in sorted(coarse & emitted):
            violations.add(f"{file_label}:{name}:{event_type}")
    return violations


def scan_org_self_delete_tree() -> set[str]:
    """Guard 1 across every REST route module."""
    _clear_caches()
    violations: set[str] = set()
    for path in _route_modules():
        mi = _load_module(_module_name_for_path(path))
        if mi is None:  # pragma: no cover - a route module that does not resolve
            raise RuntimeError(f"route module did not index: {path}")
        violations |= _org_self_delete_in_module(mi, path.name)
    return violations


def scan_double_audit_tree() -> set[str]:
    """Guard 2 across every REST route module."""
    _clear_caches()
    violations: set[str] = set()
    for path in _route_modules():
        mi = _load_module(_module_name_for_path(path))
        if mi is None:  # pragma: no cover - a route module that does not resolve
            raise RuntimeError(f"route module did not index: {path}")
        violations |= _double_audit_in_module(mi, path.name)
    return violations


def count_route_handlers() -> int:
    """Route handlers in the tree (vacuity guard for the scans above)."""
    total = 0
    for path in _route_modules():
        mi = _load_from_disk(_module_name_for_path(path))
        if mi is None:  # pragma: no cover - a route module that does not resolve
            raise RuntimeError(f"route module did not index: {path}")
        total += sum(1 for fn in mi.functions.values() if _is_route_handler(fn))
    return total


def coarse_rename_landed() -> bool:
    """True once the ``api_access_<verb>`` coarse-namespace rename (PR #1339) is in the tree."""
    return any(_COARSE_RENAME_PREFIX in path.read_text(encoding="utf-8") for path in _route_modules())


# ---------------------------------------------------------------------------
# Pre-rename collisions (PR #1339 supersedes these; see module docstring)
# ---------------------------------------------------------------------------

#: Same-type collisions that pre-date the ``api_access_<verb>`` rename. While the
#: rename has NOT landed (``coarse_rename_landed()`` is False) these are the
#: known, unchanged baseline: the guard fails on anything BEYOND this set. Once
#: the rename lands the latch turns strict (empty set) and this tuple simply
#: stops matching — it can then be deleted in a hygiene pass.
#: Computed by ``scan_double_audit_tree()`` on main before PR #1339 (25 handlers).
_PRE_RENAME_COLLISIONS: frozenset[str] = frozenset(
    {
        "admin.py:admin_create_user:user_created_by_admin",
        "admin.py:admin_deactivate_user:user_deactivated",
        "admin.py:admin_delete_team:team_deleted",
        "admin.py:admin_invite_user:invite_created",
        "admin.py:admin_manual_purge:run_purge",
        "admin.py:admin_reactivate_user:user_reactivated",
        "admin.py:admin_reset_password:user_password_reset_by_admin",
        "admin.py:admin_revoke_invitation:invite_revoked",
        "admin.py:request_org_deletion:org_deletion_requested",
        "admin_feature_flags.py:set_org_flag_override:feature_flag_override_set",
        "admin_feature_flags.py:toggle_feature_flag:feature_flag_override_set",
        "admin_orgs.py:admin_set_org_guardrails_kill_switch:guardrails_kill_switch",
        "admin_orgs.py:admin_set_org_triggers_paused:triggers_paused",
        "admin_rotation.py:rotate_key:fernet_key_rotation_started",
        "admin_run_retention.py:purge:run_retention_purge",
        "cost_components.py:create_component:cost_component_created",
        "cost_components.py:delete_component:cost_component_deleted",
        "cost_components.py:update_component:cost_component_updated",
        "me.py:change_password:password_changed",
        "teams.py:add_member_endpoint:team_member_added",
        "teams.py:change_member_role_endpoint:team_member_role_changed",
        "teams.py:create_team_endpoint:team_created",
        "teams.py:delete_team_endpoint:team_deleted",
        "teams.py:remove_member_endpoint:team_member_removed",
        "teams.py:update_team_endpoint:team_updated",
    }
)


# ---------------------------------------------------------------------------
# Teeth: each guard must FAIL on a synthetic violation and PASS when clean
# ---------------------------------------------------------------------------

_ORG_DELETE_VIOLATION = """\
from fastapi import APIRouter, Depends

from modulo.auth.dependencies import get_current_tenant_user
from modulo.core.audit_coverage import audited
from modulo.db.crud.organisation import delete_organisation
from modulo.db.session import get_db_session

router = APIRouter()


@router.delete(
    "/org",
    dependencies=[Depends(audited("organisation_deleted", "organisation", principal_dep=get_current_tenant_user))],
)
async def delete_own_organisation(session=get_db_session) -> None:
    await delete_organisation(session, org_id=session.scalar_one())
"""

_ORG_DELETE_CLEAN = """\
from fastapi import APIRouter, Depends

from modulo.core.system_audit_logger import append_system_audit_event
from modulo.db.crud.organisation import delete_organisation
from modulo.db.session import get_db_session

router = APIRouter()


@router.delete("/org")
async def delete_own_organisation(session=get_db_session) -> None:
    await append_system_audit_event(session, event_type="org_deletion_completed", org_id=None)
    await delete_organisation(session, org_id=session.scalar_one())
"""

_ORG_DELETE_BY_SHAPE_NOT_NAME = """\
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.dependencies import get_current_tenant_user
from modulo.core.audit_coverage import audited
from modulo.db.models.organisation import Organisation

router = APIRouter()


async def _retire_tenant(session: AsyncSession, row_id: object) -> None:
    result = await session.execute(select(Organisation).where(Organisation.id == row_id))
    row = result.scalar_one_or_none()
    if row is None:
        return
    await session.delete(row)


@router.post(
    "/tenants/retire",
    dependencies=[Depends(audited("tenant_retired", "organisation", principal_dep=get_current_tenant_user))],
)
async def retire_tenant_endpoint(session: AsyncSession) -> None:
    await _retire_tenant(session, row_id=1)
"""

_DOUBLE_AUDIT_VIOLATION = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event

router = APIRouter()


@router.post(
    "/widgets",
    dependencies=[Depends(audited("widget_created", "widget", principal_dep=get_current_tenant_user))],
)
async def create_widget(session) -> None:
    await append_audit_event(session, event_type="widget_created", resource_type="widget")
"""

_DOUBLE_AUDIT_VIA_WRAPPER = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event

router = APIRouter()


async def _emit(session, *, event_type: str) -> None:
    await append_audit_event(session, event_type=event_type, resource_type="widget")


@router.post(
    "/widgets",
    dependencies=[Depends(audited("widget_created", "widget", principal_dep=get_current_tenant_user))],
)
async def create_widget(session) -> None:
    await _emit(session, event_type="widget_created")
"""

_DOUBLE_AUDIT_VIA_MODULE_CONSTANT = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event

_COARSE_TYPE = "widget_created"

router = APIRouter()


@router.post(
    "/widgets",
    dependencies=[Depends(audited(_COARSE_TYPE, "widget", principal_dep=get_current_tenant_user))],
)
async def create_widget(session) -> None:
    await append_audit_event(session, event_type=_COARSE_TYPE, resource_type="widget")
"""

_DOUBLE_AUDIT_CLEAN = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event

router = APIRouter()


@router.post(
    "/widgets",
    dependencies=[Depends(audited("api_access_post", "widget", principal_dep=get_current_tenant_user))],
)
async def create_widget(session) -> None:
    await append_audit_event(session, event_type="widget_created", resource_type="widget")
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_tree_is_present():
    """Vacuity guard: a scan that silently finds nothing must not pass."""
    route_files = _route_modules()
    assert len(route_files) >= 80, f"route tree looks empty: {len(route_files)} file(s) in {ROUTES_DIR}"
    assert count_route_handlers() >= 300, "scan found too few route handlers - it is not reading the tree"


def test_no_route_handler_deletes_an_organisation_and_carries_audited():
    """Guard 1: org hard-delete + audited() = a post-commit append that can never land."""
    violations = scan_org_self_delete_tree()
    assert not violations, (
        f"{len(violations)} route handler(s) hard-delete an Organisation AND carry audited(...), "
        "so the post-commit append can never satisfy audit_events.organisation_id's FK (FAR-1517). "
        "Drop audited(...) from the handler and write the record IN-transaction with "
        "system_audit_logger.append_system_audit_event instead:\n  " + "\n  ".join(sorted(violations))
    )


def test_no_route_double_audits_the_same_event_type():
    """Guard 2: coarse audited() type must not equal an inline append's type."""
    violations = scan_double_audit_tree()
    expected = set() if coarse_rename_landed() else set(_PRE_RENAME_COLLISIONS)
    unexpected = violations - expected
    assert not unexpected, (
        f"{len(unexpected)} handler(s) append the SAME event type twice per action - the route's "
        "audited(...) dependency and its own inline append_audit_event(...). Give the coarse event "
        "the secondary api_access_<verb> namespace and leave the rich inline event alone:\n  "
        + "\n  ".join(sorted(unexpected))
    )


def test_pre_rename_baseline_is_not_needed_once_the_rename_landed():
    """Documented hand-off: the known-collision list exists only pre-#1339.

    Not a failure on either side — it records which state the tree is in, so a
    reader of this file never has to guess whether the latch below is armed.
    """
    if coarse_rename_landed():
        assert not _PRE_RENAME_COLLISIONS, "PR #1339 landed: delete the now-dead pre-rename baseline"
    else:
        assert isinstance(_PRE_RENAME_COLLISIONS, frozenset)


def test_guard_1_flags_a_synthetic_org_self_delete_with_audited():
    """Teeth: the real detection path fails on a violating source."""
    violations = org_self_delete_violations(_ORG_DELETE_VIOLATION, "synthetic_org_delete.py")
    assert violations, "guard 1 missed a handler that hard-deletes an org while carrying audited()"
    assert "synthetic_org_delete.py:delete_own_organisation" in violations


def test_guard_1_passes_the_same_handler_without_audited():
    """Teeth (clean half): same org delete, no audited() -> no violation."""
    violations = org_self_delete_violations(_ORG_DELETE_CLEAN, "synthetic_org_delete_clean.py")
    assert not violations, f"guard 1 false positive on the FAR-1517 pattern: {sorted(violations)}"


def test_guard_1_detects_org_deletion_by_shape_not_by_name():
    """Teeth: a helper named ``_retire_tenant`` (no 'org' in the name) is caught
    because the guard follows the actual Organisation delete, not identifiers."""
    violations = org_self_delete_violations(_ORG_DELETE_BY_SHAPE_NOT_NAME, "synthetic_retire.py")
    assert violations, "guard 1 missed an org delete reached through a neutrally-named helper"
    assert "synthetic_retire.py:retire_tenant_endpoint" in violations


def test_guard_2_flags_a_same_type_double_audit():
    """Teeth: audited type == inline append type is a violation."""
    violations = double_audit_violations(_DOUBLE_AUDIT_VIOLATION, "synthetic_double.py")
    assert violations == {"synthetic_double.py:create_widget:widget_created"}


def test_guard_2_reaches_the_inline_append_through_a_same_module_wrapper():
    """Teeth: the emission lives in a helper the handler calls, not in its body."""
    violations = double_audit_violations(_DOUBLE_AUDIT_VIA_WRAPPER, "synthetic_wrapper.py")
    assert violations == {"synthetic_wrapper.py:create_widget:widget_created"}


def test_guard_2_resolves_a_module_level_string_constant():
    """Teeth: both sides use a constant, not an inline literal."""
    violations = double_audit_violations(_DOUBLE_AUDIT_VIA_MODULE_CONSTANT, "synthetic_constant.py")
    assert violations == {"synthetic_constant.py:create_widget:widget_created"}


def test_guard_2_allows_a_distinct_coarse_and_rich_event_type():
    """Teeth (clean half): api_access_post coarse + rich widget_created is the fix."""
    violations = double_audit_violations(_DOUBLE_AUDIT_CLEAN, "synthetic_double_clean.py")
    assert not violations, f"guard 2 false positive on the PR #1339 pattern: {sorted(violations)}"
