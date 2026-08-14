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
- ``assert len(x) > 0`` / ``assert len(x) >= 1`` / ``assert len(x) != 0``
  (the non-emptiness mirror of the ``len(x) == 0`` lens) — sized containers
  are truthy exactly when non-empty, so these should read ``assert x``
- ``assert x == []`` / ``assert x == {}`` against an empty container literal —
  ``== []``/``== {}`` is the equality-based twin of the ``len() == 0`` idiom
  and should read ``assert not x`` (an empty container is falsy)
- hand-rolled ``try: ... raise AssertionError(...) except X: pass`` instead of
  ``pytest.raises`` (the success path is only guarded by the ``raise`` line)
- ``assert`` nested inside ``except`` handlers (a failing assert masks the
  original exception and discards its traceback context)
- no-op ``test_*`` functions whose body contains no verification at all (they
  report green even when the code under test is completely broken, as long as
  no exception escapes)

Every lens is written so it reports actionable file:line violations instead
of a bare "assert not violations", mirroring the sibling architecture tests.
"""

import ast
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
    with less noise and no literal-type coupling. Operands whose type is
    statically a container (attribute access, subscript, call, or await) are
    flagged; a bare name is left alone because it may bind a ``__bool__``- or
    ``__eq__``-overloading object whose emptiness is not ``not``."""
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
            if not isinstance(test.ops[0], ast.Eq):
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
                violations.append(
                    f"  {rel}:{node.lineno}  asserts value == {'[]' if isinstance(literal, ast.List) else '{}'} "
                    "— prefer 'assert not ...'"
                )
                break
    assert not violations, (
        f"Found {len(violations)} empty-container literal comparison(s).\n"
        "An empty list/dict is falsy; write 'assert not <expr>' instead of "
        "'assert <expr> == []/{}'.\n" + "\n".join(violations)
    )


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
