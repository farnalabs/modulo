"""Architecture test: QA lenses over the test suite.

The tests in this file apply the same AST-scanning discipline the rest of
``tests/architecture/`` applies to ``src/modulo``, but pointed at the test
packages themselves. Each lens guards against a class of test-quality
regression that silently weakens the suite:

- always-pass/always-fail assertions (dead or inverted tests, including
  ``assert not <falsy constant>``)
- ``pytest.skip``/skip markers without a reason (undocumented silencing)
- bare ``except:`` handlers (swallow BaseException, hide KeyboardInterrupt)
- debugger remnants (``breakpoint``/``pdb``) committed by accident
- deprecated ``datetime.utcnow()`` / ``datetime.utcfromtimestamp()``
- naive ``datetime.now()`` (no timezone argument) fed into tz-aware code
- ``== True`` / ``== False`` equality on booleans (type confusion + E712)
- ``== None`` / ``!= None`` equality (identity vs. equality on singletons, E711)
- same-scope ``test_*`` redefinition (silently drops the earlier test)
- ``asyncio.run()`` nested inside ``async def`` tests (conflicts with the loop)
- ``assert`` in a ``try:`` body guarded by a swallowing ``except Exception:``
- stray ``print()`` calls polluting CI output
- fixtures that nothing requests (dead setup code that never runs)
- ``==`` against a float literal that is not exactly representable in binary
  (``0.1``, ``0.04``, ``0.95``, ...) — precision-fragile equality that
  ``pytest.approx`` is designed to replace
- ``assert len(x) == 0`` / ``assert 0 == len(x)`` where the ``len()`` operand
  is an attribute access, subscript, call, or container literal — an
  anti-idiom that should read ``assert not x`` and trips ruff SIM101
- tautological ``len()`` comparison bounds — ``len(x) >= 0`` and friends
  compare against a bound that ``len()`` can never cross (it never returns a
  negative number), so the assertion either always passes (``>= 0``, ``> -1``,
  ``!= -1``) or always fails (``< 0``, ``<= -1``, ``== -1``) and is dead either
  way
- ``assert len(x) > 0`` / ``assert len(x) >= 1`` / ``assert len(x) != 0``
  (the non-emptiness mirror of the ``len(x) == 0`` lens) — sized containers
  are truthy exactly when non-empty, so these should read ``assert x``
- ``assert x == []`` / ``assert x == {}`` against an empty container literal —
  ``== []``/``== {}`` is the equality-based twin of the ``len() == 0`` idiom
  and should read ``assert not x`` (an empty container is falsy)
- ``assert x == ""`` / ``assert x != ""`` against an empty string literal — the
  string twin of the empty-container lens; an empty string is falsy, so these
  should read ``assert not x`` / ``assert x``
- ``assert x == ()`` / ``assert x != ()`` against an empty tuple literal — the
  tuple twin of the empty-container lens; an empty tuple is falsy, so these
  should read ``assert not x`` / ``assert x`` (``is``/``is not`` against ``()``
  is deliberately left alone because ``()`` is interned)
- hand-rolled ``try: ... raise AssertionError(...) except X: pass`` instead of
  ``pytest.raises`` (the success path is only guarded by the ``raise`` line)
- ``assert`` nested inside ``except`` handlers (a failing assert masks the
  original exception and discards its traceback context)
- no-op ``test_*`` functions whose body contains no verification at all (they
  report green even when the code under test is completely broken, as long as
  no exception escapes)
- assertions comparing two *literal constants* (``assert 1 == 1``,
  ``assert 3 > 5``, ``assert 'a' not in {'b': 1}``) — the outcome is fixed at
  source time, so the assertion either always passes (dead green) or always
  fails (unconditionally red) regardless of the behaviour under test
- ``is``/``is not`` identity comparisons against a mutable container literal
  (``assert x is []``, ``assert result is {}``, ``assert x is not {1}``) —
  list/dict/set literals are freshly allocated on every evaluation, so the
  comparison can never hold (``is``) or can never fail (``is not``) and is
  dead either way (Python 3.8+ also emits a SyntaxWarning for it)
- redundant ``assert <mock>.called`` right before the test inspects the same
  mock's recorded calls (``<mock>.calls[0]``/``<mock>.call_args[0]``) — the
  introspection access that follows already fails loudly when the call never
  happened, so the ``.called`` assert is dead code that can silently drift out
  of sync with what the test actually inspects
- membership tests against an empty container literal (``assert x in []``,
  ``assert x not in {}``, ``assert x in ()``) — an empty container can never
  contain anything, so ``in`` always FAILS and ``not in`` always PASSES no
  matter what the operand evaluates to
- ``@pytest.mark.parametrize`` with a single case in ``argvalues`` — a
  parametrize that adds no matrix coverage; indistinguishable from an ordinary
  test body and almost always a leftover from trimming the case list down
- unbounded subprocess calls — ``subprocess.run``/``Popen``/``call``/
  ``check_call``/``check_output`` without a ``timeout=`` bound, and
  ``asyncio.create_subprocess_*`` processes whose ``communicate()``/``wait()``
  is not wrapped in ``asyncio.wait_for(...)``. A child process with no bound
  can hang CI indefinitely, and the failure is opaque (the runner just stops)
  instead of surfacing a bound violation the way ``requests_without_timeout``
  already does for HTTP in ``src/modulo``.
- ``assert A and B`` where every operand is a comparison — a compound boolean
  assertion that should be one ``assert`` per condition; when the conjunction
  fails, pytest reports the whole expression and cannot say which operand broke
  (``or`` conjunctions are deliberately left alone: they are the intentional
  "any of these" idiom and cannot be split without changing semantics)
- ``assert bool(x)`` / ``assert not bool(x)`` — ``bool()`` is a no-op inside an
  ``assert``, which already tests truthiness (and inverts it under ``not``);
  the wrapper adds noise without changing the outcome
- ``assert not (a == b)`` / ``assert not (a in b)`` / ``assert not (a is b)`` —
  negating a single comparison with ``not`` instead of writing the positive
  mirror (``assert a != b``, ``assert a not in b``, ``assert a is not b``).
  A ``not``-wrapped comparison reports the *negation* of a comparison in the
  failure diff, where the mirrored operator reads the intent directly; it is
  also the exact class of expression ruff's SIM201/SIM202 flags. ``not``
  applied to a ``BoolOp`` (De Morgan compound) is left alone — that is the
  intentional "none of these hold" idiom.
- ``assert x == set()`` / ``assert x != list()`` against a zero-argument
  builtin call that always produces an empty container (``list()``,
  ``dict()``, ``set()``, ``tuple()``, ``bytes()``, ``bytearray()``,
  ``frozenset()``) — the call-based twin of the ``== []``/``== {}`` literal
  lens. Every such builtin returns a falsy container, so these should read
  ``assert not x`` / ``assert x``
- ``@pytest.mark.parametrize`` with an *empty* ``argvalues`` — the inverse
  twin of the single-case lens. A parametrize with zero cases is collected as
  zero test items, so the test body never runs at all: pytest emits a
  collection warning and the suite still reports green, silently dropping
  whatever regression coverage the test provided. Usually a leftover from
  deleting the last case, or an ``argvalues`` list built by code that returned
  nothing.
- ``assert isinstance(a, X) and isinstance(b, Y)`` — an ``and`` conjunction
  whose operands are all ``isinstance()`` calls is a compound boolean
  assertion: when it fails, pytest reports the whole conjunction and cannot
  say which operand had the wrong type. Split it into one ``assert`` per
  isinstance call so each failure names its own operand. A single isinstance,
  or an isinstance mixed with a truthiness/``is not None`` check (the
  deliberate "type and non-empty" idiom), is left alone

Every lens is written so it reports actionable file:line violations instead
of a bare "assert not violations", mirroring the sibling architecture tests.
"""

import ast
import operator
import re
from fractions import Fraction
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent

#: Test packages that are tooling rather than assertions and may legitimately
#: emit progress output or take long pauses (load/benchmark harnesses).
EXCLUDED_PACKAGES = {"load", "performance"}


def _decorator_name(dec: ast.AST) -> str | None:
    """Return the bare name of a decorator (``pytest.fixture`` -> ``fixture``)."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Name):
        return dec.id
    return None


def _iter_test_modules():
    for path in sorted(TESTS.rglob("*.py")):
        if any(part in EXCLUDED_PACKAGES for part in path.parts):
            continue
        yield path


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def test_no_always_pass_or_fail_assertions():
    """Assertions against a literal that can never fail (or can never pass)
    are dead code — they report a test as green regardless of behavior. This
    covers plain constants (`assert 1`) and negated constants (`assert not []`),
    which have the same guaranteed outcome but a different AST shape."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if isinstance(test, ast.Constant):
                if isinstance(test.value, complex):
                    continue
                value = test.value
                verdict = "always FAILS" if not value else "always PASSES"
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  assert {value!r} — {verdict}")
            elif (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Constant)
                and not isinstance(test.operand.value, complex)
            ):
                value = test.operand.value
                verdict = "always PASSES" if not value else "always FAILS"
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  assert not {value!r} — {verdict}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against literal constants.\n"
        "Assert against the actual behavior under test instead of a constant.\n" + "\n".join(violations)
    )


# Folders for comparison operators whose outcome is fully determined when both
# operands are literal constants (numbers, strings, booleans, ``None``, or
# container literals). ``is``/``is not`` are deliberately excluded: for *distinct*
# literals their outcome is implementation-defined (small-int/string interning),
# and for *identical* operands the self-comparison lens already owns them.
_LITERAL_COMPARISON_FOLDERS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _fold_literal_comparison(node: ast.Compare) -> str | None:
    """Return ``"always PASSES"``/``"always FAILS"`` for a comparison whose
    operands are both literal constants, or ``None`` when it cannot be folded
    statically (variables, calls, ``is``/``is not``, or a chained compare)."""
    if len(node.ops) != 1:
        return None
    op = node.ops[0]
    folder = _LITERAL_COMPARISON_FOLDERS.get(type(op))
    if folder is None:
        return None
    try:
        left = ast.literal_eval(node.left)
        right = ast.literal_eval(node.comparators[0])
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if isinstance(left, complex) or isinstance(right, complex):
        return None
    try:
        outcome = folder(left, right)
    except (TypeError, KeyError):
        return None
    return "always PASSES" if outcome else "always FAILS"


def test_no_literal_constant_comparisons():
    """An assertion comparing two *literal constants* — ``assert 1 == 1``,
    ``assert 3 > 5``, ``assert 'a' not in {'b': 1}`` — is fully determined at
    source time, so it is dead code either way: it always passes (reporting
    green no matter how broken the code under test is) or always fails
    (breaking the suite unconditionally). These are almost always leftover
    debugging, or a broken attempt to reference a value where the intended
    object was accidentally replaced by a literal — the outcome never depends
    on the code under test. ``is``/``is not`` are excluded (interning makes
    their outcome implementation-defined for distinct literals) and the
    self-comparison lens owns identical operands.
    """
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            for sub in ast.walk(node.test):
                if not isinstance(sub, ast.Compare):
                    continue
                verdict = _fold_literal_comparison(sub)
                if verdict is None:
                    continue
                violations.append(
                    f"  {rel}:{sub.lineno}  {ast.unparse(sub)} — {verdict} (both operands are literal constants)"
                )
    assert not violations, (
        f"Found {len(violations)} literal-constant comparison(s) in assertions.\n"
        "Both operands are source literals, so the outcome is fixed at write time.\n"
        "Assert against the actual value under test, or the comparison is dead code.\n" + "\n".join(violations)
    )


def test_literal_comparison_lens_flags_constant_outcomes():
    """Synthetic positive/negative control for the literal-constant lens,
    mirroring the no-op and self-comparison lens patterns: it must flag every
    assertion whose operands are both source literals (fixed outcome) and
    ignore comparisons involving variables, calls, chained compares, or
    ``is``/``is not`` identity on distinct literals."""
    positive_sources = [
        "def test_foo():\n    assert 1 == 1\n",
        "def test_foo():\n    assert 3 > 5\n",
        "def test_foo():\n    assert 'a' != 'b'\n",
        "def test_foo():\n    assert 0.5 >= 0.25\n",
        "def test_foo():\n    assert [] == []\n",
        "def test_foo():\n    assert 'x' in {'x': 1}\n",
        "def test_foo():\n    assert 'hitl_gate_a_b' not in {'a': 'agent'}\n",
        "def test_foo():\n    assert 1 == 1 and x == 2\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assert))
        flagged = any(
            isinstance(sub, ast.Compare) and _fold_literal_comparison(sub) is not None
            for sub in ast.walk(assert_node.test)
        )
        assert flagged, f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x == 1\n",
        "def test_foo():\n    assert 1 == x\n",
        "def test_foo():\n    assert x in {'a': 1}\n",
        "def test_foo():\n    assert x == x\n",
        "def test_foo():\n    assert len(a) != len(a)\n",
        "def test_foo():\n    assert 'a' in some_dict\n",
        "def test_foo():\n    assert x is None\n",
        "def test_foo():\n    assert 1 == 1 == 1\n",
        "def test_foo():\n    assert x == 1 and y == 2\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assert))
        flagged = any(
            isinstance(sub, ast.Compare) and _fold_literal_comparison(sub) is not None
            for sub in ast.walk(assert_node.test)
        )
        assert not flagged, f"lens should NOT flag:\n{source}"


def test_no_none_equality_comparison():
    """``x == None`` / ``x != None`` rely on ``__eq__`` (E711) and break for
    objects whose equality is overloaded; compare identity with ``is None``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            op = node.ops[0]
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and side.value is None:
                    op_name = "==" if isinstance(op, ast.Eq) else "!="
                    violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  compares {op_name} None")
                    break
    assert not violations, (
        f"Found {len(violations)} equality comparison(s) against None.\n"
        "Use 'is None'/'is not None' to compare identity, not equality.\n" + "\n".join(violations)
    )


def test_no_test_redefinition_in_same_scope():
    """Two ``test_*`` functions (or methods in the same class) with the same
    name silently shadow each other — pytest only collects the last one and the
    earlier test is never run. Duplicates in *different* classes are fine."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)

        def _is_test(node):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            if node.name.startswith("test_"):
                return True
            return any(isinstance(d, ast.Name) and d.id == "test" for d in node.decorator_list)

        module_seen = {}
        for item in tree.body:
            if _is_test(item):
                module_seen.setdefault(item.name, []).append(item.lineno)
        for name, lines in module_seen.items():
            if len(lines) > 1:
                violations.append(f"  {rel}  <module> {name} redefined: {lines}")
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            class_seen = {}
            for item in cls.body:
                if _is_test(item):
                    class_seen.setdefault(item.name, []).append(item.lineno)
            for name, lines in class_seen.items():
                if len(lines) > 1:
                    violations.append(f"  {rel}  {cls.name}.{name} redefined: {lines}")
    assert not violations, (
        f"Found {len(violations)} test redefinition(s) in the same scope.\n"
        "A later definition silently shadows the earlier test; rename it.\n" + "\n".join(violations)
    )


def test_no_asyncio_run_inside_async_test():
    """``asyncio.run()`` inside an ``async def`` test conflicts with the running
    event loop (pytest-asyncio already provides one) and will raise — the test
    is simply wrong as written."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "run"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "asyncio"
                ):
                    violations.append(f"  {rel}:{node.lineno}  asyncio.run() inside async def {fn.name}")
    assert not violations, (
        f"Found {len(violations)} asyncio.run() call(s) inside async tests.\n"
        "pytest-asyncio provides the loop; drop the nested asyncio.run().\n" + "\n".join(violations)
    )


def test_no_assert_under_swallowing_except():
    """An ``assert`` in a ``try:`` body whose ``except Exception:``/``except:``
    handler swallows the exception (no re-raise, no pytest.fail) can fail
    silently and still report the test as green."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

        def _reports_failure(handler):
            def _scan(nodes):
                for stmt in nodes:
                    if isinstance(stmt, ast.Raise):
                        return True
                    if isinstance(stmt, ast.Call):
                        f = stmt.func
                        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                        if name in ("fail", "skip", "xfail"):
                            return True
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    if _scan(ast.iter_child_nodes(stmt)):
                        return True
                return False

            return _scan(handler.body)

        def _catches_assertion(handler):
            if handler.type is None:
                return True
            return isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            current = node
            parent = parents.get(current)
            while parent is not None:
                if isinstance(parent, ast.Try) and current in parent.body:
                    swallowing = [h for h in parent.handlers if _catches_assertion(h) and not _reports_failure(h)]
                    if swallowing:
                        violations.append(f"  {rel}:{node.lineno}  assert inside try guarded by swallowing except")
                        break
                current = parent
                parent = parents.get(current)
    assert not violations, (
        f"Found {len(violations)} assert(s) inside a try/except that swallows failures.\n"
        "Move the assert outside the try or re-raise/pytest.fail in the handler.\n" + "\n".join(violations)
    )


def test_no_skip_without_reason():
    """Skips without a reason are undocumented silencing — a future reader
    cannot tell whether the skip is expected or accidental."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
            if name in ("skip", "skipped") and not node.args and not node.keywords:
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  pytest.skip() without reason")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                f = dec.func
                dname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                if dname not in ("skip", "xfail", "skipif"):
                    continue
                if not any(k.arg == "reason" and k.value for k in dec.keywords):
                    violations.append(f"  {path.relative_to(TESTS)}:{dec.lineno}  @pytest.mark.{dname} without reason")
    assert not violations, (
        f"Found {len(violations)} skip/skipif/xfail without a reason.\n"
        "Always pass reason= so the skip is self-documenting.\n" + "\n".join(violations)
    )


def test_no_bare_except():
    """``except:`` catches BaseException (KeyboardInterrupt, SystemExit) and
    hides failures; test code should always name ``Exception``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  bare 'except:'")
    assert not violations, (
        f"Found {len(violations)} bare 'except:' handler(s).\n"
        "Use 'except Exception:' so KeyboardInterrupt/SystemExit still propagate.\n" + "\n".join(violations)
    )


def test_no_debugger_remnants():
    """Committed breakpoints or pdb imports pause CI runs and are always a
    leftover from interactive debugging."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "breakpoint":
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  breakpoint()")
            if isinstance(node, ast.Import) and any(a.name in ("pdb", "ipdb", "pudb") for a in node.names):
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  import {node.names[0].name}")
            if isinstance(node, ast.ImportFrom) and node.module in ("pdb", "ipdb", "pudb"):
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  from {node.module} import ...")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_trace"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("pdb", "ipdb")
            ):
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  {node.func.value.id}.set_trace()")
    assert not violations, (
        f"Found {len(violations)} debugger remnant(s).\n"
        "Remove breakpoint()/pdb before committing.\n" + "\n".join(violations)
    )


def test_no_deprecated_utcnow():
    """``datetime.utcnow()``/``datetime.utcfromtimestamp()`` are deprecated
    since Python 3.12; use timezone-aware ``datetime.now(timezone.utc)``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("utcnow", "utcfromtimestamp"):
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  datetime.{node.attr}()")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "datetime"
                and any(a.name in ("utcnow", "utcfromtimestamp") for a in node.names)
            ):
                violations.append(
                    f"  {path.relative_to(TESTS)}:{node.lineno}  from datetime import {node.names[0].name}"
                )
    assert not violations, (
        f"Found {len(violations)} deprecated utcnow()/utcfromtimestamp() usage(s).\n"
        "Use timezone-aware datetime.now(datetime.timezone.utc).\n" + "\n".join(violations)
    )


def test_no_naive_datetime_now():
    """``datetime.now()`` with no timezone argument produces a *naive*
    timestamp. When that value is fed into a ``DateTime(timezone=True)``
    column, a ``pydantic`` aware-datetime field, or any code that later
    compares against a tz-aware timestamp, the comparison is undefined —
    Python raises ``TypeError`` on aware/naive comparison, or worse the two
    silently disagree around UTC. Test fixtures should always pin the zone
    explicitly: ``datetime.now(UTC)``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)

        def _is_datetime_receiver(value: ast.AST) -> bool:
            # datetime.now()  (from datetime import datetime)
            return (
                (isinstance(value, ast.Name) and value.id == "datetime")
                # datetime.datetime.now()  (import datetime)
                or (
                    isinstance(value, ast.Attribute)
                    and value.attr == "datetime"
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "datetime"
                )
            )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "now"):
                continue
            if node.args or node.keywords:
                continue
            if not _is_datetime_receiver(func.value):
                continue
            violations.append(f"  {rel}:{node.lineno}  datetime.now() with no timezone (naive datetime)")
    assert not violations, (
        f"Found {len(violations)} naive datetime.now() call(s) (no timezone argument).\n"
        "Use timezone-aware datetime.now(UTC) so the value is\n"
        "comparable with DateTime(timezone=True) columns and aware datetimes.\n" + "\n".join(violations)
    )


def test_no_boolean_literal_equality():
    """``x == True`` / ``x == False`` rely on int/bool coercion (SQLite stores
    BOOLEAN as INTEGER) and trip ruff E712; prefer ``is True``/``is False``
    with the value coerced to a real bool, or compare against ``1``/``0``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if len(node.ops) != 1:
                continue
            op = node.ops[0]
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and (comp.value is True or comp.value is False):
                    op_name = "==" if isinstance(op, ast.Eq) else "!="
                    violations.append(
                        f"  {path.relative_to(TESTS)}:{node.lineno}  compares value {op_name} {comp.value!r}"
                    )
    assert not violations, (
        f"Found {len(violations)} boolean-literal comparison(s).\n"
        "Prefer 'is True'/'is False' over '== True'/'== False'.\n" + "\n".join(violations)
    )


def test_no_stray_print_in_test_code():
    """``print()`` calls in test modules pollute CI logs and are usually
    leftover debug output. (Load/benchmark harnesses are excluded.)"""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  print(...)")
    assert not violations, (
        f"Found {len(violations)} stray print() call(s) in test code.\n"
        "Remove debug prints or route diagnostics through logging.\n" + "\n".join(violations)
    )


def test_no_dead_fixtures():
    """pytest only instantiates fixtures on demand, so a fixture that no test
    (or other fixture) ever requests is unreachable setup code. It inflates
    the suite, adds per-run collection overhead, and misleads readers into
    believing a capability is covered — its body may already be broken without
    anyone noticing. A fixture counts as used when its name appears as a test
    parameter, an attribute, inside ``@pytest.mark.usefixtures(...)`` /
    ``request.getfixturevalue(...)`` strings, or via the conformance-fixture
    registry; ``autouse=True`` fixtures are legitimately unreferenced."""
    used_names: dict[str, int] = {}
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names[node.id] = used_names.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute):
                used_names[node.attr] = used_names.get(node.attr, 0) + 1
            elif isinstance(node, ast.arg):
                used_names[node.arg] = used_names.get(node.arg, 0) + 1
        for token in re.findall(r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']', path.read_text(encoding="utf-8")):
            used_names[token] = used_names.get(token, 0) + 1

    def _decorator_autouse(dec: ast.AST) -> bool:
        if not isinstance(dec, ast.Call):
            return False
        return any(
            kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in dec.keywords
        )

    violations: list[str] = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_decorator_name(d) == "fixture" for d in node.decorator_list):
                continue
            if any(_decorator_autouse(d) for d in node.decorator_list):
                continue
            if used_names.get(node.name, 0):
                continue
            violations.append(
                f"  {path.relative_to(TESTS)}:{node.lineno}  @pytest.fixture {node.name}()"
                " — never requested by any test"
            )
    assert not violations, (
        f"Found {len(violations)} fixture(s) that no test requests.\n"
        "pytest never instantiates an unrequested fixture, so its body is dead code.\n"
        "Remove it, or wire it up (request it / autouse=True) so it does real work.\n" + "\n".join(violations)
    )


def test_no_len_equals_zero_assertions():
    """``assert len(x) == 0`` should be ``assert not x`` — every sized container
    is falsy exactly when it is empty, so the explicit length comparison adds
    noise and trips ruff SIM101 (flake8-simplify, not enabled in ruff.toml).
    The lens only flags operands whose type is statically a container that
    cannot override truthiness: attribute access, subscript, call, or literal.
    A bare ``len(name) == 0`` is left alone because the name may bind a custom
    object (``__bool__``) or a non-falsy sized type such as a numpy array."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            op = node.ops[0]
            if not isinstance(op, ast.Eq):
                continue
            sides = [(node.left, node.comparators[0]), (node.comparators[0], node.left)]
            for lhs, rhs in sides:
                if not (isinstance(rhs, ast.Constant) and rhs.value == 0):
                    continue
                if not (isinstance(lhs, ast.Call) and isinstance(lhs.func, ast.Name) and lhs.func.id == "len"):
                    continue
                if not lhs.args:
                    continue
                operand = lhs.args[0]
                if isinstance(operand, ast.Name):
                    continue
                if not isinstance(operand, (ast.Attribute, ast.Subscript, ast.Call, ast.List, ast.Dict, ast.Tuple)):
                    continue
                if any(part in EXCLUDED_PACKAGES for part in path.parts):
                    continue
                violations.append(f"  {rel}:{node.lineno}  assert len(...) == 0 — prefer 'assert not ...'")
    assert not violations, (
        f"Found {len(violations)} 'assert len(...) == 0' assertion(s).\n"
        "Sized containers are falsy when empty; write 'assert not <expr>' instead.\n" + "\n".join(violations)
    )


def test_no_len_gt_zero_assertions():
    """``assert len(x) > 0`` should be ``assert x`` — the non-emptiness mirror
    of the ``len(x) == 0`` lens above. Every sized container is truthy exactly
    when it is non-empty, so the explicit length comparison adds noise (and
    trips ruff SIM101 when flake8-simplify is enabled). For the same reason as
    the ``len(x) == 0`` lens, only operands whose type is statically a
    container are flagged (attribute access, subscript, call, or await); a
    bare ``len(name) > 0`` is left alone because the name may bind a
    non-falsy sized type such as a numpy array."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            op = test.ops[0]
            lhs, rhs = test.left, test.comparators[0]
            if not (isinstance(lhs, ast.Call) and isinstance(lhs.func, ast.Name) and lhs.func.id == "len"):
                continue
            if not lhs.args:
                continue
            if not isinstance(rhs, ast.Constant):
                continue
            matches = (
                (isinstance(op, ast.Gt) and rhs.value == 0)
                or (isinstance(op, ast.GtE) and rhs.value == 1)
                or (isinstance(op, ast.NotEq) and rhs.value == 0)
            )
            if not matches:
                continue
            operand = lhs.args[0]
            if isinstance(operand, ast.Name):
                continue
            if not isinstance(operand, (ast.Attribute, ast.Subscript, ast.Call, ast.Await)):
                continue
            violations.append(f"  {rel}:{node.lineno}  assert len(...) > 0 — prefer 'assert ...'")
    assert not violations, (
        f"Found {len(violations)} 'assert len(...) > 0' assertion(s).\n"
        "Sized containers are truthy when non-empty; write 'assert <expr>' instead.\n" + "\n".join(violations)
    )


def test_no_empty_container_literal_equality():
    """``assert x == []`` / ``assert x == {}`` compare a value against an empty
    container literal — the equality-based twin of the ``len(x) == 0`` idiom.
    An empty list/dict is falsy, so ``assert not x`` reads the same intent
    with less noise and no literal-type coupling. The ``!=`` mirror
    (``assert x != []``) is the empty-container twin of ``len(x) > 0`` and
    should read ``assert x``. Operands whose type is statically a container
    (attribute access, subscript, call, or await) are flagged; a bare name is
    left alone because it may bind a ``__bool__``- or ``__eq__``-overloading
    object whose emptiness is not ``not``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            if not isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
                continue
            sides = [(test.left, test.comparators[0]), (test.comparators[0], test.left)]
            for operand, literal in sides:
                empty_literal = (isinstance(literal, ast.List) and not literal.elts) or (
                    isinstance(literal, ast.Dict) and not literal.keys
                )
                if not empty_literal:
                    continue
                if isinstance(operand, ast.Name):
                    continue
                if not isinstance(operand, (ast.Attribute, ast.Subscript, ast.Call, ast.Await)):
                    continue
                op_name = "==" if isinstance(test.ops[0], ast.Eq) else "!="
                prefer = "assert not ..." if isinstance(test.ops[0], ast.Eq) else "assert ..."
                violations.append(
                    f"  {rel}:{node.lineno}  asserts value {op_name} {'[]' if isinstance(literal, ast.List) else '{}'} "
                    f"— prefer '{prefer}'"
                )
                break
    assert not violations, (
        f"Found {len(violations)} empty-container literal comparison(s).\n"
        "An empty list/dict is falsy; write 'assert not <expr>' instead of "
        "'assert <expr> == []/{}' and 'assert <expr>' instead of 'assert <expr> != []/{}'.\n" + "\n".join(violations)
    )


def test_no_empty_string_equality():
    """``assert x == ""`` / ``assert x != ""`` compare a value against an empty
    string literal — the string twin of the empty-container lens above. An
    empty string is falsy, so ``assert x == ""`` should read ``assert not x``
    and ``assert x != ""`` should read ``assert x`` — the same intent with less
    noise and no literal-type coupling. Operands whose type is statically a
    container (attribute access, subscript, call, or await) are flagged; a bare
    name is left alone because it may bind ``None`` or a non-str object whose
    emptiness is not ``not``. A ``.get(...)`` lookup is left alone for the same
    reason: it returns ``None`` for a missing key, and ``""`` vs ``None`` is a
    meaningful distinction for headers/config/API fields that truthiness
    silently conflates."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            if not isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
                continue
            sides = [(test.left, test.comparators[0]), (test.comparators[0], test.left)]
            for operand, literal in sides:
                if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str) and literal.value == ""):
                    continue
                if isinstance(operand, ast.Name):
                    continue
                if (
                    isinstance(operand, ast.Call)
                    and isinstance(operand.func, ast.Attribute)
                    and operand.func.attr == "get"
                ):
                    continue
                if not isinstance(operand, (ast.Attribute, ast.Subscript, ast.Call, ast.Await)):
                    continue
                op_name = "==" if isinstance(test.ops[0], ast.Eq) else "!="
                prefer = "assert not ..." if isinstance(test.ops[0], ast.Eq) else "assert ..."
                violations.append(f"  {rel}:{node.lineno}  asserts value {op_name} '' — prefer '{prefer}'")
                break
    assert not violations, (
        f"Found {len(violations)} empty-string comparison(s).\n"
        "An empty string is falsy; write 'assert not <expr>' instead of "
        "'assert <expr> == \"\"' and 'assert <expr>' instead of 'assert <expr> != \"\"'.\n" + "\n".join(violations)
    )


_EMPTY_TUPLE_OPERANDS = (ast.Attribute, ast.Subscript, ast.Call, ast.Await)
"""Operand node types the empty-tuple lens flags. A bare name is left alone
because it may bind ``None`` or a non-tuple object, and a ``.get(...)`` lookup
is left alone because it returns ``None`` for a missing key — ``()`` vs
``None`` is a meaningful distinction (empty result vs. no result) that
truthiness silently conflates."""


def _empty_tuple_comparisons(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` that compares a
    value against an empty tuple literal with ``==``/``!=``."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
            continue
        sides = [(test.left, test.comparators[0]), (test.comparators[0], test.left)]
        for operand, literal in sides:
            if not (isinstance(literal, ast.Tuple) and not literal.elts):
                continue
            if isinstance(operand, ast.Name):
                continue
            if isinstance(operand, ast.Call) and isinstance(operand.func, ast.Attribute) and operand.func.attr == "get":
                continue
            if not isinstance(operand, _EMPTY_TUPLE_OPERANDS):
                continue
            op_name = "==" if isinstance(test.ops[0], ast.Eq) else "!="
            prefer = "assert not ..." if isinstance(test.ops[0], ast.Eq) else "assert ..."
            found.append((node.lineno, f"asserts value {op_name} () — prefer '{prefer}'"))
            break
    return found


def test_no_empty_tuple_equality():
    """``assert x == ()`` / ``assert x != ()`` compare a value against an empty
    tuple literal — the tuple twin of the empty-container lens above. An empty
    tuple is falsy, so ``assert x == ()`` should read ``assert not x`` and
    ``assert x != ()`` should read ``assert x`` — the same intent with less
    noise and no literal-type coupling. Unlike the ``is``/``is not`` identity
    lens (which deliberately leaves tuple literals alone because ``()`` is
    interned), equality against ``()`` has no identity wrinkle. Operands whose
    type is statically a container (attribute access, subscript, call, or
    await) are flagged; a bare name is left alone because it may bind ``None``
    or a non-tuple object, and a ``.get(...)`` lookup is left alone because it
    returns ``None`` for a missing key — ``()`` vs ``None`` is a meaningful
    distinction for APIs that signal "empty result" vs "no result"."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_tuple_comparisons(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} empty-tuple comparison(s).\n"
        "An empty tuple is falsy; write 'assert not <expr>' instead of "
        "'assert <expr> == ()' and 'assert <expr>' instead of 'assert <expr> != ()'.\n" + "\n".join(violations)
    )


def test_empty_tuple_lens_flags_empty_tuple():
    """Synthetic positive/negative control for the empty-tuple lens: must flag
    ``== ()``/``!= ()`` on attribute/subscript/call/await operands (either
    operand order) and ignore ``is ()``, bare names, ``.get(...)``, non-empty
    tuple literals, and list/dict literals."""
    positive_sources = [
        "def test_foo():\n    assert result.items == ()\n",
        "def test_foo():\n    assert result['items'] != ()\n",
        "def test_foo():\n    assert fetch_items() == ()\n",
        "def test_foo():\n    assert await fetch_items() == ()\n",
        "def test_foo():\n    assert () != result.items\n",
        "def test_foo():\n    assert result.items[0] == ()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_tuple_comparisons(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x == ()\n",
        "def test_foo():\n    assert x is ()\n",
        "def test_foo():\n    assert config.get('items') == ()\n",
        "def test_foo():\n    assert result.items == (1, 2)\n",
        "def test_foo():\n    assert result.items == []\n",
        "def test_foo():\n    assert result.items == {}\n",
        "def test_foo():\n    assert () == ()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_tuple_comparisons(tree), f"lens should NOT flag:\n{source}"


_EMPTY_BUILTIN_CALLS = frozenset({"list", "dict", "set", "tuple", "bytes", "bytearray", "frozenset"})
"""Zero-argument builtin calls that always produce an empty (falsy) container.

The literal-based empty-container lens catches ``[]``/``{}``/``""``/``()`` but
cannot see ``set()``/``list()`` — those are ``ast.Call`` nodes, not literals.
This lens is their call-based twin."""


def _empty_builtin_call_comparisons(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` that compares a
    value against an empty container produced by a zero-argument builtin call
    (``list()``/``dict()``/``set()``/``tuple()``/``bytes()``/``bytearray()``/
    ``frozenset()``) with ``==``/``!=``."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
            continue
        sides = [(test.left, test.comparators[0]), (test.comparators[0], test.left)]
        for operand, literal in sides:
            if not (
                isinstance(literal, ast.Call)
                and isinstance(literal.func, ast.Name)
                and literal.func.id in _EMPTY_BUILTIN_CALLS
                and not literal.args
                and not literal.keywords
            ):
                continue
            if isinstance(operand, ast.Name):
                continue
            if isinstance(operand, ast.Call) and isinstance(operand.func, ast.Attribute) and operand.func.attr == "get":
                continue
            if not isinstance(operand, (ast.Attribute, ast.Subscript, ast.Call, ast.Await)):
                continue
            op_name = "==" if isinstance(test.ops[0], ast.Eq) else "!="
            prefer = "assert not ..." if isinstance(test.ops[0], ast.Eq) else "assert ..."
            found.append((node.lineno, f"asserts value {op_name} {literal.func.id}() — prefer '{prefer}'"))
            break
    return found


def test_no_empty_builtin_call_equality():
    """``assert x == set()`` / ``assert x == list()`` compare a value against
    an empty container produced by a zero-argument builtin call — the
    call-based twin of the ``== []``/``== {}`` literal lens. Every such
    builtin returns a falsy container, so ``assert x == set()`` should read
    ``assert not x`` and ``assert x != set()`` should read ``assert x`` — the
    same intent with less noise and no literal-type coupling. Operands whose
    type is statically a container (attribute access, subscript, call, or
    await) are flagged; a bare name is left alone because it may bind ``None``
    or a ``__bool__``-/``__eq__``-overloading object whose emptiness is not
    ``not``, and a ``.get(...)`` lookup is left alone because it returns
    ``None`` for a missing key — ``set()`` vs ``None`` is a meaningful
    distinction."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_builtin_call_comparisons(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} empty-builtin-call comparison(s).\n"
        "An empty list()/dict()/set()/tuple()/bytes()/frozenset() is falsy; write "
        "'assert not <expr>' instead of 'assert <expr> == list()/set()' and "
        "'assert <expr>' instead of 'assert <expr> != list()/set()'.\n" + "\n".join(violations)
    )


def test_empty_builtin_call_lens_flags_empty_calls():
    """Synthetic positive/negative control for the empty-builtin-call lens:
    must flag ``== set()``/``!= frozenset()`` on attribute/subscript/call/await
    operands (either operand order) for every supported builtin and ignore bare
    names, ``.get(...)`` lookups, non-empty builtin calls, non-container calls,
    and list/dict literal comparisons."""
    positive_sources = [
        "def test_foo():\n    assert result.items == set()\n",
        "def test_foo():\n    assert result['items'] != frozenset()\n",
        "def test_foo():\n    assert collect_items() == list()\n",
        "def test_foo():\n    assert await load_items() == dict()\n",
        "def test_foo():\n    assert tuple() != result.items\n",
        "def test_foo():\n    assert result.items == bytes()\n",
        "def test_foo():\n    assert result.items == bytearray()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_builtin_call_comparisons(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x == set()\n",
        "def test_foo():\n    assert config.get('items') == set()\n",
        "def test_foo():\n    assert result.items == set([1])\n",
        "def test_foo():\n    assert result.items == frozenset({'a'})\n",
        "def test_foo():\n    assert result.items == []\n",
        "def test_foo():\n    assert result.items == {}\n",
        "def test_foo():\n    assert result.items == len(items)\n",
        "def test_foo():\n    assert result.items == sorted(items)\n",
        "def test_foo():\n    assert result.items == str()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_builtin_call_comparisons(tree), f"lens should NOT flag:\n{source}"


def test_no_precision_fragile_float_equality():
    """``x == 0.1`` style assertions are precision-fragile: most decimal
    fractions have no exact binary representation, so the value under test
    can differ from the literal in the last ulp (e.g. ``0.04 == 0.04`` is not
    guaranteed once the left side is the result of arithmetic or a DB round
    trip). Prefer ``pytest.approx(literal)`` which compares within tolerance.

    The lens only flags literals that are *not* exactly representable as a
    binary float (``0.5``, ``0.25``, ``150.0`` are safe; ``0.1``, ``0.04``,
    ``0.95`` are not), so it targets genuinely fragile comparisons without
    forcing ``approx`` on trivial cases.
    """
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for left, op, right in zip([node.left, *node.comparators[:-1]], node.ops, node.comparators, strict=True):
                if not isinstance(op, ast.Eq):
                    continue
                for side in (left, right):
                    if not isinstance(side, ast.Constant) or not isinstance(side.value, float):
                        continue
                    other = right if side is left else left
                    if isinstance(other, ast.Constant) and isinstance(other.value, float):
                        continue
                    if Fraction(side.value) == Fraction(str(side.value)):
                        continue
                    violations.append(
                        f"  {path.relative_to(TESTS)}:{node.lineno}  compares "
                        f"value == {side.value!r} (no exact binary representation)"
                    )
    assert not violations, (
        f"Found {len(violations)} precision-fragile float comparison(s).\n"
        "Use pytest.approx(<literal>) instead of == against a non-representable float literal.\n"
        + "\n".join(violations)
    )


def test_no_tautological_len_bounds():
    """``len(x) >= 0`` and friends are dead assertions: ``len()`` never returns
    a negative number, so the comparison can never change outcome. The assert
    is either guaranteed to pass (``>= 0``, ``> -1``, ``!= -N``) or guaranteed
    to fail (``< 0``, ``<= -1``, ``== -N``) — it reports green regardless of
    behaviour, or unconditionally breaks the suite. Assert the condition you
    actually mean (``assert x`` for non-empty, ``assert not x`` for empty), or
    drop the check entirely. Both operand orders are covered (``0 <= len(x)``
    is the same tautology as ``len(x) >= 0``)."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)

        def _is_len(expr: ast.AST) -> bool:
            return (
                isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "len" and expr.args
            )

        def _int_value(expr: ast.AST) -> int | None:
            if isinstance(expr, ast.Constant) and isinstance(expr.value, int):
                return expr.value
            if (
                isinstance(expr, ast.UnaryOp)
                and isinstance(expr.op, ast.USub)
                and isinstance(expr.operand, ast.Constant)
                and isinstance(expr.operand.value, int)
            ):
                return -expr.operand.value
            return None

        def _verdict(op: type, value: int) -> str | None:
            if op is ast.GtE and value <= 0:
                return "always PASSES"
            if op is ast.Gt and value < 0:
                return "always PASSES"
            if op is ast.Lt and value <= 0:
                return "always FAILS"
            if op is ast.LtE and value < 0:
                return "always FAILS"
            if op is ast.Eq and value < 0:
                return "always FAILS"
            if op is ast.NotEq and value < 0:
                return "always PASSES"
            return None

        mirror = {
            ast.GtE: ast.LtE,
            ast.LtE: ast.GtE,
            ast.Gt: ast.Lt,
            ast.Lt: ast.Gt,
            ast.Eq: ast.Eq,
            ast.NotEq: ast.NotEq,
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            op = type(node.ops[0])
            mirrored = mirror.get(op)
            if mirrored is None:
                continue
            pairs = [
                (node.left, node.comparators[0], op),
                (node.comparators[0], node.left, mirrored),
            ]
            for len_side, const_side, effective in pairs:
                if not _is_len(len_side):
                    continue
                value = _int_value(const_side)
                if value is None:
                    continue
                verdict = _verdict(effective, value)
                if verdict is None:
                    continue
                op_name = {
                    ast.GtE: ">=",
                    ast.Gt: ">",
                    ast.Lt: "<",
                    ast.LtE: "<=",
                    ast.Eq: "==",
                    ast.NotEq: "!=",
                }.get(effective, "?")
                violations.append(
                    f"  {rel}:{node.lineno}  assert len(...) {op_name} {value} — {verdict} (len() is never negative)"
                )
    assert not violations, (
        f"Found {len(violations)} tautological len() comparison(s).\n"
        "len() never returns a negative number, so the bound can never be exercised.\n"
        "Assert the real condition (assert x / assert not x) or drop the dead check.\n" + "\n".join(violations)
    )


def test_no_manual_raises_pattern():
    """A hand-rolled ``try: <call>; raise AssertionError(...) except X: pass``
    is a fragile substitute for ``pytest.raises``: the success path is guarded
    only by the ``raise`` line (which is skipped if the code under test is
    correct), and the ``except: pass`` swallows the failure. It also loses the
    assertion-context reporting that ``pytest.raises`` gives you. Prefer::

        with pytest.raises(X):
            <call>
    """
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            raises_assert = any(
                isinstance(stmt, ast.Raise)
                and stmt.exc is not None
                and isinstance(stmt.exc, ast.Call)
                and isinstance(stmt.exc.func, ast.Name)
                and stmt.exc.func.id == "AssertionError"
                for stmt in node.body
            )
            swallows = any(
                len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass) for handler in node.handlers
            )
            if raises_assert and swallows:
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  try/raise AssertionError/except: pass")
    assert not violations, (
        f"Found {len(violations)} hand-rolled raises pattern(s).\n"
        "Replace try/raise AssertionError/except: pass with `with pytest.raises(...):`.\n" + "\n".join(violations)
    )


def test_no_assert_inside_except():
    """An ``assert`` nested inside an ``except`` handler replaces the original
    exception with a bare ``AssertionError`` when it fires, discarding the
    traceback that explains *why* the code under test raised. Capture the
    exception with ``pytest.raises(...) as exc_info`` and assert on
    ``exc_info.value`` after the ``with`` block, or record the error and assert
    in a separate step."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assert):
                    violations.append(f"  {path.relative_to(TESTS)}:{sub.lineno}  assert inside except handler")
    assert not violations, (
        f"Found {len(violations)} assertion(s) inside except handler(s).\n"
        "Use pytest.raises(...) as exc_info and assert on exc_info.value outside the handler.\n" + "\n".join(violations)
    )


_RAISES_CONTEXT_NAMES = frozenset(
    {
        "raises",
        "assert_raises",
        "assert_does_not_raise",
        "rejects",
        "raises_match",
        "warns",
        "warns_match",
        "deprecated_call",
    }
)
"""``with`` context-manager names that count as verification of a no-op test."""

_FAIL_CALL_NAMES = frozenset({"fail", "skip", "xfail"})
"""Calls that report test outcome directly (other than ``assert``)."""

_SCHEMATISEST_SELF_VALIDATING = frozenset(
    {"call_and_validate", "call_and_validate_examples", "call_and_validate_frozen"}
)
"""Schemathesis case methods that validate every generated response internally."""


def _noop_lens_verifies(node: ast.AST) -> bool:
    """True if ``node`` contains anything that verifies behavior (any assert,
    raises-context, fail/skip/xfail call, or call to an assert/self-validating
    helper). Nested defs/classes are skipped — they define helpers, not the
    test body itself — unless the test body references the helper, in which
    case its asserts actually run and count. A helper that is defined but never
    called cannot report a broken code path, so an assert trapped inside it
    does not make the test a verifier."""
    invoked = _names_referenced_outside_nested_defs(node)
    stack: list[tuple[ast.AST, bool]] = [(node, False)]
    while stack:
        sub, in_invoked_class = stack.pop()
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.Raise):
            return True
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                ctx = item.context_expr
                if not isinstance(ctx, ast.Call):
                    continue
                f = ctx.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                if name in _RAISES_CONTEXT_NAMES:
                    return True
        if isinstance(sub, ast.Call):
            f = sub.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name in _FAIL_CALL_NAMES or name in _SCHEMATISEST_SELF_VALIDATING:
                return True
            if name and "assert" in name:
                return True
        if (
            isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            and sub is not node
            and not in_invoked_class
            and sub.name not in invoked
        ):
            continue
        if isinstance(sub, ast.ClassDef) and sub is not node:
            if sub.name not in invoked:
                continue
            in_invoked_class = True
        stack.extend((child, in_invoked_class) for child in ast.iter_child_nodes(sub))
    return False


def _names_referenced_outside_nested_defs(node: ast.AST) -> set[str]:
    """Names referenced in the test body excluding the bodies of nested
    defs/classes — used to tell whether a nested helper is actually invoked."""
    names: set[str] = set()
    stack = [node]
    while stack:
        sub = stack.pop()
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub is not node:
            continue
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        stack.extend(ast.iter_child_nodes(sub))
    return names


def test_no_noop_test_functions():
    """A ``test_*`` function whose body contains no verification at all is a
    no-op test: it reports green even when the code under test is completely
    broken, as long as no exception escapes. Smoke tests that merely 'call the
    code' must assert something about the outcome (or wrap the call in
    ``pytest.raises``) — otherwise a silent regression slips through."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(_decorator_name(d) == "fixture" for d in node.decorator_list):
                continue
            if not (node.name.startswith("test_") or any(_decorator_name(d) == "mark" for d in node.decorator_list)):
                continue
            if _noop_lens_verifies(node):
                continue
            violations.append(f"  {rel}:{node.lineno}  {node.name}() — no assertion or raises context in body")
    assert not violations, (
        f"Found {len(violations)} no-op test function(s) that never verify anything.\n"
        "Add an assertion on the outcome, or wrap the call in pytest.raises(...) if it must raise.\n"
        + "\n".join(violations)
    )


def test_noop_lens_recognizes_verification_patterns():
    """The no-op lens must count every legitimate pytest verification pattern
    as verification — otherwise adding a correct test trips the lens. This
    covers ``with``/``async with`` raises-contexts, warning contexts, and
    direct outcome calls. Asserts inside nested helpers count only when the
    test body actually invokes the helper; an assert trapped in a never-called
    helper does not verify anything."""
    verifying_sources = [
        "def test_foo():\n    assert foo() == 1\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        foo()\n",
        "def test_foo():\n    with pytest.warns(UserWarning):\n        foo()\n",
        "def test_foo():\n    with pytest.deprecated_call():\n        foo()\n",
        "async def test_foo():\n    async with pytest.raises(ValueError):\n        await foo()\n",
        "async def test_foo():\n    async with pytest.warns(UserWarning):\n        await foo()\n",
        "def test_foo():\n    pytest.fail('boom')\n",
        "def test_foo():\n    def helper():\n        assert foo() == 1\n    helper()\n",
        (
            "def test_foo():\n"
            "    class Helper:\n"
            "        def check(self):\n"
            "            assert foo() == 1\n"
            "    Helper().check()\n"
        ),
    ]
    for source in verifying_sources:
        tree = ast.parse(source)
        assert _noop_lens_verifies(tree.body[0]), f"lens should count as verifying:\n{source}"

    non_verifying_sources = [
        "def test_foo():\n    foo()\n",
        "def test_foo():\n    def helper():\n        assert foo() == 1\n    foo()\n",
        "def test_foo():\n    class Helper:\n        def check(self):\n            assert foo() == 1\n",
    ]
    for source in non_verifying_sources:
        tree = ast.parse(source)
        assert not _noop_lens_verifies(tree.body[0]), f"lens should NOT count as verifying:\n{source}"


_SELF_COMPARISON_OPS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
"""Comparison operators where ``<operand> OP <identical operand>`` is a
tautology in ordinary Python semantics: ``x == x``/``x <= x``/``x >= x``/
``x is x`` always PASS, while ``x != x``/``x < x``/``x > x``/``x is not x``
always FAIL, no matter what ``x`` evaluates to. IEEE-754 NaN is the one
exception (``float('nan') != float('nan')`` is True), so the lens cannot
claim the outcome is literally constant — what makes a self-comparison dead
code is that it can never exercise distinct values."""


def _self_comparison_tautologies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion that compares an
    operand with a syntactically identical copy of itself."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], _SELF_COMPARISON_OPS):
            continue
        left, right = node.left, node.comparators[0]
        if not isinstance(left, (ast.Name, ast.Attribute, ast.Subscript)):
            continue
        if ast.dump(left) != ast.dump(right):
            continue
        op_name = node.ops[0].__class__.__name__
        expr = ast.unparse(left)
        found.append(
            (node.lineno, f"compares {expr} {op_name} {expr} — identical operands can never exercise distinct values")
        )
    return found


def test_no_self_comparison_tautology():
    """An assertion comparing a value with *itself* — ``assert x == x``,
    ``assert result.value != result.value``, ``assert row['key'] is row['key']``
    — is a tautology in ordinary Python semantics: it can never exercise the
    behaviour under test, yet it reports green (or, for ``!=``/``<``/``>``/
    ``is not``, red) no matter how broken the code under test is. IEEE-754
    NaN is the one caveat (``float('nan') == float('nan')`` is False), so the
    lens targets the deeper invariant: identical operands can never exercise
    distinct values. These are almost always copy-paste or leftover-debugging
    artefacts.

    The lens only flags syntactically identical operands whose type is a
    variable, attribute path, or subscript — expressions that re-evaluate to
    the same object. ``Call`` operands are deliberately NOT flagged: ``assert
    signal_fingerprint(a) == signal_fingerprint(a)`` is a legitimate
    determinism/stability check of a (pure) function, so the lens cannot know
    a call is redundant without interprocedural analysis.
    """
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _self_comparison_tautologies(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} self-comparison tautolog(ies).\n"
        "Comparing a value with itself can never exercise distinct values; it is dead code.\n"
        "Assert against the expected value instead: 'assert x == <expected>'.\n" + "\n".join(violations)
    )


def test_self_comparison_lens_flags_tautologies():
    """Synthetic positive/negative control for the self-comparison lens,
    mirroring the no-op lens's verification-pattern test: the lens must flag
    every syntactically identical self-comparison (variables, attribute
    paths, subscripts) and ignore comparisons that could involve distinct
    values or side-effecting calls."""
    positive_sources = [
        "def test_foo():\n    assert x == x\n",
        "def test_foo():\n    assert result.value != result.value\n",
        "def test_foo():\n    assert row['key'] is row['key']\n",
        "def test_foo():\n    assert a.b.c <= a.b.c\n",
        "def test_foo():\n    assert items[0] > items[0]\n",
        "def test_foo():\n    assert x is not x\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _self_comparison_tautologies(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x == y\n",
        "def test_foo():\n    assert x != y\n",
        "def test_foo():\n    assert row['a'] is row['b']\n",
        "def test_foo():\n    assert x == 1\n",
        "def test_foo():\n    assert len(a) != len(a)\n",
        "def test_foo():\n    assert signal_fingerprint(a) == signal_fingerprint(a)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _self_comparison_tautologies(tree), f"lens should NOT flag:\n{source}"


_IDENTITY_LITERAL_CONTAINERS = (ast.List, ast.Dict, ast.Set)
"""Mutable container literal node types. A list/dict/set literal is freshly
allocated on every evaluation, so ``is`` identity against one can never hold
(and ``is not`` against one always holds). Tuples are deliberately excluded:
``()`` is interned and non-empty tuple literals are compiled as constants, so
identity against a tuple literal *can* legitimately hold."""


def _identity_literal_tautologies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``is``/``is not`` comparison
    whose operand is a mutable container literal (list/dict/set)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op = node.ops[0]
        if not isinstance(op, (ast.Is, ast.IsNot)):
            continue
        for side in (node.left, *node.comparators):
            if not isinstance(side, _IDENTITY_LITERAL_CONTAINERS):
                continue
            op_name = "is" if isinstance(op, ast.Is) else "is not"
            kind = type(side).__name__.lower()
            verdict = "always FAILS" if isinstance(op, ast.Is) else "always PASSES"
            found.append(
                (
                    node.lineno,
                    f"compares value {op_name} {kind} literal — freshly allocated each time, "
                    f"{verdict} (use ==/!= for value equality)",
                )
            )
            break
    return found


def test_no_identity_comparison_with_container_literal():
    """``assert x is []`` / ``assert result is {}`` / ``assert x is not {1}``
    compare *identity* against a mutable container literal. The literal is
    freshly allocated every time the expression runs, so the comparison can
    never hold (``is`` → always FAILS) or can never fail (``is not`` → always
    PASSES) — dead code that reports red or green regardless of behaviour.
    Python 3.8+ even emits a SyntaxWarning for it, and what the assertion
    actually means is value equality (``==``/``!=``).

    Tuples are deliberately excluded: ``()`` is interned and non-empty tuple
    literals are compiled as constants, so identity against them can
    legitimately hold."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _identity_literal_tautologies(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} identity comparison(s) against container literal(s).\n"
        "A list/dict/set literal is freshly allocated on every evaluation, so 'is'/'is not' "
        "against it is dead code.\n"
        "Use value equality (== / !=) instead.\n" + "\n".join(violations)
    )


def test_identity_literal_lens_flags_tautologies():
    """Synthetic positive/negative control for the identity-vs-container-literal
    lens: must flag ``is``/``is not`` against list/dict/set literals (either
    operand order) and ignore identity against variables, calls, non-mutable
    types, and equality comparisons."""
    positive_sources = [
        "def test_foo():\n    assert x is []\n",
        "def test_foo():\n    assert x is not []\n",
        "def test_foo():\n    assert x is {}\n",
        "def test_foo():\n    assert x is not {1, 2}\n",
        "def test_foo():\n    assert {} is x\n",
        "def test_foo():\n    assert result.value is [1, 2]\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _identity_literal_tautologies(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x is y\n",
        "def test_foo():\n    assert x is None\n",
        "def test_foo():\n    assert x == []\n",
        "def test_foo():\n    assert x is ()\n",
        "def test_foo():\n    assert x is (1, 2)\n",
        "def test_foo():\n    assert x is make_list()\n",
        "def test_foo():\n    assert x is 'abc'\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _identity_literal_tautologies(tree), f"lens should NOT flag:\n{source}"


_MOCK_CALL_INTROSPECTION = frozenset({"calls", "call_args", "call_args_list", "call_count"})
"""Mock attributes that inspect the recorded calls after the fact. Accessing
``<mock>.calls[0]``/``<mock>.call_args[0]`` fails loudly (``IndexError``/
``AttributeError``) when the call never happened, so a ``.called`` assertion
immediately before such an access is dead — it duplicates the check the
introspection access already performs, and can silently drift out of sync with
what the test actually inspects."""


def _redundant_called_assertions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert <mock>.called`` that
    is immediately followed by an introspection access on the same mock."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for i, stmt in enumerate(node.body[:-1]):
            test = stmt.test if isinstance(stmt, ast.Assert) else None
            if not isinstance(test, ast.Attribute) or test.attr != "called":
                continue
            base = test.value
            if not isinstance(base, (ast.Attribute, ast.Name)):
                continue
            nxt = node.body[i + 1]
            nxt_attrs = [
                sub for sub in ast.walk(nxt) if isinstance(sub, ast.Attribute) and sub.attr in _MOCK_CALL_INTROSPECTION
            ]
            if any(ast.dump(sub.value) == ast.dump(base) for sub in nxt_attrs):
                found.append(
                    (
                        stmt.lineno,
                        f"assert {ast.unparse(base)}.called is redundant — the "
                        f"following {ast.unparse(base)}.<calls>/<call_args> access already "
                        "fails loudly when no call was recorded",
                    )
                )
    return found


def test_no_redundant_called_assertions():
    """``assert <mock>.called`` immediately before the test inspects the same
    mock's recorded calls (``<mock>.calls[0]``, ``<mock>.call_args[0]``, ...) is
    dead code: the introspection access that follows fails loudly — an
    ``IndexError`` on ``calls[0]``/``call_args[0]``, an empty ``call_args_list``
    that makes later ``in``-style assertions fail — if the call never happened.
    The ``.called`` assert therefore duplicates the very check the next line
    performs, and because the two can drift apart (asserting one mock's
    ``.called`` while inspecting a *different* call path), it quietly gives a
    false sense of rigour. Drop the assert and keep the introspection access.
    The lens only flags bare ``assert <x>.called`` used positively; a negated
    ``assert not <x>.called`` is a genuine no-call check and is left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _redundant_called_assertions(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} redundant 'assert <mock>.called' assertion(s).\n"
        "The immediately following <mock>.calls[0]/<mock>.call_args access already fails "
        "loudly when the call never happened, so the .called assert is dead code.\n"
        "Drop the redundant assert and keep the introspection access.\n" + "\n".join(violations)
    )


def test_redundant_called_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the redundant-``.called`` lens,
    mirroring the identity-literal lens pattern: it must flag a bare
    ``assert <mock>.called`` that is immediately followed by a recorded-calls
    access on the same mock, and ignore negated no-call checks, ``.called``
    without a follow-up introspection, and introspection on a different mock."""
    positive_sources = [
        "def test_foo():\n    assert route.called\n    assert route.calls[0].request.url.endswith('/x')\n",
        "def test_foo():\n    assert session.add.called\n    row = session.add.call_args.args[0]\n",
        "def test_foo():\n    assert mock.execute.called\n    calls = mock.execute.call_args_list\n",
        "def test_foo():\n    assert response.called\n    response.calls[0]\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _redundant_called_assertions(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert not mock.called\n",
        "def test_foo():\n    assert mock.called\n",
        "def test_foo():\n    assert mock.called\n    mock.other_attr\n",
        "def test_foo():\n    assert mock.called\n    other.calls[0]\n",
        "def test_foo():\n    assert mock.called\n    return\n    mock.calls[0]\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _redundant_called_assertions(tree), f"lens should NOT flag:\n{source}"


def _empty_container_membership_tautologies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``in``/``not in`` comparison
    whose container operand is an empty list/dict/tuple literal.

    An empty container can never contain anything, so ``in`` always FAILS and
    ``not in`` always PASSES regardless of what the other operand evaluates to.
    The case where *both* operands are literals is owned by the literal-
    comparison lens (``assert 1 in []``); a ``Constant`` other-side is skipped
    here so the two lenses do not double-report the same line. ``set()`` is
    deliberately not flagged: it is a call, not a literal, so its emptiness
    cannot be known statically.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], (ast.In, ast.NotIn)):
            continue
        for operand, container in ((test.left, test.comparators[0]), (test.comparators[0], test.left)):
            if not _is_empty_container_literal(container):
                continue
            if isinstance(operand, ast.Constant):
                continue
            op_name = "in" if isinstance(test.ops[0], ast.In) else "not in"
            verdict = "always FAILS" if isinstance(test.ops[0], ast.In) else "always PASSES"
            kind = type(container).__name__.lower()
            found.append(
                (
                    node.lineno,
                    f"asserts value {op_name} {kind} literal — {verdict} (empty container never contains anything)",
                )
            )
            break
    return found


def _is_empty_container_literal(node: ast.AST) -> bool:
    """True when ``node`` is an empty list/dict/tuple literal (``[]``/``{}``/``()``)."""
    if isinstance(node, ast.List):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Tuple):
        return not node.elts
    return False


def test_no_empty_container_membership():
    """``assert x in []`` (or ``{}``/``()``) compares membership against an
    empty container literal — a membership test that can never hold. ``in``
    against an empty container always FAILS and ``not in`` always PASSES, no
    matter what ``x`` evaluates to, so the assertion is dead code either way:
    it reports red (``in``) or green (``not in``) without exercising the code
    under test. This is the membership twin of the empty-container *equality*
    lens (``assert x == []``). ``set()`` is not flagged (a call, not a
    literal), and literal-vs-literal membership is owned by the literal-
    comparison lens."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_container_membership_tautologies(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} membership assertion(s) against an empty container literal.\n"
        "An empty container can never satisfy 'in' (and always satisfies 'not in').\n"
        "Assert the actual membership you mean, or drop the dead check.\n" + "\n".join(violations)
    )


def test_empty_container_membership_lens_flags_impossible_membership():
    """Synthetic positive/negative control for the empty-container membership
    lens: must flag ``in``/``not in`` against an empty ``[]``/``{}``/``()``
    literal (either operand order, non-literal other side) and ignore
    membership against non-empty literals, variables, calls, strings, the
    literal-vs-literal case owned by the literal-comparison lens, and the
    equality/identity twins owned by their own lenses."""
    positive_sources = [
        "def test_foo():\n    assert x in []\n",
        "def test_foo():\n    assert x not in []\n",
        "def test_foo():\n    assert x in {}\n",
        "def test_foo():\n    assert x not in ()\n",
        "def test_foo():\n    assert result.value in []\n",
        "def test_foo():\n    assert [] not in x\n",
        "def test_foo():\n    assert x not in {}\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_container_membership_tautologies(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x in [1, 2]\n",
        "def test_foo():\n    assert x in {1: 'a'}\n",
        "def test_foo():\n    assert x in (1, 2)\n",
        "def test_foo():\n    assert x in some_list\n",
        "def test_foo():\n    assert x not in make_list()\n",
        "def test_foo():\n    assert x in 'abc'\n",
        "def test_foo():\n    assert 1 in []\n",
        "def test_foo():\n    assert 'a' not in {}\n",
        "def test_foo():\n    assert x == []\n",
        "def test_foo():\n    assert x is not []\n",
        "def test_foo():\n    assert x not in set()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_container_membership_tautologies(tree), f"lens should NOT flag:\n{source}"


def _parametrize_argvalue_counts(tree: ast.AST) -> list[tuple[int, int]]:
    """Return ``(lineno, n_cases)`` for every ``@...parametrize`` decorator
    whose ``argvalues`` is a statically-known ``list``/``tuple`` literal. Only
    decorator applications are considered — a bare ``parametrize(...)`` call
    inside a body is not pytest parametrization and belongs to a different
    lens. The parametrize-adjacent lenses filter on ``n_cases`` (``== 0``,
    ``== 1``, ...) so a new lens never re-copies the decorator walk."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if _decorator_name(dec) != "parametrize":
                continue
            if len(dec.args) >= 2:
                argvalues = dec.args[1]
            else:
                argvalues = next((kw.value for kw in dec.keywords if kw.arg == "argvalues"), None)
            if not isinstance(argvalues, (ast.List, ast.Tuple)):
                continue
            found.append((dec.lineno, len(argvalues.elts)))
    return found


def _single_case_parametrize_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize``
    decorator whose ``argvalues`` holds exactly one case. Only decorator
    applications are considered — a bare ``parametrize(...)`` call inside a
    body is not pytest parametrization and belongs to a different lens."""
    return [
        (lineno, "parametrize with a single case in argvalues — collapse to a plain test")
        for lineno, n_cases in _parametrize_argvalue_counts(tree)
        if n_cases == 1
    ]


def test_no_single_value_parametrize():
    """``@pytest.mark.parametrize`` with exactly one case in ``argvalues`` is a
    parametrize that adds nothing: the suite gains no matrix coverage and the
    single case is indistinguishable from an ordinary test body. It is almost
    always a leftover from trimming the case list down, or a parametrize
    introduced before the second case existed — either way the parameter
    plumbing misleads readers into believing multiple cases are exercised.
    Collapse it to a plain test (with the value assigned locally) so the
    parametrize decorator is only used when it actually varies the test."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _single_case_parametrize_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} parametrize decorator(s) with a single case.\n"
        "A single-case parametrize adds no matrix coverage; write the value as a "
        "local variable in an ordinary test.\n" + "\n".join(violations)
    )


def test_single_value_parametrize_lens_flags_redundant_cases():
    """Synthetic positive/negative control for the single-case parametrize
    lens: must flag a single element in ``argvalues`` (list or tuple, declared
    positionally or via ``argvalues=``) and ignore multi-case parametrizes,
    non-parametrize calls, and parametrizes without a statically known case
    list."""
    positive_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', (1,))\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', argvalues=[1])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2])\n"
        "    @pytest.mark.parametrize('y', [3])\n    def test_bar(x, y): pass\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _single_case_parametrize_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', (1, 2))\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', SOME_CASES)\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.skip(reason='x')\n    def test_bar(): pass\n",
        "def test_foo():\n    parametrize('x', [1])\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _single_case_parametrize_violations(tree), f"lens should NOT flag:\n{source}"


def _empty_parametrize_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize``
    decorator whose ``argvalues`` holds zero cases. Only decorator applications
    are considered — a bare ``parametrize(...)`` call inside a body is not
    pytest parametrization and belongs to a different lens."""
    return [
        (lineno, "parametrize with an empty argvalues — the test is collected as zero items and never runs")
        for lineno, n_cases in _parametrize_argvalue_counts(tree)
        if n_cases == 0
    ]


def test_no_empty_parametrize():
    """``@pytest.mark.parametrize`` with zero cases in ``argvalues`` is the
    inverse twin of the single-case lens above: the test is collected as zero
    test items, so its body never executes. pytest emits a collection warning
    (``PytestCollectionWarning: cannot parametrize ... with empty parameter
    set``) but the suite still reports green — a regression the test was
    written to catch slips through silently. It is almost always a leftover
    from deleting the last case, or an ``argvalues`` list produced by code
    that returned nothing. Delete the parametrize (and the test) or supply a
    real case."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_parametrize_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} parametrize decorator(s) with an empty case list.\n"
        "A zero-case parametrize is collected as zero test items — the test never runs, "
        "so its coverage silently disappears. Delete the dead parametrize or supply a real case.\n"
        + "\n".join(violations)
    )


def test_empty_parametrize_lens_flags_never_run_cases():
    """Synthetic positive/negative control for the empty-parametrize lens: must
    flag a zero-element ``argvalues`` (list or tuple, declared positionally or
    via ``argvalues=``) and ignore single/multi-case parametrizes, variable
    case lists, non-parametrize calls, and parametrizes without a statically
    known case list."""
    positive_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', ())\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', argvalues=[])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2])\n"
        "    @pytest.mark.parametrize('y', [])\n    def test_bar(x, y): pass\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_parametrize_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2])\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', SOME_CASES)\n    def test_bar(x): pass\n",
        "def test_foo():\n    @pytest.mark.skip(reason='x')\n    def test_bar(): pass\n",
        "def test_foo():\n    parametrize('x', [])\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_parametrize_violations(tree), f"lens should NOT flag:\n{source}"


_SYNC_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}


def _unbounded_sync_subprocess_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``subprocess.<fn>(...)`` call
    made without a ``timeout=`` bound. ``subprocess.run(timeout=None)`` is just
    as unbounded as an omitted keyword — ``None`` is the default meaning "wait
    forever" — so an explicit ``None`` literal is still flagged."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr not in _SYNC_SUBPROCESS_CALLS:
            continue
        if not isinstance(f.value, ast.Name) or f.value.id != "subprocess":
            continue
        bounded = any(
            kw.arg == "timeout" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
            for kw in node.keywords
            if kw.arg
        )
        if bounded:
            continue
        found.append(
            (node.lineno, f"subprocess.{f.attr}(...) without a timeout bound — a hung child blocks the test forever")
        )
    return found


def _compound_boolean_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert A and B`` whose
    operands are all comparisons (including nested comparison ``and``s). ``or``
    conjunctions are deliberately NOT flagged: they are the intentional "any of
    these" idiom (error-message vocabularies, optional API fields) and cannot
    be split into independent asserts without changing semantics."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.BoolOp) or not isinstance(test.op, ast.And):
            continue
        if len(test.values) < 2:
            continue
        if not all(isinstance(v, ast.Compare) for v in test.values):
            continue
        found.append(
            (
                node.lineno,
                f"asserts {ast.unparse(test)} — compound 'and'; split into separate asserts "
                "so a failure reports which condition broke",
            )
        )
    return found


_ASYNC_SUBPROCESS_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}


def _wait_for_bounds(awaitable: ast.AST) -> bool:
    """True when ``awaitable`` is ``asyncio.wait_for(..., timeout=...)`` with a
    non-``None`` timeout, the async twin of a sync ``timeout=`` keyword."""
    if not (isinstance(awaitable, ast.Call) and isinstance(awaitable.func, ast.Attribute)):
        return False
    if awaitable.func.attr != "wait_for":
        return False
    timeout = awaitable.args[1] if len(awaitable.args) >= 2 else None
    if timeout is None:
        for kw in awaitable.keywords:
            if kw.arg == "timeout":
                timeout = kw.value
    return not (isinstance(timeout, ast.Constant) and timeout.value is None)


def _unbounded_async_subprocess_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``asyncio.create_subprocess_*``
    process whose ``proc.communicate()``/``proc.wait()`` is not wrapped in
    ``asyncio.wait_for(...)`` with a timeout. ``proc.communicate()`` blocks
    until the child exits; without a bound the test hangs the event loop."""
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    found = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        proc_vars = {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and isinstance(node.value.value.func, ast.Attribute)
            and node.value.value.func.attr in _ASYNC_SUBPROCESS_CALLS
            and isinstance(node.value.value.func.value, ast.Name)
            and node.value.value.func.value.id == "asyncio"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("communicate", "wait"):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id not in proc_vars:
                continue
            if isinstance(parent.get(node), ast.Call) and _wait_for_bounds(parent.get(node)):
                continue
            found.append(
                (
                    node.lineno,
                    "proc.communicate()/wait() not wrapped in "
                    "asyncio.wait_for(..., timeout=...) — a hung child blocks the test forever",
                )
            )
    return found


def test_no_unbounded_subprocess_calls():
    """A subprocess spawned by a test — sync via ``subprocess.run``/``Popen``
    or async via ``asyncio.create_subprocess_*`` — must carry an explicit
    timeout bound. Without one, a child that hangs takes the whole test (and
    CI run) down with it, and the failure is opaque: the runner simply stops
    instead of reporting which bound was exceeded. This is the test-suite twin
    of the ``requests_without_timeout`` rule that already guards HTTP in
    ``src/modulo``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unbounded_sync_subprocess_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
        for lineno, detail in _unbounded_async_subprocess_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unbounded subprocess call(s).\n"
        "Give every child process an explicit timeout bound: add timeout=<secs> "
        "to the subprocess call, or wrap await proc.communicate()/wait() in "
        "asyncio.wait_for(..., timeout=<secs>).\n" + "\n".join(violations)
    )


def test_unbounded_subprocess_lens_flags_hang_risks():
    """Synthetic positive/negative control for the unbounded-subprocess lens:
    must flag sync calls without a timeout (or with an explicit ``None``), and
    async ``communicate()``/``wait()`` awaits not wrapped in a timed
    ``wait_for``; must ignore bounded calls, non-``subprocess`` callers, and
    plain variable awaits."""
    positive_sources = [
        "def test_foo():\n    subprocess.run(['ls'])\n",
        "def test_foo():\n    subprocess.Popen(['ls'], stdout=subprocess.PIPE)\n",
        "def test_foo():\n    subprocess.check_call(['ls'], timeout=None)\n",
        "async def test_foo():\n    proc = await asyncio.create_subprocess_shell('ls')\n"
        "    out = await proc.communicate()\n",
        "async def test_foo():\n    proc = await asyncio.create_subprocess_exec('ls')\n    code = await proc.wait()\n",
        "async def test_foo():\n    proc = await asyncio.create_subprocess_shell('ls')\n"
        "    out = await asyncio.wait_for(proc.communicate(), timeout=None)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unbounded_sync_subprocess_violations(tree) or _unbounded_async_subprocess_violations(tree), (
            f"lens should flag:\n{source}"
        )

    negative_sources = [
        "def test_foo():\n    subprocess.run(['ls'], timeout=5)\n",
        "def test_foo():\n    subprocess.run(['ls'], timeout=TIMEOUT)\n",
        "def test_foo():\n    os.system('ls')\n",
        "def test_foo():\n    subprocess.run(['ls'], check=True, capture_output=True, text=True, timeout=5)\n",
        "async def test_foo():\n    proc = await asyncio.create_subprocess_shell('ls')\n"
        "    out = await asyncio.wait_for(proc.communicate(), timeout=10)\n",
        "async def test_foo():\n    proc = await asyncio.create_subprocess_shell('ls')\n"
        "    out = await asyncio.wait_for(proc.communicate(), 10)\n",
        "async def test_foo():\n    out = await foo.communicate()\n",
        "def test_foo():\n    subprocess.run(['ls'], timeout=5)\n    my_func(subprocess.run)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unbounded_sync_subprocess_violations(tree), f"lens should NOT flag:\n{source}"
        assert not _unbounded_async_subprocess_violations(tree), f"lens should NOT flag:\n{source}"


def test_no_compound_boolean_assertions():
    """``assert A and B`` where every operand is a comparison is a compound
    boolean assertion: when it fails, pytest reports the whole conjunction and
    cannot say which condition broke, so the first green run hides which half
    of the check regressed. Split it into one ``assert`` per condition — the
    suite keeps the same guarantees and each failure names its own operand.
    ``or`` conjunctions are left alone: they are the intentional "any of these"
    idiom (error-message vocabularies, optional API fields) and cannot be split
    without changing semantics."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _compound_boolean_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} compound 'and' assertion(s).\n"
        "Each comparison should be its own assert so a failure names the broken condition.\n" + "\n".join(violations)
    )


def test_compound_boolean_lens_flags_split_able_conjunctions():
    """Synthetic positive/negative control for the compound-``and`` lens: must
    flag every ``assert`` whose top-level ``and`` joins only comparisons (and
    nested comparison ``and``s) and ignore pure truthiness conjunctions, ``or``
    conjunctions, single comparisons, De Morgan ``not (A and B)``, and mixes
    where an ``or`` component makes the conjunction intentional."""
    positive_sources = [
        "def test_foo():\n    assert a == 1 and b == 2\n",
        "def test_foo():\n    assert x is not None and y is not None\n",
        "def test_foo():\n    assert 'a' in x and 'b' in x and 'c' in x\n",
        "def test_foo():\n    assert a == 1 and b == 2 and c == 3\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _compound_boolean_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert a and b\n",
        "def test_foo():\n    assert a == 1 or b == 2\n",
        "def test_foo():\n    assert a == 1\n",
        "def test_foo():\n    assert not (a == 1 and b == 2)\n",
        "def test_foo():\n    assert a == 1 and (b or c)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _compound_boolean_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _redundant_bool_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert bool(x)`` /
    ``assert not bool(x)`` where the ``bool()`` wrapper is redundant."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        target = test.operand if negated else test
        if not (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id == "bool"
            and len(target.args) == 1
            and not target.keywords
        ):
            continue
        found.append((node.lineno, f"assert {ast.unparse(test)} — bool() is redundant inside an assert"))
    return found


def test_no_redundant_bool_in_assert():
    """``assert bool(x)`` / ``assert not bool(x)`` wrap the value in a no-op:
    ``assert`` already tests truthiness (and inverts it under ``not``), so the
    ``bool()`` call adds noise without changing behavior. Assert the value
    directly — the same outcome with one less call and no misdirection about an
    explicit conversion being needed."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _redundant_bool_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} redundant bool() assertion(s).\n"
        "assert already tests truthiness; drop the bool() wrapper.\n" + "\n".join(violations)
    )


def test_redundant_bool_lens_flags_noop_wrappers():
    """Synthetic positive/negative control for the redundant-``bool`` lens:
    must flag ``assert bool(x)`` and ``assert not bool(x)`` (either operand
    shape) and ignore ``bool()`` used inside a comparison — where the explicit
    conversion to a real bool is meaningful — and plain truthiness asserts."""
    positive_sources = [
        "def test_foo():\n    assert bool(x)\n",
        "def test_foo():\n    assert not bool(x)\n",
        "def test_foo():\n    assert bool(result.value)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _redundant_bool_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x\n",
        "def test_foo():\n    assert not x\n",
        "def test_foo():\n    assert bool(x) is True\n",
        "def test_foo():\n    assert bool(x) == True\n",
        "def test_foo():\n    assert bool(x) == bool(y)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _redundant_bool_assert_violations(tree), f"lens should NOT flag:\n{source}"


_NEGATED_COMPARISON_MIRRORS = {
    ast.Eq: "!=",
    ast.NotEq: "==",
    ast.In: "not in",
    ast.NotIn: "in",
    ast.Is: "is not",
    ast.IsNot: "is",
}
"""Operator -> preferred positive mirror for a negated single comparison."""


def _negated_comparison_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert not (a <op> b)``
    where ``not`` negates a single comparison instead of the comparison being
    written with the mirrored operator (``!=``/``==``/``not in``/``in``/
    ``is not``/``is``). ``not`` over a ``BoolOp`` (De Morgan compound such as
    ``not (a == 1 and b == 2)``) is deliberately left alone — it is the
    intentional "none of these hold" idiom and the mirrored form is a
    different expression."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        operand = test.operand
        if not (isinstance(operand, ast.Compare) and len(operand.ops) == 1):
            continue
        op = operand.ops[0]
        mirror = _NEGATED_COMPARISON_MIRRORS.get(type(op))
        if mirror is None:
            continue
        prefer = f"assert {ast.unparse(operand.left)} {mirror} {ast.unparse(operand.comparators[0])}"
        found.append((node.lineno, f"{ast.unparse(test)} — prefer '{prefer}'"))
    return found


def test_no_negated_comparison_asserts():
    """``assert not (a == b)`` negates a single comparison when the positive
    mirror — ``assert a != b`` — reads the intent directly. Wrapping the
    comparison in ``not`` makes pytest report a negated boolean in the failure
    diff (``assert not False``) instead of naming the two values that were
    compared, and it is the exact class of expression ruff's SIM201/SIM202
    flags (SIM201: ``assert not a == b`` -> ``assert a != b``; SIM202:
    ``assert not a != b`` -> ``assert a == b``). The ``in``/``is`` mirrors are
    the membership/identity twins (``assert a not in b``, ``assert a is not
    b``). ``not`` over a compound ``BoolOp`` is left alone: ``assert not
    (a == 1 and b == 2)`` is the intentional "none of these hold" idiom and
    the existing compound-``and`` lens deliberately exempts it from splitting."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _negated_comparison_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} negated single-comparison assertion(s).\n"
        "Write the comparison with the mirrored operator instead of wrapping it "
        "in not: 'assert a != b' / 'assert a not in b' / 'assert a is not b'.\n" + "\n".join(violations)
    )


def test_negated_comparison_lens_flags_reversed_asserts():
    """Synthetic positive/negative control for the negated-comparison lens:
    must flag ``not``-wrapped ``==``/``!=``/``in``/``not in``/``is``/``is not``
    comparisons (each with the correct preferred mirror) and ignore plain
    truthiness negations, negated compounds, ``not`` over other operators, and
    comparisons written with the mirrored operator already."""
    positive_sources = [
        ("def test_foo():\n    assert not (a == b)\n", "assert a != b"),
        ("def test_foo():\n    assert not (a != b)\n", "assert a == b"),
        ("def test_foo():\n    assert not (a in b)\n", "assert a not in b"),
        ("def test_foo():\n    assert not (a not in b)\n", "assert a in b"),
        ("def test_foo():\n    assert not (a is b)\n", "assert a is not b"),
        ("def test_foo():\n    assert not (a is not b)\n", "assert a is b"),
        ("def test_foo():\n    assert not (result.value == expected)\n", "assert result.value != expected"),
    ]
    for source, prefer in positive_sources:
        tree = ast.parse(source)
        violations = _negated_comparison_assert_violations(tree)
        assert violations, f"lens should flag:\n{source}"
        assert prefer in violations[0][1], f"lens should suggest '{prefer}' for:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert a == b\n",
        "def test_foo():\n    assert a != b\n",
        "def test_foo():\n    assert a\n",
        "def test_foo():\n    assert not a\n",
        "def test_foo():\n    assert not (a == 1 and b == 2)\n",
        "def test_foo():\n    assert not (a < b)\n",
        "def test_foo():\n    assert not a or b\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _negated_comparison_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _compound_isinstance_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert isinstance(a, T)
    and isinstance(b, U)`` whose ``and`` operands are all ``isinstance()``
    calls."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.BoolOp) or not isinstance(test.op, ast.And):
            continue
        if len(test.values) < 2:
            continue
        if not all(
            isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "isinstance" for v in test.values
        ):
            continue
        found.append(
            (
                node.lineno,
                f"asserts {ast.unparse(test)} — compound isinstance 'and'; split into separate asserts "
                "so a failure reports which operand has the wrong type",
            )
        )
    return found


def test_no_compound_isinstance_assertions():
    """``assert isinstance(a, X) and isinstance(b, Y)`` joins two independent
    type checks with ``and`` — a compound boolean assertion. When it fails,
    pytest reports the whole conjunction and cannot say which value had the
    wrong type, so the first green run hides which operand regressed. Split it
    into one ``assert`` per isinstance call — the suite keeps the same
    guarantees and each failure names its own operand. This is the isinstance
    twin of the compound-``and`` lens, which only flags all-``Compare``
    conjunctions and so cannot see isinstance calls. A single isinstance on
    its own, or an isinstance mixed with a truthiness/``is not None`` check,
    is the deliberate "type and non-empty" idiom and is left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _compound_isinstance_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} compound isinstance 'and' assertion(s).\n"
        "Each isinstance should be its own assert so a failure names the operand with the wrong type.\n"
        + "\n".join(violations)
    )


def test_compound_isinstance_lens_flags_split_able_conjunctions():
    """Synthetic positive/negative control for the compound-isinstance lens:
    must flag ``and`` conjunctions whose operands are all ``isinstance()``
    calls (any operand shape, nested or not) and ignore a single isinstance,
    mixed conjunctions (isinstance + truthiness / ``is not None`` / a
    comparison), pure comparison compounds (owned by the compound-``and``
    lens), and ``or`` conjunctions."""
    positive_sources = [
        "def test_foo():\n    assert isinstance(a, int) and isinstance(b, str)\n",
        "def test_foo():\n    assert isinstance(result, dict) and isinstance(result['key'], list)\n",
        "def test_foo():\n    assert isinstance(a, X) and isinstance(b, Y) and isinstance(c, Z)\n",
        "def test_foo():\n    assert isinstance(a, (int, float)) and isinstance(b, str)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _compound_isinstance_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert isinstance(a, int)\n",
        "def test_foo():\n    assert isinstance(a, int) and a > 0\n",
        "def test_foo():\n    assert isinstance(a, int) and a is not None\n",
        "def test_foo():\n    assert isinstance(a, int) and a\n",
        "def test_foo():\n    assert a == 1 and b == 2\n",
        "def test_foo():\n    assert isinstance(a, int) or isinstance(b, str)\n",
        "def test_foo():\n    assert a\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _compound_isinstance_assert_violations(tree), f"lens should NOT flag:\n{source}"
