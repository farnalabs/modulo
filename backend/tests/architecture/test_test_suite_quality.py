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

    def _decorator_name(dec: ast.AST) -> str | None:
        if isinstance(dec, ast.Call):
            dec = dec.func
        if isinstance(dec, ast.Attribute):
            return dec.attr
        if isinstance(dec, ast.Name):
            return dec.id
        return None

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
