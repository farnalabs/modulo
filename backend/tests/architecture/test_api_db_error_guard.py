"""Architecture test: every route-local ``except SQLAlchemyError`` arm that
reports a 503 — or reports the MCP database-unavailable payload — must lead
with the shared session-contract guard.

FAR-1464. A local ``except SQLAlchemyError`` arm never reaches
``handle_db_errors``, so before this sweep a client-side session-contract
violation (``InvalidRequestError`` — e.g. a query outside the transaction on
the ``autobegin=False`` DI session) was reported as ``503 "Database temporarily
unavailable."`` with ``reason=db_transient`` — a retry-inviting outage reply
for a non-retryable programming bug. The fix is a guard as the FIRST statement
of every arm that answers 503:

    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "<module>.<function>")
        ... existing logging / 503 raise ...

This test AST-scans ``backend/src/modulo/api/**`` and FAILS when an arm that
reports a 503 does not lead with that guard, so the misclassification cannot
reappear. Arms legitimately exempt are listed in ``_EXEMPT`` with a reason.

Scope notes:

* The predicate is "reports a 503": raises ``HTTPException(503)`` (directly or
  via a helper that does), RETURNS a starlette/fastapi ``Response`` with
  ``status_code=503``, or logs ``log_service_unavailable("db_transient", ...)``.
  Arms that merely swallow, re-raise, or answer 4xx/500 are out of scope —
  they cannot misreport an outage as a 503.
* FAR-1482 adds the SECOND predicate: "returns the MCP database-unavailable
  payload". MCP tool results carry no status code — the payload IS the
  response — so the same misclassification appears as a returned
  ``{"error": "database_unavailable"}`` / ``_tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)``
  dict rather than a 503. That shape would otherwise be invisible to a
  503-only scanner, so it gets its own predicate and its own violation text.
* Two guard shapes exist, one per surface, and both are recognised as
  "leads with the guard" (see ``_leads_with_guard``): the REST/HTTP form
  ``raise_session_contract_error(exc, key)`` as a bare statement, and the MCP
  payload form ``if (err := _tool_session_contract_error(exc, key)) is not
  None: return err`` — where the call sits in the ``if`` test so it evaluates
  before any database-unavailable reply. Both delegate to the SAME shared
  classifier in ``db_error_handling``; only the rendering differs (500 vs
  payload), which is why accepting either satisfies both predicates.
* Helper resolution is CROSS-MODULE within the scanned ``modulo.api`` tree
  (iteration-2 fix): a helper reached via ``from modulo.api.<...> import <name>``
  (incl. ``as`` aliases, relative imports, ``import modulo.api.<...>`` usage and
  ``import *``) is resolved to its definition in the source module and the
  503-predicate is computed as a fixed point over the whole import graph —
  so an arm whose 503 comes from an imported helper is flagged exactly like a
  module-local one. Helpers defined OUTSIDE the scanned tree (``modulo.core``,
  ``modulo.db``, third-party) are unresolvable and are NOT treated as 503 —
  documented boundary, not a silent gap; no current arm delegates its 503 to
  a non-``modulo.api`` helper.
* ``except PendingRollbackError`` arms are NOT scanned: that type is a strict
  SUBclass of ``InvalidRequestError``, so such an arm can only ever see
  PendingRollbackError instances — a plain session-contract violation never
  reaches it, and no guard can apply. The same reasoning excludes
  ``except IntegrityError`` arms that answer ``database_unavailable``: an
  InvalidRequestError can never be caught there.
* ``MissingGreenlet`` is a subclass of ``InvalidRequestError``; the guard
  delegates both to the shared classifier, which maps them to 500.

The cross-module resolution is proven by the synthetic-tree tests at the
bottom of this module: a 503-raising helper in one file, an UNGUARDED arm in
another file delegating to it (both the ``from ... import`` and the
``import ... as`` shapes) is flagged; the guarded shape and a module-local
control behave the same way the real tree does. The MCP payload predicate has
its own planted-violation / guarded-pair proof for the same reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "modulo" / "api"
_API_PREFIX = "modulo.api"

#: Files whose SQLAlchemyError arms are allowed to report a 503 WITHOUT the
#: guard, mapped to the reason the exemption is legitimate (FAR-1464).
_EXEMPT = {
    "db_error_handling.py": (
        "The shared classifier itself: _translate_wrapped_exception's SQLAlchemyError "
        "backstop IS the canonical 503 mapping every other arm's guard delegates to — "
        "guarding it would be self-referential."
    ),
}

#: REST/HTTP rendering of the shared classifier: raises 500, never returns.
_HTTP_GUARD_NAME = "raise_session_contract_error"
#: MCP payload rendering of the SAME classifier: returns the payload, else None.
_MCP_GUARD_NAME = "_tool_session_contract_error"
_GUARD_NAMES = frozenset({_HTTP_GUARD_NAME, _MCP_GUARD_NAME})

#: MCP tool-error shapes the same misclassification takes when there is no
#: status code to carry it (FAR-1482).
_MCP_DB_UNAVAILABLE_CODE = "database_unavailable"
_MCP_DB_UNAVAILABLE_MSG_ARG = "_MSG_DB_TEMPORARILY_UNAVAILABLE"

_RESPONSE_CLASSES = frozenset({"Response", "JSONResponse", "PlainTextResponse", "HTMLResponse", "RedirectResponse"})
_HTTP_503_MARKERS = frozenset({"503", "HTTP_503_SERVICE_UNAVAILABLE"})
_MAX_BINDING_CHASE = 10


def _name_of(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_503_status(value: ast.expr | None) -> bool:
    """True for the literal 503 and status.HTTP_503_SERVICE_UNAVAILABLE."""
    if isinstance(value, ast.Constant) and str(value.value) in _HTTP_503_MARKERS:
        return True
    return isinstance(value, ast.Attribute) and value.attr in _HTTP_503_MARKERS


def _raises_503(node: ast.Raise) -> bool:
    """``raise HTTPException(status_code=503, ...)``."""
    if not isinstance(node.exc, ast.Call) or _name_of(node.exc.func) != "HTTPException":
        return False
    return any(kw.arg == "status_code" and _is_503_status(kw.value) for kw in node.exc.keywords)


def _returns_503_response(node: ast.Return) -> bool:
    """``return JSONResponse(status_code=503, ...)`` (also inside tuples)."""
    candidates: list[ast.expr] = []
    value = node.value
    if isinstance(value, ast.Tuple):
        candidates.extend(value.elts)
    elif value is not None:
        candidates.append(value)
    for expr in candidates:
        if not isinstance(expr, ast.Call) or _name_of(expr.func) not in _RESPONSE_CLASSES:
            continue
        if any(kw.arg == "status_code" and _is_503_status(kw.value) for kw in expr.keywords):
            return True
    return False


def _logs_db_transient(node: ast.Call) -> bool:
    if _name_of(node.func) != "log_service_unavailable" or not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and first.value == "db_transient"


def _returns_mcp_db_unavailable(node: ast.Return) -> bool:
    """``return {"error": "database_unavailable", ...}`` (FAR-1482).

    Also matches ``return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)``, whose
    code is already ``internal_error`` but whose DETAIL still claims a database
    outage — the same misreport in a different wrapper.
    """
    value = node.value
    if isinstance(value, ast.Call) and _name_of(value.func) == "_tool_error":
        return bool(value.args) and _name_of(value.args[0]) == _MCP_DB_UNAVAILABLE_MSG_ARG
    if isinstance(value, ast.Dict):
        for key, item in zip(value.keys, value.values, strict=False):
            if (
                isinstance(key, ast.Constant)
                and key.value == "error"
                and isinstance(item, ast.Constant)
                and item.value == _MCP_DB_UNAVAILABLE_CODE
            ):
                return True
    return False


def _body_returns_mcp_db_unavailable(body: list[ast.stmt]) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Return) and _returns_mcp_db_unavailable(node):
                return True
    return False


def _dotted_parts(node: ast.expr | None) -> list[str] | None:
    """``a.b.c`` -> ``["a", "b", "c"]``; None for non-dotted expressions."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        base = _dotted_parts(node.value)
        return None if base is None else [*base, node.attr]
    return None


def _body_shape(body: list[ast.stmt]) -> tuple[bool, list[ast.expr]]:
    """ONE walk of a statement list: (directly reports a 503, call func exprs).

    "Directly" = raises ``HTTPException(503)``, returns a 503 ``Response``, or
    logs ``log_service_unavailable("db_transient", ...)``. The call funcs are
    collected so helper-mediated 503s can be resolved against the import-graph
    fixed point WITHOUT re-walking the AST every iteration (iteration-2
    perf: the naive re-walk made this test take ~63s on the real tree).
    """
    direct = False
    call_funcs: list[ast.expr] = []
    for stmt in body:
        for sub in ast.walk(stmt):
            if (isinstance(sub, ast.Raise) and _raises_503(sub)) or (
                isinstance(sub, ast.Return) and _returns_503_response(sub)
            ):
                direct = True
            elif isinstance(sub, ast.Call):
                call_funcs.append(sub.func)
                if _logs_db_transient(sub):
                    direct = True
    return direct, call_funcs


def _module_of_rel(rel: str) -> str:
    """Map a file path relative to the api root to its ``modulo.api...`` module name."""
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
        return ".".join([_API_PREFIX, *parts]) if parts else _API_PREFIX
    parts[-1] = parts[-1][: -len(".py")]
    return ".".join([_API_PREFIX, *parts])


def _resolve_from_module(current_module: str, node: ast.ImportFrom) -> str | None:
    """Absolute target module of a ``from ... import ...`` (level-aware)."""
    if node.level:
        package = current_module.split(".")[:-1]
        keep = len(package) - (node.level - 1)
        if keep <= 0:
            return None
        base = ".".join(package[:keep])
        return f"{base}.{node.module}" if node.module else base
    return node.module


class _ApiIndex:
    """Parsed snapshot of the scanned tree plus its import graph."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.trees: dict[str, ast.Module] = {}
        self.rels: dict[str, str] = {}
        self.defs: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        self.bindings: dict[str, dict[str, tuple[str, str]]] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        self.stars: dict[str, list[str]] = {}
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            module = _module_of_rel(rel)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            self.trees[module] = tree
            self.rels[module] = rel
            self.defs.setdefault(module, {})
            self.bindings.setdefault(module, {})
            self.aliases.setdefault(module, {})
            self.stars.setdefault(module, [])
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.defs[module][node.name] = node
                elif isinstance(node, ast.ImportFrom):
                    target = _resolve_from_module(module, node)
                    if target is None:
                        continue
                    for alias in node.names:
                        if alias.name == "*":
                            self.stars[module].append(target)
                        else:
                            self.bindings[module][alias.asname or alias.name] = (target, alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(_API_PREFIX):
                            # import modulo.api.routes.admin [as adm]
                            self.aliases[module][alias.asname or alias.name.split(".")[0]] = alias.name


def _resolve_def_key(index: _ApiIndex, module: str, func: ast.expr) -> tuple[str, str] | None:
    """Map a call's func expression to the (module, name) of its definition.

    Resolves: local top-level defs; ``from ... import`` bindings (chained
    re-exports, capped); ``import modulo.api.<...>`` aliases; dotted paths that
    are themselves a known module; and ``import *`` targets. Returns None for
    anything outside the scanned tree — unresolvable helpers are NOT treated
    as 503 (documented boundary in the module docstring).
    """
    parts = _dotted_parts(func)
    if not parts:
        return None
    if len(parts) == 1:
        name = parts[0]
        if name in index.defs.get(module, {}):
            return (module, name)
        seen: set[tuple[str, str]] = set()
        cur_mod, cur_name = module, name
        for _ in range(_MAX_BINDING_CHASE):
            binding = index.bindings.get(cur_mod, {}).get(cur_name)
            if binding is None:
                break
            if binding in seen:
                return None
            seen.add(binding)
            cur_mod, cur_name = binding
            if cur_name in index.defs.get(cur_mod, {}):
                return (cur_mod, cur_name)
        for star_target in index.stars.get(module, []):
            if name in index.defs.get(star_target, {}):
                return (star_target, name)
        return None
    # Dotted: longest known-module prefix, then a def name in that module.
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in index.rels:
            if parts[i] in index.defs.get(candidate, {}):
                return (candidate, parts[i])
            return None
    head = parts[0]
    rest = parts[1:]
    base: str | None = index.aliases.get(module, {}).get(head)
    if base is None:
        binding = index.bindings.get(module, {}).get(head)
        if binding is not None:
            # ``from x import mod`` then ``mod.def(...)``: binding names a module
            # when module+name maps to a file in the tree.
            as_module = f"{binding[0]}.{binding[1]}"
            if as_module in index.rels:
                base = as_module
    if base is None or not rest:
        return None
    sub = ".".join(rest[:-1])
    target_module = f"{base}.{sub}" if sub else base
    if target_module in index.rels and rest[-1] in index.defs.get(target_module, {}):
        return (target_module, rest[-1])
    return None


def _compute_503_states(index: _ApiIndex) -> dict[tuple[str, str], bool]:
    """Fixed point: which (module, def) definitions report a 503?

    Monotone from "directly reports 503": a def reports a 503 when its body
    directly does, or calls a helper that already resolves True — through
    local defs OR imported names, across module boundaries, however deep the
    chain. Each def's body is walked ONCE and each call's def-key resolved
    ONCE; the iteration only re-evaluates cached keys against the flipping
    state (the naive re-walk made this test take ~63s on the real tree).
    """
    shapes: dict[tuple[str, str], tuple[bool, list[tuple[str, str]]]] = {}
    for m, defs in index.defs.items():
        for name, fdef in defs.items():
            direct, call_funcs = _body_shape(fdef.body)
            resolved = [key for f in call_funcs if (key := _resolve_def_key(index, m, f)) is not None]
            shapes[(m, name)] = (direct, resolved)
    state = {key: direct for key, (direct, _resolved) in shapes.items()}
    changed = True
    while changed:
        changed = False
        for key, (direct, resolved) in shapes.items():
            if state[key]:
                continue
            if direct or any(state.get(dep, False) for dep in resolved):
                state[key] = True
                changed = True
    return state


def _except_type_names(handler: ast.ExceptHandler) -> list[str]:
    t = handler.type
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Attribute):
        return [t.attr]
    if isinstance(t, ast.Tuple):
        names: list[str] = []
        for elt in t.elts:
            name = _name_of(elt)
            if name is not None:
                names.append(name)
        return names
    return []


def _call_is_guard(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and _name_of(n.func) in _GUARD_NAMES for n in ast.walk(node))


def _leads_with_guard(handler: ast.ExceptHandler) -> bool:
    """True when the arm's FIRST statement evaluates the shared classifier.

    Two shapes, one per surface (both delegate to ``db_error_handling``):

    * REST/HTTP — a bare call statement, e.g.
      ``raise_session_contract_error(exc, "routes.x.handler")``. It raises 500
      for a contract violation and never returns, so calling it first is enough.
    * MCP payload — the guard RETURNS the payload instead of raising, so the
      arm has to branch on it and hand the payload back (``if (err :=
      guard(exc, key)) is not None: return err``). The ``return`` inside the
      branch is required: without it the verdict is dropped and the arm still
      answers "database unavailable" — exactly the misclassification this test
      exists to catch.
    """
    first = handler.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call):
        return _name_of(first.value.func) in _GUARD_NAMES
    if isinstance(first, ast.If) and _call_is_guard(first.test):
        return any(isinstance(node, ast.Return) for node in ast.walk(first))
    return False


def _iter_violations(api_root: Path = _API_ROOT, exempt: dict[str, str] | None = None) -> list[str]:
    exempt = _EXEMPT if exempt is None else exempt
    index = _ApiIndex(api_root)
    state = _compute_503_states(index)
    violations: list[str] = []
    for module, rel in sorted(index.rels.items(), key=lambda item: item[1]):
        if rel in exempt:
            continue
        tree = index.trees[module]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Try, ast.TryStar)):
                continue
            for handler in node.handlers:
                if "SQLAlchemyError" not in _except_type_names(handler):
                    continue
                direct, call_funcs = _body_shape(handler.body)
                reports_503 = direct or any(
                    (key := _resolve_def_key(index, module, func)) is not None and state.get(key, False)
                    for func in call_funcs
                )
                reports_mcp_payload = _body_returns_mcp_db_unavailable(handler.body)
                if not reports_503 and not reports_mcp_payload:
                    continue
                if _leads_with_guard(handler):
                    continue
                if reports_mcp_payload:
                    violations.append(
                        f"  {rel}:{handler.lineno} — except SQLAlchemyError returns an MCP "
                        f"database-unavailable payload without a leading session-contract guard"
                    )
                else:
                    violations.append(
                        f"  {rel}:{handler.lineno} — except SQLAlchemyError reports a 503 "
                        f"without a leading {_HTTP_GUARD_NAME}(...) guard"
                    )
    return violations


def test_sqlalchemy_error_503_arms_lead_with_session_contract_guard() -> None:
    """FAR-1464/FAR-1482: no DB-outage-reporting arm may misclassify a session-contract error."""
    violations = _iter_violations()
    exempt_note = "".join(f"\n  EXEMPT {rel}: {reason}" for rel, reason in sorted(_EXEMPT.items()))
    assert not violations, (
        f"Found {len(violations)} except-SQLAlchemyError arm(s) that report a 503 (FAR-1464) or "
        "an MCP database-unavailable payload (FAR-1482) without a leading session-contract guard. "
        "Add the guard as the arm's FIRST statement — InvalidRequestError/MissingGreenlet must "
        "surface as a programming error, never as an outage:\n" + "\n".join(violations) + exempt_note
    )


def test_every_exemption_is_a_real_file() -> None:  # pragma: no cover - bookkeeping
    """Allowlist hygiene: an exemption for a file that no longer exists is stale."""
    missing = [rel for rel in _EXEMPT if not (_API_ROOT / rel).exists()]
    assert not missing, f"stale _EXEMPT entries (file gone): {missing}"


# ---------------------------------------------------------------------------
# Cross-module resolution proof (iteration-2 QA gate): a 503 that is raised in
# ONE file and delegated to from ANOTHER file must be flagged exactly like a
# module-local one. Synthetic two-file trees, unguarded vs guarded.
# ---------------------------------------------------------------------------

_CROSS_HELPER_SOURCE = """\
from fastapi import HTTPException, status


def boom() -> None:
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="down")
"""

_CROSS_ROUTE_FROM_IMPORT = """\
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.cross_helper_503 import boom


async def handler_from_import() -> None:
    try:
        await whatever()
    except SQLAlchemyError:
        boom()
"""

_CROSS_ROUTE_DOTTED_IMPORT = """\
import modulo.api.cross_helper_503 as helper_alias
from sqlalchemy.exc import SQLAlchemyError


async def handler_dotted_import() -> None:
    try:
        await whatever()
    except SQLAlchemyError:
        helper_alias.boom()
"""

_CROSS_ROUTE_GUARDED = """\
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.cross_helper_503 import boom


async def handler_from_import() -> None:
    try:
        await whatever()
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "routes.cross_route.handler_from_import")
        boom()
"""

_LOCAL_ROUTE = """\
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError


def local_boom() -> None:
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="down")


async def handler_local() -> None:
    try:
        await whatever()
    except SQLAlchemyError:
        local_boom()
"""


def _write_synthetic_tree(root: Path, route_sources: dict[str, str]) -> None:
    """Build a miniature modulo.api tree: helper file + route file(s)."""
    (root / "routes").mkdir(parents=True, exist_ok=True)
    (root / "cross_helper_503.py").write_text(_CROSS_HELPER_SOURCE, encoding="utf-8")
    for rel, source in route_sources.items():
        (root / rel).write_text(source, encoding="utf-8")


def test_cross_module_delegating_503_helper_is_flagged_when_unguarded(tmp_path: Path) -> None:
    """The iteration-2 hole: an arm delegating its 503 to an IMPORTED helper.

    Both import shapes the scanner must follow — ``from modulo.api.X import f``
    and ``import modulo.api.X as alias`` — must flag the unguarded arm in the
    importing file, even though the 503 raise lives in a different file.
    """
    _write_synthetic_tree(
        tmp_path,
        {
            "routes/cross_route.py": _CROSS_ROUTE_FROM_IMPORT + "\n\n" + _CROSS_ROUTE_DOTTED_IMPORT,
        },
    )
    violations = _iter_violations(tmp_path, exempt={})

    assert any("routes/cross_route.py" in v for v in violations), (
        f"cross-module-delegated 503 was NOT flagged (the QA-gate hole): {violations}"
    )
    flagged_lines = sum(1 for v in violations if "routes/cross_route.py" in v)
    assert flagged_lines == 2, f"both synthetic arms (from-import and dotted-import) must be flagged: {violations}"


def test_cross_module_delegating_503_helper_is_clean_when_guarded(tmp_path: Path) -> None:
    """The same synthetic shape with the guard first statement passes."""
    _write_synthetic_tree(tmp_path, {"routes/cross_route.py": _CROSS_ROUTE_GUARDED})
    violations = _iter_violations(tmp_path, exempt={})

    assert not violations, f"guarded cross-module arm must be clean: {violations}"


def test_module_local_delegating_503_helper_is_flagged_when_unguarded(tmp_path: Path) -> None:
    """Control: the module-local delegation shape keeps being flagged."""
    _write_synthetic_tree(tmp_path, {"routes/local_route.py": _LOCAL_ROUTE})
    violations = _iter_violations(tmp_path, exempt={})

    assert any("routes/local_route.py" in v for v in violations), (
        f"module-local delegation must stay flagged: {violations}"
    )


# ---------------------------------------------------------------------------
# MCP payload predicate (FAR-1482). MCP tool results carry no status code, so
# the same misclassification returns a database-unavailable payload instead of
# raising 503 — invisible to a 503-only scanner. Planted-violation proof for
# the payload predicate AND for the MCP guard shape it accepts.
# ---------------------------------------------------------------------------

_MCP_PAYLOAD_UNGUARDED = """\
from sqlalchemy.exc import SQLAlchemyError


async def handler() -> dict:
    try:
        await whatever()
    except SQLAlchemyError:
        return {"error": "database_unavailable", "detail": "Database operation failed. Please try again."}
"""

_MCP_PAYLOAD_GUARDED = """\
from sqlalchemy.exc import SQLAlchemyError


async def handler() -> dict:
    try:
        await whatever()
    except SQLAlchemyError as exc:
        if (err := _tool_session_contract_error(exc, "mcp.handler")) is not None:
            return err
        return {"error": "database_unavailable", "detail": "Database operation failed. Please try again."}
"""

_MCP_TEMPORARILY_UNGUARDED = """\
from sqlalchemy.exc import SQLAlchemyError


async def handler() -> dict:
    try:
        await whatever()
    except SQLAlchemyError:
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)
"""


def test_mcp_database_unavailable_payload_is_flagged_when_unguarded(tmp_path: Path) -> None:
    """Anti-vacuity for the FAR-1482 payload predicate: a planted violation fires."""
    _write_synthetic_tree(tmp_path, {"routes/mcp_payload.py": _MCP_PAYLOAD_UNGUARDED})
    violations = _iter_violations(tmp_path, exempt={})

    assert any("routes/mcp_payload.py" in v and "database-unavailable payload" in v for v in violations), (
        f"MCP database-unavailable payload without the guard was NOT flagged: {violations}"
    )


def test_mcp_temporarily_unavailable_payload_is_flagged_when_unguarded(tmp_path: Path) -> None:
    """The ``_tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)`` wrapper shape too."""
    _write_synthetic_tree(tmp_path, {"routes/mcp_payload.py": _MCP_TEMPORARILY_UNGUARDED})
    violations = _iter_violations(tmp_path, exempt={})

    assert any("routes/mcp_payload.py" in v and "database-unavailable payload" in v for v in violations), (
        f"_tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE) without the guard was NOT flagged: {violations}"
    )


def test_mcp_payload_guard_shape_is_accepted(tmp_path: Path) -> None:
    """The MCP ``if (err := guard(...)) is not None: return err`` shape passes.

    The guard RETURNS a payload rather than raising, so it cannot be a bare
    statement — proving the scanner recognises the payload rendering without
    weakening the REST/HTTP check.
    """
    _write_synthetic_tree(tmp_path, {"routes/mcp_payload.py": _MCP_PAYLOAD_GUARDED})
    violations = _iter_violations(tmp_path, exempt={})

    assert not violations, f"guarded MCP payload arm must be clean: {violations}"
