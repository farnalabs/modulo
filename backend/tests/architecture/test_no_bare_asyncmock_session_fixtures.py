"""Architecture test: session doubles must express the ``AsyncSession`` contract.

Why this exists
---------------
Production reads part of SQLAlchemy's ``AsyncSession`` contract **synchronously**:

* ``session.in_transaction()`` / ``session.get_bind()`` — ``db/rls.py`` (the RLS
  preamble ``set_rls_org`` and ``_ensure_active_transaction``);
* ``session.add()`` / ``session.add_all()`` / ``session.expunge()`` — audit and
  report emitters (``core/audit_logger``);
* ``async with session.begin():`` — ``core/audit_logger``; and
* members of the **result** of an awaited ``session.execute(...)`` —
  ``db/settings_resolver.py`` reads ``result.scalar_one_or_none()``, other
  paths read ``result.scalars().all()`` / ``result.first()``.

A session test double built as a bare ``AsyncMock()`` auto-creates every child
attribute as an ``AsyncMock``. Each synchronous call therefore returns a
never-awaited coroutine, and each synchronous read of an awaited ``execute``
result returns another ``AsyncMock`` instead of a concrete row/``None``. The
orphaned coroutines surface later as ``PytestUnraisableExceptionWarning`` (one
multi-line traceback each). Left unchecked, the warnings summary grows until
pytest's terminal writer raises
``BlockingIOError: [Errno 11] write could not complete without blocking`` and
the CI step exits 1 — which is how this class of fixture took the merge queue
down on 2026-10-21.

The contract-correct way to build the double lives in
``tests/unit/api/mock_session.py``: ``configure_mock_session`` (or the narrower
``configure_rls_preamble``), plus ``make_empty_execute_result`` when the code
needs a real synchronous ``Result``.

What this test enforces
-----------------------
For every session-ish ``AsyncMock()`` binding under ``tests/unit/api``:

1. **a double returned from its enclosing function — a shared helper like
   ``_make_mock_session()`` / ``_make_session()`` — must go through
   ``configure_mock_session`` / ``configure_rls_preamble``.** This is the exact
   shape of the 2026-10-21 defect: the helper builds the double, and arbitrary
   production code reached through the route decides which session members get
   touched. There is no way to see that from the test, so the double has to
   express the whole synchronous contract up front.
2. every **synchronous** member it is driven through in the test itself is
   explicitly typed as a ``MagicMock`` (the sync contract); and
3. every ``begin()`` / ``begin_nested()`` it is driven through is typed as an
   async context manager — a ``MagicMock`` returning an async CM, or an
   ``@asynccontextmanager`` function; and
4. if the scope reads the result of ``await session.execute(...)``, that
   ``execute`` is explicitly stubbed with a result (``return_value=`` /
   ``side_effect=`` / a plain ``make_empty_execute_result()`` / ``MagicMock``).

Rules 2-4 apply to **every** session-ish binding, helper-built or inline. Rule
1 is scoped to helper-built doubles on purpose: an inline double lives in one
test and rule 2-4 already flag it when the test itself drives a leaky member.
Requiring *every* session-ish ``AsyncMock`` to use the helper would mean
rewriting ~120 deliberately-minimal inline doubles (including ones whose whole
point is an ``execute`` ``side_effect``) with no leak to show for it. A
production-driven sync call on an inline double is not statically visible from
the test — that residual is what the suite-level warning gate is for.

The discriminator (why this does not flag unrelated ``AsyncMock`` s)
-------------------------------------------------------------------
* **Session-ish name.** The binding target must match
  ``(^|_)(session|db)(_|$)`` case-insensitively. Doubles in this package are
  named ``session``, ``mock_session``, ``db``, ``db_session``,
  ``auth_session``, ``stock_session``, ``fake_session``, ... . Non-session
  collaborators — ``connector``, ``backend``, ``model_backend``, ``client``,
  ``httpx_client``, ``response``, ``flag``, ``service`` — do not match, so
  their ``AsyncMock`` s are ignored. This is why the rule is name-based rather
  than "every ``AsyncMock``".
* **Driven through the leak surface, not merely constructed.** Only the members
  listed above can leave a coroutine behind. ``execute``, ``commit``,
  ``rollback``, ``flush``, ``refresh``, ``scalar`` and ``scalars`` are
  *awaited* by production, so a bare child is harmless there unless its
  *result* is read. A double that is constructed but never driven through a
  guarded member (e.g. a route that returns 403 before touching the session)
  cannot leak and is not a finding. Flagging it would demand rewriting ~100
  fixtures that are deliberately simpler than the full contract — and would
  break the ones whose whole point is a ``side_effect`` (e.g. a failing
  ``execute``).
* **``session.info`` is not a leak source.** It is a plain ``dict`` attribute,
  not a coroutine producer, so it is not required to be typed. (An
  unconfigured ``AsyncMock.info`` is still a contract smell, but it cannot
  produce the warning this guard exists for.)
* **Scope-aware, with one level of alias.** Usage is measured in the scope that
  binds the double. When the binding is returned from a helper (``_make_session``
  and friends) it is exercised by that helper's callers, so usage of
  ``session = self._make_session()`` at each call site is folded in. Matching
  usage across the whole file would false-positive wherever the same name also
  refers to a real ``AsyncSession`` fixture (``test_plan_context``,
  ``test_toctou_hardening``).

A no-violation pass must be meaningful: ``test_detector_flags_the_defect_and_
accepts_the_fix`` feeds synthetic sources through the same detector, so a
detector that silently matched nothing fails here instead of passing.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

_API_TESTS = Path(__file__).resolve().parent.parent / "unit" / "api"

#: ``session``/``db`` as a whole component of the name: ``session``,
#: ``mock_session``, ``db``, ``db_session``, ``auth_session``, ...
_SESSION_NAME = re.compile(r"(^|_)(session|db)(_|$)", re.IGNORECASE)

#: Members production calls SYNCHRONOUSLY. On a bare ``AsyncMock`` child each
#: call returns a never-awaited coroutine.
_SYNC_MEMBERS = frozenset({"in_transaction", "get_bind", "add", "add_all", "expunge"})

#: Members production uses as an ASYNC CONTEXT MANAGER. A bare child returns a
#: coroutine, which is not an async CM (TypeError) *and* leaks.
_ASYNC_CM_MEMBERS = frozenset({"begin", "begin_nested"})

#: Readers production calls on the value returned by ``await execute(...)``.
_RESULT_READERS = frozenset(
    {
        "scalar_one_or_none",
        "scalar_one",
        "scalar",
        "first",
        "fetchone",
        "one",
        "one_or_none",
        "all",
        "fetchall",
        "mappings",
        "unique",
    }
)

_CONFIGURE_HELPERS = frozenset({"configure_mock_session", "configure_rls_preamble"})

_ASYNC_CM_DECORATORS = frozenset({"asynccontextmanager"})

_ASYNC_MOCK = "AsyncMock"
_MAGIC_MOCK = "MagicMock"


@dataclass
class _Facts:
    """What one lexical scope does with the names it can see."""

    used: dict[str, set[str]] = field(default_factory=dict)
    typed: dict[str, dict[str, str]] = field(default_factory=dict)
    execute_result_reads: dict[str, set[str]] = field(default_factory=dict)
    execute_stubbed: set[str] = field(default_factory=set)
    returns: set[str] = field(default_factory=set)
    configured: set[str] = field(default_factory=set)


def _call_name(func: ast.expr) -> str | None:
    """Return the simple name of a call target (``f`` or ``obj.f`` -> ``f``)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _value_kind(value: ast.expr) -> str:
    """Classify an assigned value for contract-typing checks."""
    if isinstance(value, ast.Call):
        name = _call_name(value.func)
        return f"call:{name}" if name else "call:"
    if isinstance(value, ast.Name):
        return f"name:{value.id}"
    return "other"


def _call_has_result_keyword(call: ast.Call) -> bool:
    return any(kw.arg in {"return_value", "side_effect"} for kw in call.keywords)


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _direct_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Yield nodes of *this* scope only (nested functions/classes excluded)."""
    stack: list[ast.AST] = [child for child in getattr(scope, "body", []) if not isinstance(child, _SCOPE_BOUNDARIES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node) if not isinstance(child, _SCOPE_BOUNDARIES))


def _async_cm_function_names(scope: ast.AST) -> set[str]:
    """Names of local functions decorated with ``@asynccontextmanager``."""
    names: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if _call_name(target) in _ASYNC_CM_DECORATORS:
                names.add(node.name)
    return names


def _collect_facts(scope: ast.AST) -> _Facts:
    facts = _Facts()
    nodes = list(_direct_nodes(scope))

    # Pass 1: attribute reads / assignments, execute() result variables, returns.
    execute_vars: dict[str, set[str]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    name, attr = target.value.id, target.attr
                    facts.used.setdefault(name, set()).add(attr)
                    facts.typed.setdefault(name, {})[attr] = _value_kind(node.value)
                    if attr == "execute" and isinstance(node.value, ast.Call) and _call_has_result_keyword(node.value):
                        facts.execute_stubbed.add(name)
                    if (
                        attr == "execute"
                        and isinstance(node.value, ast.Call)
                        and _call_name(node.value.func) != _ASYNC_MOCK
                    ):
                        # ``make_empty_execute_result()`` / ``MagicMock()`` — a
                        # real synchronous result, not an auto-created child.
                        facts.execute_stubbed.add(name)
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "execute"
                    and isinstance(target.value.value, ast.Name)
                    and target.attr in {"return_value", "side_effect"}
                ):
                    facts.execute_stubbed.add(target.value.value.id)
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Await):
                    awaited = node.value.value
                    if (
                        isinstance(awaited, ast.Call)
                        and isinstance(awaited.func, ast.Attribute)
                        and awaited.func.attr == "execute"
                        and isinstance(awaited.func.value, ast.Name)
                    ):
                        execute_vars.setdefault(awaited.func.value.id, set()).add(target.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            facts.used.setdefault(node.value.id, set()).add(node.attr)
            # Chained read: ``(await session.execute(...)).scalar_one_or_none()``
            if (
                isinstance(node.value, ast.Await)
                and isinstance(node.value.value, ast.Call)
                and isinstance(node.value.value.func, ast.Attribute)
                and node.value.value.func.attr == "execute"
                and isinstance(node.value.value.func.value, ast.Name)
            ):
                facts.execute_result_reads.setdefault(node.value.value.func.value.id, set()).add(node.attr)
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
            facts.returns.add(node.value.id)
        elif isinstance(node, ast.Call) and _call_name(node.func) in _CONFIGURE_HELPERS:
            facts.configured.update(arg.id for arg in node.args if isinstance(arg, ast.Name))

    # Pass 2: attribute reads on variables that hold an awaited execute() result.
    result_vars = {var: session for session, variables in execute_vars.items() for var in variables}
    if result_vars:
        for node in nodes:
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                session = result_vars.get(node.value.id)
                if session is not None:
                    facts.execute_result_reads.setdefault(session, set()).add(node.attr)
    return facts


def _scopes(tree: ast.Module) -> Iterator[ast.AST]:
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _session_bindings(scope: ast.AST) -> Iterator[tuple[str, int]]:
    """``(name, lineno)`` for each ``name = AsyncMock(...)`` in this scope."""
    for node in _direct_nodes(scope):
        if not (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == _ASYNC_MOCK
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and _SESSION_NAME.search(target.id):
                yield target.id, node.lineno


def _helper_alias_usage(tree: ast.Module, helper_returns: dict[str, set[str]]) -> dict[str, _Facts]:
    """Facts for ``x = helper()`` call sites, keyed by the helper's name.

    A helper that returns its session double is exercised by its callers, so
    the caller's use of the returned value has to count towards the double.
    """
    alias: dict[str, _Facts] = {}
    for scope in _scopes(tree):
        facts = _collect_facts(scope)
        for node in _direct_nodes(scope):
            if not (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and (_call_name(node.value.func) or "") in helper_returns
            ):
                continue
            helper = _call_name(node.value.func)
            assert helper is not None  # narrowed by the membership test above
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                folded = alias.setdefault(helper, _Facts())
                for attr in facts.used.get(target.id, set()):
                    folded.used.setdefault(target.id, set()).add(attr)
                for reader in facts.execute_result_reads.get(target.id, set()):
                    folded.execute_result_reads.setdefault(target.id, set()).add(reader)
                folded.returns.update(facts.returns)
    return alias


def _violations_in_source(source: str, filename: str) -> list[str]:
    """Return a human-readable violation per leaking session double."""
    tree = ast.parse(source, filename=filename)
    cm_functions = _async_cm_function_names(tree)

    # Helper name -> names it returns (only plain ``return <name>``).
    helper_returns: dict[str, set[str]] = {}
    for scope in _scopes(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name, _ in _session_bindings(scope):
                if name in _collect_facts(scope).returns:
                    helper_returns.setdefault(scope.name, set()).add(name)
    alias_usage = _helper_alias_usage(tree, helper_returns)

    violations: list[str] = []
    seen: set[tuple[int, str]] = set()
    for scope in _scopes(tree):
        facts = _collect_facts(scope)
        helper_name = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""
        for name, lineno in _session_bindings(scope):
            if (lineno, name) in seen:
                continue
            seen.add((lineno, name))
            if name in facts.configured:
                continue
            used = set(facts.used.get(name, set()))
            result_readers = set(facts.execute_result_reads.get(name, set()))
            is_shared_helper = helper_name in helper_returns and name in helper_returns[helper_name]
            if is_shared_helper:
                folded = alias_usage.get(helper_name, _Facts())
                used |= folded.used.get(name, set())
                result_readers |= folded.execute_result_reads.get(name, set())
            typed = facts.typed.get(name, {})

            problems: list[str] = []
            if is_shared_helper:
                # Rule 1: a double built by a helper is driven by arbitrary
                # production code the test cannot see, so it must express the
                # whole synchronous contract (i.e. go through the helper).
                problems.append(
                    "shared helper double is not built through configure_mock_session/configure_rls_preamble"
                )
            untyped_sync = sorted((used & _SYNC_MEMBERS) - set(typed))
            if untyped_sync:
                problems.append(f"sync member(s) not typed as MagicMock: {', '.join(untyped_sync)}")
            for member in sorted(used & _ASYNC_CM_MEMBERS):
                kind = typed.get(member, "")
                if kind == f"call:{_MAGIC_MOCK}":
                    continue
                if kind.startswith("name:") and kind.removeprefix("name:") in cm_functions:
                    continue
                problems.append(f"{member}() is not an async context manager")
            if result_readers and name not in facts.execute_stubbed:
                readers = ", ".join(sorted(result_readers & _RESULT_READERS)) or ", ".join(sorted(result_readers))
                problems.append(f"execute() result read ({readers}) without a stubbed synchronous result")
            if problems:
                violations.append(f"{filename}:{lineno}: '{name}' — " + "; ".join(problems))
    return violations


def _all_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(_API_TESTS.rglob("*.py")):
        violations.extend(
            _violations_in_source(path.read_text(encoding="utf-8"), str(path.relative_to(_API_TESTS.parent.parent)))
        )
    return violations


def test_no_bare_asyncmock_session_doubles() -> None:
    """Every driven session double expresses the synchronous session contract."""
    violations = _all_violations()
    assert not violations, (
        "Session test doubles leaking un-awaited coroutines — build the double with "
        "tests/unit/api/mock_session.configure_mock_session (or type the synchronous "
        "session contract explicitly):\n" + "\n".join(violations)
    )


def test_detector_flags_the_defect_and_accepts_the_fix() -> None:
    """The detector must actually detect, or the guard above proves nothing."""
    leaking = """
from unittest.mock import AsyncMock

async def test_x():
    session = AsyncMock()
    async with session.begin():
        pass
    session.in_transaction()
    session.execute.return_value = None
"""
    assert _violations_in_source(leaking, "leaking.py")

    configured = """
from unittest.mock import AsyncMock
from tests.unit.api.mock_session import configure_mock_session

async def test_x():
    session = configure_mock_session(AsyncMock())
    async with session.begin():
        pass
    session.in_transaction()
"""
    assert not _violations_in_source(configured, "configured.py")

    typed = """
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

@asynccontextmanager
async def fake_begin():
    yield

async def test_x():
    session = AsyncMock()
    session.begin = fake_begin
    session.in_transaction = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=MagicMock())
"""
    assert not _violations_in_source(typed, "typed.py")

    # The 2026-10-21 defect shape: a shared helper builds a bare double and
    # types only part of the contract. Arbitrary production code reached
    # through the route supplies the rest, so the helper must be flagged.
    shared_bare = """
from unittest.mock import AsyncMock, MagicMock

def _make_mock_session():
    session = AsyncMock()
    begin_cm = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    session.get_bind = AsyncMock(return_value=MagicMock())
    return session
"""
    assert _violations_in_source(shared_bare, "shared_bare.py")

    # ... and the fixed version of the same helper is clean.
    shared_configured = """
from unittest.mock import AsyncMock, MagicMock
from tests.unit.api.mock_session import configure_mock_session

def _make_mock_session():
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    return session
"""
    assert not _violations_in_source(shared_configured, "shared_configured.py")

    # An AsyncMock for a non-session collaborator is not a finding.
    unrelated = """
from unittest.mock import AsyncMock

async def test_x():
    connector = AsyncMock()
    connector.fetch()
    client = AsyncMock()
    client.get()
"""
    assert not _violations_in_source(unrelated, "unrelated.py")
