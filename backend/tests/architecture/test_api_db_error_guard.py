"""Architecture test: every route-local ``except SQLAlchemyError`` arm that
reports a 503 must lead with the shared ``raise_session_contract_error`` guard.

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
  via a module-local helper that does), RETURNS a starlette/fastapi ``Response``
  with ``status_code=503``, or logs ``log_service_unavailable("db_transient",
  ...)``. Arms that merely swallow, re-raise, or answer 4xx/500 are out of
  scope — they cannot misreport an outage as a 503.
* ``except PendingRollbackError`` arms are NOT scanned: that type is a strict
  SUBclass of ``InvalidRequestError``, so such an arm can only ever see
  PendingRollbackError instances — a plain session-contract violation never
  reaches it, and no guard can apply.
* ``MissingGreenlet`` is a subclass of ``InvalidRequestError``; the guard
  delegates both to the shared classifier, which maps them to 500.
"""

from __future__ import annotations

import ast
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "modulo" / "api"

#: Files whose SQLAlchemyError arms are allowed to report a 503 WITHOUT the
#: guard, mapped to the reason the exemption is legitimate (FAR-1464).
_EXEMPT = {
    "db_error_handling.py": (
        "The shared classifier itself: _translate_wrapped_exception's SQLAlchemyError "
        "backstop IS the canonical 503 mapping every other arm's guard delegates to — "
        "guarding it would be self-referential."
    ),
}

_GUARD_NAME = "raise_session_contract_error"
_RESPONSE_CLASSES = frozenset({"Response", "JSONResponse", "PlainTextResponse", "HTMLResponse", "RedirectResponse"})
_HTTP_503_MARKERS = frozenset({"503", "HTTP_503_SERVICE_UNAVAILABLE"})


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


def _body_reports_503(body: list[ast.stmt], module_helpers_503: set[str]) -> bool:
    """Does this statement list report a 503 to the client?

    Direct raise, return of a 503 Response, a structured ``db_transient``
    record, or a call to a module-local helper known to raise a 503.
    """
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise) and _raises_503(sub):
                return True
            if isinstance(sub, ast.Return) and _returns_503_response(sub):
                return True
            if isinstance(sub, ast.Call):
                fn = _name_of(sub.func)
                if fn in module_helpers_503:
                    return True
                if _logs_db_transient(sub):
                    return True
    return False


def _module_helpers_raising_503(tree: ast.Module) -> set[str]:
    """Top-level functions in this module whose body reports a 503.

    Route arms commonly delegate the 503 to a local
    ``_raise_db_unavailable(...)``-style helper; callers of those helpers are
    in scope exactly like direct raises.
    """
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
    flagged: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, fdef in defs.items():
            if name not in flagged and _body_reports_503(fdef.body, flagged):
                flagged.add(name)
                changed = True
    return flagged


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


def _leads_with_guard(handler: ast.ExceptHandler) -> bool:
    first = handler.body[0]
    return (
        isinstance(first, ast.Expr) and isinstance(first.value, ast.Call) and _name_of(first.value.func) == _GUARD_NAME
    )


def _iter_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(_API_ROOT.rglob("*.py")):
        rel = path.relative_to(_API_ROOT).as_posix()
        if rel in _EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - repo is valid py
            violations.append(f"  {rel}: cannot parse: {exc}")
            continue
        helpers_503 = _module_helpers_raising_503(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Try, ast.TryStar)):
                continue
            for handler in node.handlers:
                if "SQLAlchemyError" not in _except_type_names(handler):
                    continue
                if not _body_reports_503(handler.body, helpers_503):
                    continue
                if not _leads_with_guard(handler):
                    violations.append(
                        f"  {rel}:{handler.lineno} — except SQLAlchemyError reports a 503 "
                        f"without a leading {_GUARD_NAME}(...) guard"
                    )
    return violations


def test_sqlalchemy_error_503_arms_lead_with_session_contract_guard() -> None:
    """FAR-1464: no 503-reporting route arm may misclassify a session-contract error."""
    violations = _iter_violations()
    exempt_note = "".join(f"\n  EXEMPT {rel}: {reason}" for rel, reason in sorted(_EXEMPT.items()))
    assert not violations, (
        f"Found {len(violations)} except-SQLAlchemyError arm(s) that report a 503 "
        f"without a leading {_GUARD_NAME} guard (FAR-1464). Add the guard as the "
        "arm's first statement — InvalidRequestError/MissingGreenlet must surface "
        "as 500, not 503/db_transient:\n" + "\n".join(violations) + exempt_note
    )


def test_every_exemption_is_a_real_file() -> None:  # pragma: no cover - bookkeeping
    """Allowlist hygiene: an exemption for a file that no longer exists is stale."""
    missing = [rel for rel in _EXEMPT if not (_API_ROOT / rel).exists()]
    assert not missing, f"stale _EXEMPT entries (file gone): {missing}"
