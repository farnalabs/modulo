"""Architecture guard: every ``TriggerEvent.validation_result`` write value is
in ``VALIDATION_RESULT_VALUES``.

``TriggerEvent.validation_result`` is gated by a HARD database CHECK
constraint (``ck_trigger_events_validation_result``) whose allowed values come
from the model tuple ``VALIDATION_RESULT_VALUES``. ``_TRIGGER_EVENT_VALIDATION_SQL``
derives the ORM constraint from that same tuple, so the ORM model and the DB
gate are already in lockstep. The missing link has always been the *write
sites*: a value written but absent from the vocabulary raises ``IntegrityError``
on every real database, so the event silently never persists and the route
returns the wrong status.

That exact class of bug shipped twice:

* FAR-1916 — ``result="required_refs_missing"`` was written by the trigger
  engine but absent from the vocabulary, so the 422 became a 503 and no
  refusal event had ever persisted (fixed in PR #1938, which added the value
  plus migration ``0318``).
* ``slack_app_mention._parse_mention`` writes ``result="parse_failed"`` for a
  malformed Slack ``app_mention`` payload. That value is still absent from the
  vocabulary / CHECK constraint, so the write raises ``IntegrityError`` and no
  audit row persists (see the known-drift ledger below).

This test statically resolves the value at every write site and asserts every
one is in the vocabulary. It fails naming the offending file, line and value.

The resolver walks the helper chains rather than only literal call sites, so it
catches a value passed through any of the shared writers (``_log_event``,
``_add_trigger_event``, ``_log_poll_event``, ``_log_ongoing_event``,
``_log_signal_event``, ``_record_refused_delivery``, ...). It is AST-based, not
regex-based, so a re-ordered argument list or a multi-line call cannot hide a
value.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"

#: The ORM model builds ``TriggerEvent(validation_result=...)``.
_CONSTRUCTOR = "TriggerEvent"
_CONSTRUCTOR_KEYWORD = "validation_result"

#: The shared writer helpers thread the value through a ``result`` keyword.
_HELPER_KEYWORD = "result"

# ---------------------------------------------------------------------------
# Known, reviewed drift awaiting a migration.
#
# ``parse_failed`` is genuinely written (slack_app_mention._parse_mention) but
# is NOT in VALIDATION_RESULT_VALUES / the DB CHECK. Adding it to the model
# without a widening migration would put the ORM constraint ahead of the DB
# constraint and break the model<->migration parity tests, so the value is
# deliberately NOT added here: it needs a widening migration, and migration
# numbering is sequenced by the Conductor separately from this guard.
#
# This ledger is strictly checked: the test fails if the observed drift is not
# exactly this set. A NEW drifted value fails (naming it); once the migration
# lands and ``parse_failed`` joins the vocabulary, this entry becomes stale and
# the test fails again until the entry is removed. There is therefore no state
# in which this ledger silently hides drift.
# ---------------------------------------------------------------------------
_KNOWN_PENDING_MIGRATION_DRIFT = {"parse_failed"}


class _WriteSite(NamedTuple):
    """One statically-resolved ``validation_result`` write site."""

    path: Path
    line: int
    callable_name: str
    value: str | None
    expression: str | None


def _constant_string(node: ast.AST) -> str | None:
    """Return the string a ``str`` literal node holds, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.AST) -> str | None:
    """Return the bare callable name for ``func`` (``engine._log_event`` -> ``_log_event``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _params_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return every parameter name bound by ``fn`` (positional, keyword-only, ``*args``/``**kwargs``)."""
    args = fn.args
    names = {arg.arg for arg in args.posonlyargs}
    names.update(arg.arg for arg in args.args)
    names.update(arg.arg for arg in args.kwonlyargs)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _parse_sources() -> list[tuple[Path, ast.Module]]:
    """Parse every ``src/modulo`` module, skipping migrations (which only hardcode DDL)."""
    sources: list[tuple[Path, ast.Module]] = []
    for path in sorted(SRC.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        sources.append((path, tree))
    return sources


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Map top-level ``NAME = "literal"`` assignments to their string values."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = _constant_string(node.value)
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _collect_functions(
    sources: list[tuple[Path, ast.Module]],
) -> dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Map each function's bare name to every definition of it in the tree."""
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for _path, tree in sources:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, []).append(node)
    return functions


def _discover_helper_params(
    sources: list[tuple[Path, ast.Module]],
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]],
) -> dict[str, set[str]]:
    """Find every helper parameter that flows into a ``validation_result`` write.

    Seeds from ``TriggerEvent(validation_result=<param>)`` constructions and
    propagates through helper-to-helper forwarding until a fixpoint, so
    ``_log_event(result=result) -> _add_trigger_event(result=result) ->
    TriggerEvent(validation_result=result)`` all resolve to one sink set.
    """
    sink_params: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for name, definitions in functions.items():
            for fn in definitions:
                params = _params_of(fn)
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    call_name = _call_name(node.func)
                    if call_name == _CONSTRUCTOR:
                        keywords = {_CONSTRUCTOR_KEYWORD}
                    elif call_name is not None and call_name in sink_params:
                        keywords = sink_params[call_name]
                    else:
                        continue
                    for keyword in node.keywords:
                        if keyword.arg not in keywords:
                            continue
                        if not isinstance(keyword.value, ast.Name):
                            continue
                        if keyword.value.id not in params:
                            continue
                        if keyword.value.id not in sink_params.setdefault(name, set()):
                            sink_params[name].add(keyword.value.id)
                            changed = True
    return sink_params


def _resolve_write_expression(
    node: ast.expr,
    enclosing_params: set[str],
    module_constants: dict[str, str],
) -> tuple[str | None, str | None]:
    """Resolve a write expression to ``(value, unresolved_expression)``.

    * string literal -> ``(value, None)``
    * module-level string constant -> ``(value, None)``
    * a parameter of the enclosing function -> ``(None, None)`` (resolved at its callers)
    * anything else -> ``(None, unparsed_source)`` (fails the guard loudly)
    """
    literal = _constant_string(node)
    if literal is not None:
        return literal, None
    if isinstance(node, ast.Name):
        if node.id in enclosing_params:
            return None, None
        if node.id in module_constants:
            return module_constants[node.id], None
    try:
        source = ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is total on parsed trees
        source = "<unparseable>"
    return None, source


def _collect_write_sites() -> list[_WriteSite]:
    """Statically resolve every ``validation_result`` value written in ``src/modulo``."""
    sources = _parse_sources()
    functions = _collect_functions(sources)
    sink_params = _discover_helper_params(sources, functions)
    sites: list[_WriteSite] = []
    for path, tree in sources:
        module_constants = _module_string_constants(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            enclosing_params = _params_of(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                call_name = _call_name(node.func)
                if call_name == _CONSTRUCTOR:
                    keywords = {_CONSTRUCTOR_KEYWORD}
                elif call_name is not None and call_name in sink_params:
                    keywords = sink_params[call_name]
                else:
                    continue
                keyword = next((kw for kw in node.keywords if kw.arg in keywords), None)
                if keyword is None:
                    sites.append(_WriteSite(path, node.lineno, call_name, None, f"{call_name}(...)"))
                    continue
                value, unresolved = _resolve_write_expression(keyword.value, enclosing_params, module_constants)
                sites.append(_WriteSite(path, node.lineno, call_name, value, unresolved))
    return sites


def _format_sites(sites: list[_WriteSite]) -> str:
    """Render write sites as ``relative/path.py:line callable -> value`` lines."""
    root = SRC.parent.parent
    lines = []
    for site in sites:
        rendered = site.value if site.value is not None else (site.expression or "<parameter flow>")
        lines.append(f"  {site.path.relative_to(root)}:{site.line} {site.callable_name} -> {rendered!r}")
    return "\n".join(lines)


def test_trigger_event_write_values_are_statically_resolvable() -> None:
    """Every write value must be a literal/constant (or a resolvable helper param).

    A dynamic expression is allowed to exist in principle, but it must be
    surfaced here rather than silently skipped: an unresolvable write site is
    exactly how a drifted value would slip past the membership check below.
    """
    unresolved = [site for site in _collect_write_sites() if site.expression is not None]
    assert not unresolved, (
        "TriggerEvent.validation_result write sites whose value could not be "
        "statically resolved:\n"
        + _format_sites(unresolved)
        + "\nUse a string literal or a module-level constant so the vocabulary "
        "guard can verify the value."
    )


def test_trigger_event_write_values_are_in_the_vocabulary() -> None:
    """Every resolved write value must be a member of ``VALIDATION_RESULT_VALUES``."""
    sites = _collect_write_sites()
    resolved = [site for site in sites if site.value is not None]
    offenders = [site for site in resolved if site.value not in VALIDATION_RESULT_VALUES]
    drift = {site.value for site in offenders if site.value is not None}
    assert drift == _KNOWN_PENDING_MIGRATION_DRIFT, (
        "TriggerEvent.validation_result values written but absent from "
        "VALIDATION_RESULT_VALUES (a write raises IntegrityError on every real "
        "database, so the event never persists):\n"
        + _format_sites(offenders)
        + "\nUnexpected drift is a bug: add the value to VALIDATION_RESULT_VALUES "
        "AND a widening migration in the same change. If the known entry "
        "('parse_failed') has since been added to the vocabulary, remove it "
        "from _KNOWN_PENDING_MIGRATION_DRIFT in this test."
    )
