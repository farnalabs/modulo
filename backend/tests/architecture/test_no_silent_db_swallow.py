"""Architecture test: no silent ``except SQLAlchemyError`` swallows in the API layer (FAR-1483).

A handler that neither logs the database failure nor raises/surfaces an error
turns an outage into whatever the handler returns - typically ``None`` or an
empty result, which every caller reads as "not found". The concrete defect this
guards: ``_lookup_pin_primitive`` returned ``None`` on a ``SQLAlchemyError``,
so a database outage was reported by the publish endpoint as a 422
"pin references unknown primitive" instead of a 503.

The rule mirrors the no-silent-failure-paths principle: every
``except SQLAlchemyError`` handler under ``src/modulo/api`` must do at least
one of:

* log the failure (``logger``/``_log``/``logging`` receiver, or a
  ``warning``/``exception``/``error``/``info``/``debug``/``critical`` method), or
* raise / surface it (an explicit ``raise``, or a call to a helper whose name
  marks it as raising - ``_raise_*``, ``*_unavailable_error``,
  ``_tool_error``, ``handle_db_error``, ...).

An arm that does neither is a silent swallow and fails the test.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo" / "api"

_LOG_METHODS = {"warning", "exception", "error", "info", "debug", "critical"}
_LOG_RECEIVERS = {"logger", "_log", "log", "logging", "_logger"}
# Name fragments that identify a helper which raises / surfaces the failure.
_SURFACING_HINTS = (
    "raise",
    "unavailable_error",
    "not_implemented_error",
    "tool_error",
    "db_error_response",
    "handle_db_error",
)


def _matches_sqlalchemy_error(node: ast.AST) -> bool:
    """True for ``except SQLAlchemyError`` and tuples containing it."""
    if isinstance(node, ast.Name):
        return node.id == "SQLAlchemyError"
    if isinstance(node, ast.Tuple):
        return any(_matches_sqlalchemy_error(elt) for elt in node.elts)
    return False


def _call_names(func: ast.AST) -> tuple[str, str]:
    """Return ``(receiver, name)`` for a call target."""
    if isinstance(func, ast.Attribute):
        recv = func.value.id if isinstance(func.value, ast.Name) else ""
        return recv, func.attr
    if isinstance(func, ast.Name):
        return "", func.id
    return "", ""


def _is_log_call(func: ast.AST) -> bool:
    recv, name = _call_names(func)
    if recv in _LOG_RECEIVERS:
        return True
    if name in _LOG_METHODS:
        return True
    lowered = name.lower()
    return lowered.startswith(("log_", "_log"))


def _is_surfacing_call(func: ast.AST) -> bool:
    _, name = _call_names(func)
    lowered = name.lower()
    return any(hint in lowered for hint in _SURFACING_HINTS)


def _handler_is_silent(handler: ast.ExceptHandler) -> bool:
    has_log = False
    has_raise = False
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            has_raise = True
        elif isinstance(node, ast.Call):
            if _is_log_call(node.func):
                has_log = True
            elif _is_surfacing_call(node.func):
                has_raise = True
    return not has_log and not has_raise


def test_no_silent_sqlalchemy_error_swallow() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None or not _matches_sqlalchemy_error(node.type):
                continue
            if _handler_is_silent(node):
                violations.append(f"  {path.relative_to(SRC.parent)}:{node.lineno}")

    assert not violations, (
        f"Found {len(violations)} silent except-SQLAlchemyError arm(s).\n"
        "A database failure must be logged or surfaced - never folded into a\n"
        "'not found' / empty result (FAR-1483). Log it (best-effort fails open\n"
        "WITH a log) or raise so the route can answer 503.\n" + "\n".join(violations)
    )
