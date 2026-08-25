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
- redundant call assertions right before the test inspects the same mock's
  recorded calls (``<mock>.calls[0]``/``<mock>.call_args[0]``) — the
  introspection access that follows already fails loudly when the call never
  happened, so the assertion is dead code that can silently drift out of sync
  with what the test actually inspects. Both spellings are covered: the
  statement form ``assert <mock>.called`` and the method-call form
  ``<mock>.assert_called()`` / ``<mock>.assert_awaited()`` (the ``_once``
  variants add the "exactly one call" guarantee and are left alone)
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
- unbounded worker-thread joins — ``thread.join()``/``Thread.join()`` without
  a ``timeout=`` bound, the in-process sibling of the unbounded-subprocess
  hazard. A worker that deadlocks (e.g. waits on a ``Barrier`` a sibling never
  reaches, or blocks on an I/O operation that never completes) takes the whole
  test — and every test after it in the same process — down with it, and the
  failure is opaque (the runner just stops) instead of surfacing a bound
  violation. ``join(timeout=None)`` is as unbounded as an omitted keyword and
  is flagged too. ``str.join``/``os.path.join``/``Path.joinpath`` are
  deliberately not matched: those always carry the iterable/path argument, so
  an argument-less ``.join()`` call is unambiguously a thread join.
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
- ``async def test_*`` functions whose body contains *no* async construct at
  all — no ``await``, no ``async with``, no ``async for``, no ``yield``. An
  async test that never suspends runs plain synchronous code on the event
  loop for no reason, and the coroutine boundary is a silent-false-green
  hazard: anyone who later calls an ``async`` function and forgets the
  ``await`` will find the assertion comparing against a coroutine object
  (always truthy) instead of failing. Declare such tests as a plain ``def``.
- ``async def`` fixtures whose body contains *no* async construct — the
  fixture twin of the needlessly-async-test lens. The coroutine boundary is
  the same silent-false-green hazard (a forgotten ``await`` silently builds
  the fixture from a coroutine object instead of failing), and a sync
  fixture is requested the same way by async tests, so declare these as a
  plain ``def``. Unlike the test lens, a ``pass``-only body is flagged too —
  the no-op-test lens skips fixtures, so nothing else covers it.
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
- ``@pytest.mark.parametrize`` whose declared argname is never referenced in
  the test body — the parametrize runs the body once per case but the body
  ignores the parameter, so every case exercises the *same* assertion. The
  matrix coverage is illusory: a regression in the behaviour that the
  parameter was meant to vary is caught by case 1 and reported identically
  N times, while a reader (and a mutation-testing run) believes N distinct
  inputs are covered. Drop the unused parameter (and the parametrize when
  that leaves no other varying argument)
- an async decorator on a *synchronous* ``def`` — ``@pytest.mark.asyncio`` /
  ``@pytest.mark.anyio`` on a plain ``def``, and ``@pytest_asyncio.fixture``
  on a plain ``def`` fixture. With ``asyncio_mode = auto``, pytest-asyncio
  already infers async behaviour from ``async def``, so an async marker on a
  ``def`` is a needless coroutine boundary: at best a misleading no-op (the
  body never suspends, but readers expect it to) and at worst a runtime
  mismatch (a sync fixture whose value pytest-asyncio expects to await).
  These are almost always the leftover twin of the needlessly-async
  conversion — when an ``async def`` was flipped to a plain ``def``, its
  async decorator should have gone too
- split ``assert_called_once``/``assert_awaited_once`` + ``assert_called_with``/
  ``assert_awaited_with`` pairs on the *same* mock — the two-line form is the
  split twin of the single atomic ``assert_called_once_with(...)``/
  ``assert_awaited_once_with(...)`` check. Written separately, the pair can
  silently drift out of sync (one half edited, the other not), so a reader —
  and a mutation-testing run — cannot rely on the "exactly once with these
  args" guarantee the combined form enforces in one statement. Merge the pair
  into the ``_once_with`` form

- function parameters that name a pytest *built-in fixture* (``monkeypatch``,
  ``mocker``, ``caplog``, ``capsys``, ``capfd``, ``recwarn``, ``tmp_path``,
  ``tmpdir``, ...) but are never referenced in the body — those fixtures have
  no setup side effect, so requesting one and never touching the value is
  pure dead weight that misleads readers into believing the test controls
  (say) environment state. Drop the unused parameter. ``request`` is
  deliberately not matched: pytest-bdd step functions conventionally carry it
  even when the body only reaches state through fixture names, and
  ``tmp_path_factory`` is session-scoped plumbing rather than a per-test
  capability
- ``assert a == b == c`` — an ``assert`` whose test is a *chained equality*
  comparison (a ``==`` chain with two or more operators) asserts N independent
  equalities as one expression. When the chain fails, pytest reports the whole
  chain and cannot say which link broke, so a mutation-testing run that severs
  the middle relationship (``b``) reports the same opaque failure every time.
  Split each link into its own ``assert`` so each failure names the pair that
  broke. Range checks (``assert lo <= x <= hi``) use ordering operators and
  are deliberately exempt: a bounds assertion is a single fact that reads
  naturally as a chain, so only ``==`` chains are flagged
- ``@pytest.mark.parametrize`` whose ``argvalues`` holds a *duplicate* case —
  the single-case and empty-case lenses guard the degenerate ends of the case
  list, and this lens guards the copy-paste trap in the middle: two cases with
  the *same value* run the test body twice with identical inputs, so the second
  run adds no coverage while the parametrize advertises one more distinct case
  than it exercises. A reader (and a mutation-testing run) believes N distinct
  inputs are covered when only N-1 are. The lens compares each case by value
  (after ``ast.literal_eval``), so ``1`` and ``True`` are deliberately treated
  as distinct and only byte-identical values are flagged — an unambiguous
  duplicate
- ``@pytest.mark.skipif``/``@pytest.mark.xfail`` whose *condition* is a
  statically-foldable literal (``True``, ``0``, ``[]``, a string, ...) — the
  skip outcome is decided at source time. ``skipif(True, ...)`` permanently
  deselects the test from every run (the same coverage loss as a ``@skip``
  marker, but hiding behind a "conditional" spelling that reads as
  deliberate), and ``skipif(False, ...)`` is a dead marker that never
  triggers. Both are almost always leftovers from temporarily disabling a
  test while debugging, and a reader — or a mutation-testing run — believes
  the test participates when it silently does not. The module-level
  ``pytestmark = pytest.mark.skipif(...)`` form is covered too. Dynamic
  conditions (names, calls, comparisons, attributes) are left alone: those
  evaluate at collection time and are the legitimate form, and the
  skip-without-reason lens already owns the missing-``reason`` half of the
  marker
- ``pytest.raises``/``pytest.warns`` whose expected exception is a *broad*
  class — ``Exception``, ``BaseException``, or ``AssertionError`` (for
  ``raises``) without a ``match=`` narrow. ``pytest.raises(Exception)``
  catches every failure — including a regression that raises the *wrong*
  exception and the test's own assertion errors — so the test reports green
  when the behaviour it guards silently changes to raise something else;
  ``pytest.raises(BaseException)`` widens that mask to
  ``KeyboardInterrupt``/``SystemExit``. ``pytest.raises(AssertionError)``
  without ``match=`` is the sharpest form of the hazard: an *internal* assert
  bug in the code under test is swallowed as the expected exception and the
  test passes. A ``match=`` narrows the message, not the type, so it does not
  rescue the call. The same catch-all applies to ``@pytest.mark.xfail(raises=...)``
  markers, which then xfail on *any* exception. Name the specific exception
  the code is documented to raise (``ValueError``, ``KeyError``, ...); when
  the code under test is an assert-based validator that must trip, pin the
  failure with ``match=`` so the check cannot silently absorb a different
  error. Attribute-qualified classes (``pytest.skip.Exception`` /
  ``pytest.xfail.Exception``) and concrete named exceptions are left alone —
  they are the specific form this lens exists to force, and a union of
  *specific* exceptions (``(ValueError, TypeError)``) is the intentional
  "either of these" form
- ``assert <mock>.assert_*()`` / ``assert not <mock>.assert_*()`` — using a
  unittest.mock verification method *as* the assertion's test expression.
  Every ``assert_called``/``assert_called_with``/``assert_awaited*``/
  ``assert_not_called``/``assert_has_calls`` method returns ``None``, so
  ``assert mock.assert_called()`` is a typo for ``mock.assert_called()`` +
  ``assert <something>``: the assertion is ``assert None``, which ALWAYS
  FAILS even when the double verified correctly (the test is red no matter
  what the code under test does), and ``assert not mock.assert_called()`` is
  ``assert not None``, which ALWAYS PASSES regardless of the recorded calls
  (a silent false green that a mutation-testing run believes is a real
  verification). Both are almost always a leftover from inlining a bare
  ``assert`` in front of a verification call while debugging; the correct
  form is to call the verification method as its own statement — it already
  raises ``AssertionError`` on mismatch — and assert on a real mock attribute
  (``call_count``/``called``/``call_args``) for the value check. Only the
  whole-expression position is flagged: a verification call appearing inside a
  larger expression (the negation of another operand, a function argument, ...)
  is not the assertion itself and is left alone
- a freshly-constructed Mock passed as an *expected* argument to a mock
  call-assertion — ``<mock>.assert_called_with(Mock())``,
  ``assert_called_once_with(...)``, ``assert_any_call(...)``, and their awaited
  twins, in any positional or keyword argument position. The recorded call is
  whatever object the code under test actually passed, and a fresh Mock
  compares by identity (``__eq__`` defaults to ``is``), so the expected tuple
  can never equal the recorded one: the assertion always FAILS, and for
  ``assert_any_call`` fresh mocks can never match any recorded call either.
  This is the expected-argument twin of the assert-test-expression Mock lens:
  it is almost always a leftover from inlining a double while debugging.
  Configure the double and pass the configured instance (or use a bound
  name), or assert on the real expected value
- ``assert x and not x`` / ``assert x or not x`` (and the ``not``-wrapped
  twins ``assert not (x and not x)`` / ``assert not (x or not x)``) — a
  boolean assertion whose test expression joins a value with its own negation.
  An ``and`` conjunction containing a complementary pair is a contradiction
  that can never be true, so the assert ALWAYS FAILS (unconditionally red)
  no matter what the code under test does; an ``or`` disjunction containing a
  complementary pair is a tautology that is always true, so the assert ALWAYS
  PASSES (a silent false green that a mutation-testing run believes verifies
  behavior). Both shapes are dead code — the outcome is decided by the
  expression itself, never by the behaviour under test. The lens flags only
  the top-level ``BoolOp`` of the test expression (or one ``not``-wrapped),
  comparing operands by syntax so it catches attribute paths and subscripts
  too (``assert row['x'] and not row['x']``); complementary comparisons
  written with mirrored operators (``x == y or x != y``) are left alone
  because they need operator algebra rather than syntax to prove
- wall-clock sleeps with a *computed* duration — ``time.sleep(<name>)`` /
  ``asyncio.sleep(<name>)`` where the argument is a bare name rather than a
  literal constant. A duration computed from other values (a refill rate, a
  delay variable, a backoff) is a timing-contract check that depends on real
  wall-clock passage: it is slower than necessary and flakes under load, while
  the ``sleep(<literal>)`` forms are either deliberate hang-simulation
  (timeout/cancellation tests) or deliberate tiny event-loop yields
  (``sleep(0)``) and are left alone. The fix is to inject the time source the
  code under test reads — e.g. a monotonic ``clock`` callable — and advance it
  deterministically instead of sleeping
- a ``pytest.raises(...)`` / ``pytest.warns(...)`` call standing as its own
  bare expression statement — the ``RaisesContext``/``WarningsChecker``
  context manager is constructed but never entered with ``with``, so the
  exception (or warning) it claims to expect is never actually checked: the
  test passes whether the code under test raises it, raises the *wrong*
  exception, or raises nothing at all. This is the missing-``with`` twin of
  the broad-exception lens — ``pytest.raises(X)`` as a statement is a silent
  false green that the broad lens only incidentally catches when ``X`` is
  ``Exception``/``BaseException``. The deprecated functional form
  ``pytest.raises(X, func, *args)`` actually runs the check and is
  deliberately left alone; the ``with``-entered and decorator spellings are
  naturally excluded because they never appear as a bare expression statement
- ``assert x == ANY`` / ``assert x != ANY`` against ``unittest.mock.ANY`` —
  ``ANY.__eq__`` returns ``True`` for *any* value (that is what lets it match
  an expected argument inside ``assert_called_with``/``assert_awaited_with``),
  so ``== ANY`` is ALWAYS True and ``!= ANY`` is ALWAYS False regardless of
  what the other operand evaluates to: an ``assert x == ANY`` is a silent
  false green and ``assert x != ANY`` can never pass, both decided at source
  time. ``ANY`` is only meaningful where a mock framework presides over the
  comparison — pass it as the expected argument to a mock verification
  (``mock.assert_called_with(ANY)``), never compare a value to it with
  ``==``/``!=`` yourself. The membership twin is covered too: a list/tuple
  literal that *contains* ``ANY`` (``[a, ANY]``) makes ``in`` always PASS and
  ``not in`` always FAIL, because the element match short-circuits on
  ``x == ANY``
- a direct mutation of the process environment made without the ``monkeypatch``
  fixture in scope — subscript set/delete on the ``os.environ`` mapping
  (``os.environ[key] = ...`` / ``del os.environ[key]`` and the
  ``from os import environ`` twin), the mutating ``environ`` methods
  (``pop``/``update``/``setdefault``/``clear`` and their ``__*__`` / pydantic
  twins), and ``os.putenv()``/``os.unsetenv()``. A test that mutates
  ``os.environ`` and never restores it leaks state into every test that runs
  afterwards, so the suite becomes order-dependent: a test can pass alone and
  silently corrupt a sibling (or be corrupted by one) in the full run.
  ``monkeypatch.setenv()``/``monkeypatch.delenv()`` restore the value at
  teardown automatically and are the pytest-blessed form — a function that
  requests ``monkeypatch`` is left alone even when it mutates ``os.environ``
  directly. Reads (``os.getenv``, ``os.environ.get``, subscript loads) and the
  module-level ``os.environ.setdefault(...)`` bootstrap (the ``conftest.py``
  pattern that pins ``DATABASE_URL`` once at import time, which is idempotent
  configuration rather than between-test leakage) are deliberately left alone

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


_MOCK_CALL_INTROSPECTION = frozenset(
    {"calls", "call_args", "call_args_list", "call_count", "await_args", "await_args_list", "await_count"}
)
"""Mock attributes that inspect the recorded calls after the fact. Accessing
``<mock>.calls[0]``/``<mock>.call_args[0]`` (or the ``await_*`` twins for
AsyncMock) fails loudly (``IndexError``/``AttributeError``) when the call never
happened, so a call assertion immediately before such an access is dead — it
duplicates the check the introspection access already performs, and can
silently drift out of sync with what the test actually inspects."""

_MOCK_WEAK_ASSERT_METHODS = frozenset({"assert_called", "assert_awaited"})
"""Mock assertion methods that verify only "called at least once" — the
method-call twin of ``assert <mock>.called``. They add nothing over the
introspection access that follows (which already fails loudly when the call
never happened). The ``_once``/``_with`` variants are deliberately NOT in this
set: ``assert_called_once`` adds the "exactly one call" guarantee and
``assert_called_with`` pins the arguments, so neither is redundant."""


def _redundant_called_assertions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every weak call assertion —
    ``assert <mock>.called`` or ``<mock>.assert_called()``/
    ``<mock>.assert_awaited()`` — that is immediately followed by an
    introspection access on the same mock."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for i, stmt in enumerate(node.body[:-1]):
            base = None
            if isinstance(stmt, ast.Assert) and isinstance(stmt.test, ast.Attribute) and stmt.test.attr == "called":
                base = stmt.test.value
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                f = stmt.value.func
                if isinstance(f, ast.Attribute) and f.attr in _MOCK_WEAK_ASSERT_METHODS:
                    base = f.value
            if base is None or not isinstance(base, (ast.Attribute, ast.Name)):
                continue
            nxt = node.body[i + 1]
            nxt_attrs = [
                sub for sub in ast.walk(nxt) if isinstance(sub, ast.Attribute) and sub.attr in _MOCK_CALL_INTROSPECTION
            ]
            if any(ast.dump(sub.value) == ast.dump(base) for sub in nxt_attrs):
                if isinstance(stmt, ast.Assert):
                    detail = (
                        f"assert {ast.unparse(base)}.called is redundant — the "
                        f"following {ast.unparse(base)}.<calls>/<call_args> access already "
                        "fails loudly when no call was recorded"
                    )
                else:
                    detail = (
                        f"{ast.unparse(stmt.value)} is redundant — the "
                        f"following {ast.unparse(base)}.<calls>/<call_args>/<await_args> "
                        "access already fails loudly when no call was recorded"
                    )
                found.append((stmt.lineno, detail))
    return found


def test_no_redundant_called_assertions():
    """A weak call assertion — ``assert <mock>.called`` or the method-call twin
    ``<mock>.assert_called()``/``<mock>.assert_awaited()`` — immediately before
    the test inspects the same mock's recorded calls (``<mock>.calls[0]``,
    ``<mock>.call_args[0]``, ``<mock>.await_args``, ...) is dead code: the
    introspection access that follows fails loudly — an ``IndexError`` on
    ``calls[0]``/``call_args[0]``, an empty ``call_args_list`` that makes later
    ``in``-style assertions fail — if the call never happened. The weak assert
    therefore duplicates the very check the next line performs, and because the
    two can drift apart (asserting one mock's call status while inspecting a
    *different* call path), it quietly gives a false sense of rigour. Drop the
    assert and keep the introspection access. The lens only flags the weak
    positive forms; ``assert not <mock>.called`` /
    ``<mock>.assert_not_called()`` are genuine no-call checks, and the ``_once``
    variants (``assert_called_once``/``assert_awaited_once``) add the "exactly
    one call" guarantee — both are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _redundant_called_assertions(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} redundant call assertion(s).\n"
        "The immediately following <mock>.calls[0]/<mock>.call_args/<mock>.await_args access already fails "
        "loudly when the call never happened, so the weak assert is dead code.\n"
        "Drop the redundant assert and keep the introspection access.\n" + "\n".join(violations)
    )


def test_redundant_called_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the redundant-call lens,
    mirroring the identity-literal lens pattern: it must flag a weak call
    assertion — ``assert <mock>.called`` OR ``<mock>.assert_called()`` /
    ``<mock>.assert_awaited()`` — that is immediately followed by a
    recorded-calls access on the same mock, and ignore negated no-call checks,
    the ``_once`` variants (which add the "exactly one call" guarantee), weak
    asserts without a follow-up introspection, and introspection on a different
    mock."""
    positive_sources = [
        "def test_foo():\n    assert route.called\n    assert route.calls[0].request.url.endswith('/x')\n",
        "def test_foo():\n    assert session.add.called\n    row = session.add.call_args.args[0]\n",
        "def test_foo():\n    assert mock.execute.called\n    calls = mock.execute.call_args_list\n",
        "def test_foo():\n    assert response.called\n    response.calls[0]\n",
        "def test_foo():\n    mock_append.assert_called()\n    call = mock_append.call_args\n",
        "def test_foo():\n    mock_consume.assert_awaited()\n    assert mock_consume.await_args.kwargs['x'] == 1\n",
        "def test_foo():\n    reenqueue.assert_awaited()\n    assert reenqueue.await_count == 2\n",
        "def test_foo():\n    mock.execute.assert_called()\n    calls = mock.execute.calls\n",
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
        "def test_foo():\n    mock.assert_called_once()\n    call = mock.call_args\n",
        "def test_foo():\n    mock.assert_awaited_once()\n    call = mock.await_args\n",
        "def test_foo():\n    mock.assert_not_called()\n",
        "def test_foo():\n    mock.assert_called()\n    other.call_args\n",
        "def test_foo():\n    mock.assert_called_with(1, 2)\n    call = mock.call_args\n",
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


def _parametrize_argvalue_lists(tree: ast.AST) -> list[tuple[int, list[ast.expr]]]:
    """Return ``(lineno, argvalues.elts)`` for every ``@...parametrize``
    decorator whose ``argvalues`` is a statically-known ``list``/``tuple``
    literal. Only decorator applications are considered — a bare
    ``parametrize(...)`` call inside a body is not pytest parametrization and
    belongs to a different lens. The parametrize-adjacent lenses derive their
    signal from ``len(elts)`` (``== 0``, ``== 1``, ...) or from the elements
    themselves (duplicate detection), so a new lens never re-copies the
    decorator walk."""
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
            found.append((dec.lineno, argvalues.elts))
    return found


def _single_case_parametrize_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize``
    decorator whose ``argvalues`` holds exactly one case. Only decorator
    applications are considered — a bare ``parametrize(...)`` call inside a
    body is not pytest parametrization and belongs to a different lens."""
    return [
        (lineno, "parametrize with a single case in argvalues — collapse to a plain test")
        for lineno, elts in _parametrize_argvalue_lists(tree)
        if len(elts) == 1
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
        for lineno, elts in _parametrize_argvalue_lists(tree)
        if len(elts) == 0
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


def _unbounded_thread_join_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every argument-less ``.join()``
    call made without a ``timeout=`` bound.

    An argument-less ``.join()`` is unambiguously a worker-thread join:
    ``str.join``/``os.path.join``/``Path.joinpath`` always carry the iterable
    or path argument, so the only ``.join()`` that takes nothing is
    ``Thread.join()`` (or its multiprocessing twin). Without a ``timeout=`` the
    call waits forever, so a deadlocked worker blocks the test — and the whole
    process — indefinitely. ``join(timeout=None)`` is just as unbounded as an
    omitted keyword (``None`` is the default meaning "wait forever"), so an
    explicit ``None`` literal is still flagged."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "join":
            continue
        if node.args:
            continue
        bounded = any(
            kw.arg == "timeout" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
            for kw in node.keywords
            if kw.arg
        )
        if bounded:
            continue
        found.append((node.lineno, "thread .join() without a timeout bound — a hung worker blocks the test forever"))
    return found


def test_no_unbounded_thread_join():
    """A worker thread joined without a ``timeout=`` bound can hang the whole
    test process: if the worker deadlocks (a ``Barrier`` a sibling never
    reaches, an I/O wait that never completes) the ``.join()`` waits forever,
    the runner simply stops, and every test after it in the process is lost
    without a trace. This is the in-process twin of the unbounded-subprocess
    lens, which guards the child-process version of the same hazard. Bound the
    join with ``thread.join(timeout=<secs>)`` — and, when the thread must have
    finished, assert ``not thread.is_alive()`` so a hung worker fails loudly
    with a named bound instead of stalling the suite."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unbounded_thread_join_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unbounded thread .join() call(s).\n"
        "Give every worker-thread join an explicit timeout bound: thread.join(timeout=<secs>), "
        "then assert not thread.is_alive() when the thread must have finished.\n" + "\n".join(violations)
    )


def test_unbounded_thread_join_lens_flags_hang_risks():
    """Synthetic positive/negative control for the unbounded-thread-join lens,
    mirroring the unbounded-subprocess lens pattern: it must flag argument-less
    ``.join()`` calls without a timeout (or with an explicit ``None``), in any
    receiver shape and nesting, and ignore bounded joins, ``str.join``/
    ``os.path.join``/``Path.joinpath`` (which always carry an argument), and
    non-``join`` calls."""
    positive_sources = [
        "def test_foo():\n    t = Thread(target=work)\n    t.start()\n    t.join()\n",
        "def test_foo():\n    thread.join()\n",
        "def test_foo():\n    t.join(timeout=None)\n",
        "def test_foo():\n    for t in threads:\n        t.join()\n",
        "def test_foo():\n    self._worker.join()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unbounded_thread_join_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    t.join(timeout=5)\n",
        "def test_foo():\n    t.join(timeout=TIMEOUT)\n",
        "def test_foo():\n    ', '.join(items)\n",
        "def test_foo():\n    ''.join(map(str, xs))\n",
        "def test_foo():\n    os.path.join(a, b)\n",
        "def test_foo():\n    Path(a).joinpath(b)\n",
        "def test_foo():\n    t.wait()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unbounded_thread_join_violations(tree), f"lens should NOT flag:\n{source}"


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


_ASYNC_CONSTRUCTS = (ast.Await, ast.AsyncWith, ast.AsyncFor, ast.Yield)
"""Node types that make a function genuinely async. ``async def`` alone is not
one of them — a test can be declared ``async def`` and still never suspend if
its body contains none of these (an ``await`` expression, an ``async with``
block, an ``async for`` loop, or a ``yield`` that turns the function into an
async generator). Async *comprehensions* (``[x async for x in y]``) are not a
distinct node type — they are a plain comprehension whose ``comprehension``
clause carries ``is_async=1`` — so they are detected separately."""


def _function_is_async(node: ast.AST) -> bool:
    """True when ``node`` (an ``async def``) actually suspends anywhere: it
    contains an ``await``, ``async with``, ``async for``, ``yield``, or an
    async comprehension. A nested ``async def`` helper is walked too — it is
    only a coroutine if it actually awaits, so a test that merely *defines*
    an async helper without awaiting it is still synchronous in the way that
    matters here."""
    for sub in ast.walk(node):
        if isinstance(sub, _ASYNC_CONSTRUCTS):
            return True
        if isinstance(sub, ast.comprehension) and sub.is_async:
            return True
    return False


def _async_test_without_async_behavior_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``async def test_*`` whose
    body contains no async construct at all (including async comprehensions)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if any(_decorator_name(d) == "fixture" for d in node.decorator_list):
            continue
        if not (node.name.startswith("test_") or any(_decorator_name(d) == "mark" for d in node.decorator_list)):
            continue
        if _function_is_async(node):
            continue
        effective_body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if all(isinstance(s, ast.Pass) for s in effective_body):
            continue
        found.append(
            (
                node.lineno,
                f"async def {node.name}() — body never awaits, declares no async with/for, "
                "yields nothing, and has no async comprehension; declare it a plain def",
            )
        )
    return found


def test_no_async_test_without_async_behavior():
    """``async def test_*`` whose body contains no async construct — no
    ``await``, ``async with``, ``async for``, or ``yield`` — runs plain
    synchronous code on the event loop and gains nothing from being a
    coroutine. Worse, the ``async`` boundary is a silent-false-green hazard:
    the first time someone edits the test to call an ``async`` function and
    forgets the ``await``, the assertion compares against a coroutine object
    (always truthy) and reports green without exercising the code under test.
    The suite runs with ``asyncio_mode = "auto"``, so declaring such tests as
    a plain ``def`` costs nothing and removes the boundary entirely. Tests
    that genuinely suspend (``await``/``async with``/``async for``/``yield``
    or an async comprehension) are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _async_test_without_async_behavior_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} async test function(s) that never suspend.\n"
        "An async test whose body contains no await/async with/async for/yield is a "
        "needless coroutine boundary — and a future unawaited call inside it would "
        "assert against a coroutine object (always truthy) instead of failing.\n"
        "Declare it a plain 'def' so the call cannot silently pass.\n" + "\n".join(violations)
    )


def test_async_test_lens_flags_needlessly_async_tests():
    """Synthetic positive/negative control for the needlessly-async lens: must
    flag ``async def test_*`` whose body contains no async construct (asserts,
    raises-contexts, and bare calls alike) and ignore tests that actually
    suspend (``await``, ``async with``, ``async for``), async generators
    (``yield``), sync tests, and fixtures."""
    positive_sources = [
        "async def test_foo():\n    assert foo() == 1\n",
        "async def test_foo(self):\n    return self.x\n",
        "async def test_foo():\n    with pytest.raises(ValueError):\n        foo()\n",
        "async def test_foo():\n    assert await_unaware_call()\n",
        "async def test_foo():\n    def helper():\n        return 1\n    assert helper() == 1\n",
        "async def test_foo():\n"
        "    class Helper:\n"
        "        async def run(self):\n"
        "            return 1\n"
        "    assert Helper().run() is not None\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _async_test_without_async_behavior_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "async def test_foo():\n    await foo()\n",
        "async def test_foo():\n    async with foo():\n        pass\n",
        "async def test_foo():\n    async for x in foo():\n        pass\n",
        "async def test_foo():\n    chunks = [x async for x in foo()]\n",
        "async def test_foo():\n    yield 1\n",
        "def test_foo():\n    assert foo() == 1\n",
        "@pytest.fixture\nasync def make_thing():\n    return 1\n",
        "async def helper():\n    await foo()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _async_test_without_async_behavior_violations(tree), f"lens should NOT flag:\n{source}"


def _async_fixture_without_async_behavior_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``async def`` fixture whose
    body contains no async construct at all (including async comprehensions)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not any(_decorator_name(d) == "fixture" for d in node.decorator_list):
            continue
        if _function_is_async(node):
            continue
        found.append(
            (
                node.lineno,
                f"async fixture {node.name}() — body never awaits, declares no async with/for, "
                "yields nothing, and has no async comprehension; declare it a plain def",
            )
        )
    return found


def test_no_async_fixture_without_async_behavior():
    """``async def`` fixtures whose body contains no async construct — no
    ``await``, ``async with``, ``async for``, or ``yield`` — run plain
    synchronous setup on the event loop for no reason. They are the fixture
    twin of the needlessly-async-test lens: the coroutine boundary is a
    silent-false-green hazard, because the first edit that calls an ``async``
    helper and forgets the ``await`` will silently build the fixture from a
    coroutine object instead of failing. A sync fixture is requested the same
    way by async tests, so declaring it a plain ``def`` costs nothing and is
    strictly more permissive (a sync test can also request it). Unlike the
    test twin, a ``pass``-only body is flagged too: the no-op-test lens skips
    fixtures, so a pass-only async fixture has no other net. ``yield``-based
    async generators, ``await``, ``async with``, ``async for``, and async
    comprehensions are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _async_fixture_without_async_behavior_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} async fixture(s) that never suspend.\n"
        "An async fixture whose body contains no await/async with/async for/yield is a "
        "needless coroutine boundary — and a future unawaited call inside it would "
        "silently build the fixture from a coroutine object instead of failing.\n"
        "Declare it a plain 'def' so the setup cannot silently pass.\n" + "\n".join(violations)
    )


def test_async_fixture_lens_flags_needlessly_async_fixtures():
    """Synthetic positive/negative control for the needlessly-async-fixture
    lens: must flag ``async def`` fixtures (``@pytest.fixture`` /
    ``@pytest_asyncio.fixture``, including ``pass``-only bodies) whose body
    contains no async construct and ignore fixtures that actually suspend
    (``await``, ``async with``, ``async for``), async generators (``yield``),
    sync fixtures, and tests."""
    positive_sources = [
        "@pytest.fixture\nasync def make_thing():\n    return 1\n",
        "@pytest.fixture(autouse=True)\nasync def ensure_setup():\n    pass\n",
        "@pytest_asyncio.fixture\nasync def broker():\n    b = Broker()\n    b.conn = fake()\n    return b\n",
        "@pytest.fixture\nasync def settings():\n    x = env()\n    return Settings(x)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _async_fixture_without_async_behavior_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "@pytest.fixture\nasync def make_thing():\n    await setup()\n    return 1\n",
        "@pytest.fixture\nasync def conn():\n    async with pool() as c:\n        yield c\n",
        "@pytest.fixture\nasync def feed():\n    async for x in gen():\n        yield x\n",
        "@pytest.fixture\nasync def gen_thing():\n    yield 1\n",
        "@pytest.fixture\ndef make_thing():\n    return 1\n",
        "async def test_foo():\n    return 1\n",
        "@pytest.fixture\nasync def feed():\n    return [x async for x in gen()]\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _async_fixture_without_async_behavior_violations(tree), f"lens should NOT flag:\n{source}"


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


def _unused_parametrize_arg_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize``
    decorator whose declared argname is never referenced in the decorated
    test's body.

    Only decorator applications are considered — a bare ``parametrize(...)``
    call inside a body is not pytest parametrization and belongs to a
    different lens. ``indirect=True`` parametrizes are skipped: there the
    argname names a *fixture*, which pytest resolves by name even when the
    body never mentions it, so "unreferenced" does not mean "unused". For
    the same reason, a name that appears in the body through a fixture
    reference string (``request.getfixturevalue("x")``) is only skipped when
    the parametrize is indirect — a direct parametrize value is bound to a
    local of that exact name and nothing else, so a bare ``Name`` in the
    body is the definitive use check.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if _decorator_name(dec) != "parametrize":
                continue
            if any(kw.arg == "indirect" for kw in dec.keywords):
                continue
            if len(dec.args) < 1 or not isinstance(dec.args[0], ast.Constant):
                continue
            argnames = str(dec.args[0].value)
            body_names = {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}
            for name in (n.strip() for n in argnames.split(",") if n.strip()):
                if name in body_names:
                    continue
                found.append(
                    (
                        dec.lineno,
                        f"parametrize arg {name!r} never referenced in {node.name}() body — "
                        "every case runs the same assertion, so the matrix coverage is illusory",
                    )
                )
    return found


def test_no_unused_parametrize_args():
    """``@pytest.mark.parametrize`` with an argname the test body never
    references runs the same assertion once per case. The parametrize then
    advertises N-way matrix coverage that does not exist: every case is
    behaviourally identical, so a regression is caught by case 1 and reported
    identically N times, and a reader or mutation-testing run believes N
    distinct inputs are covered. Drop the unused parameter — and when that
    leaves no other varying argument, drop the parametrize decorator entirely
    and keep a plain test. ``indirect=True`` parametrizes are exempt: there
    the argname names a fixture, resolved by name even when the body never
    mentions it."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unused_parametrize_arg_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} parametrize argname(s) never referenced in the test body.\n"
        "A parameter the body ignores makes every case identical — the matrix coverage is illusory.\n"
        "Reference the parameter in the assertion, or drop it (and the parametrize when it is\n"
        "the only argument) and keep a plain test.\n" + "\n".join(violations)
    )


def test_unused_parametrize_arg_lens_flags_ignored_cases():
    """Synthetic positive/negative control for the unused-parametrize-arg
    lens: must flag an argname absent from the test body (multi-name
    argnames included, where only the missing name is flagged) and ignore
    referenced args, indirect parametrizes, non-parametrize calls, and
    parametrizes without a string argname."""
    positive_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2])\n    def test_bar(x):\n        assert 1 == 1\n",
        "def test_foo():\n    @pytest.mark.parametrize('endpoint', ENDPOINTS)\n"
        "    def test_bar(endpoint):\n        do_thing()\n",
        "def test_foo():\n    @pytest.mark.parametrize('a,b', [(1, 2)])\n"
        "    def test_bar(a, b):\n        assert a == 1\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unused_parametrize_arg_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2])\n    def test_bar(x):\n        assert x == 1\n",
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', [1, 2], indirect=True)\n"
            "    def test_bar(x):\n        do_thing()\n"
        ),
        "def test_foo():\n    parametrize('x', [1])\n",
        "def test_foo():\n    @pytest.mark.parametrize(CASES, [1, 2])\n    def test_bar(x):\n        assert x == 1\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unused_parametrize_arg_violations(tree), f"lens should NOT flag:\n{source}"


_UNUSED_BUILTIN_FIXTURE_PARAMS = {
    "mocker",
    "monkeypatch",
    "capsys",
    "capsysbinary",
    "capfd",
    "capfdbinary",
    "caplog",
    "recwarn",
    "tmp_path",
    "tmpdir",
}
"""pytest built-in fixtures with *no setup side effect* that the
unused-builtin-fixture lens scans for.

``request`` and ``tmp_path_factory`` are deliberately excluded: pytest-bdd
step functions conventionally carry ``request`` even when the body only
reaches scenario state through fixture names, and ``tmp_path_factory`` is
session-scoped plumbing rather than a per-test capability. Every fixture in
this set, by contrast, is a value the body either uses or has no reason to
request at all."""


def _unused_builtin_fixture_param_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every function parameter that
    names a pytest built-in fixture but is never referenced in the function
    body.

    Only ``Name`` references in the body (its full subtree, so closure use in
    a nested helper counts) are considered usage. A parameter that names a
    built-in fixture but is never read is dead weight: unlike a custom
    fixture, which may exist purely for setup/teardown side effects, the
    fixtures in ``_UNUSED_BUILTIN_FIXTURE_PARAMS`` have no side effect, so
    requesting one without using the value can only mislead a reader into
    believing the test controls that capability.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in node.args.args if a.arg in _UNUSED_BUILTIN_FIXTURE_PARAMS}
        if not params:
            continue
        body_names = {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}
        for name in sorted(params - body_names):
            found.append(
                (
                    node.lineno,
                    f"{name}() built-in fixture parameter never referenced in {node.name}() body — "
                    "the fixture has no setup side effect, so the request is dead weight",
                )
            )
    return found


def test_no_unused_builtin_fixture_params():
    """A function parameter that names a pytest built-in fixture
    (``monkeypatch``, ``mocker``, ``caplog``, ``capsys``, ...) but is never
    referenced in the body requests dead state. Those fixtures have no setup
    side effect — unlike a custom fixture, which a test may request purely to
    run its setup/teardown — so an unreferenced one adds a parameter that
    misleads a reader into believing the test controls (say) environment
    state or captured output when it does neither. Drop the unused parameter.
    ``request`` (pytest-bdd steps conventionally carry it) and
    ``tmp_path_factory`` (session-scoped plumbing) are deliberately exempt."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unused_builtin_fixture_param_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} parameter(s) naming a built-in fixture the body never uses.\n"
        "The fixture has no setup side effect, so the request is dead weight — "
        "drop the parameter.\n" + "\n".join(violations)
    )


def test_unused_builtin_fixture_lens_flags_dead_params():
    """Synthetic positive/negative control for the unused-builtin-fixture
    lens: must flag a built-in fixture parameter absent from the body (async
    and method forms included) and ignore referenced params, ``request``/
    ``tmp_path_factory`` parameters, custom-fixture parameters, and
    ``**kwargs``/positional-only matching."""
    positive_sources = [
        "def test_foo(monkeypatch):\n    assert 1 == 1\n",
        "async def test_foo(caplog):\n    await do_thing()\n",
        "def test_foo():\n    def test_bar(tmp_path):\n        pass\n",
        "class TestFoo:\n    def test_bar(self, capsys) -> None:\n        assert True\n",
        "def _run_probe(monkeypatch, *, cooldown_ok):\n    return cooldown_ok\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unused_builtin_fixture_param_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo(monkeypatch):\n    monkeypatch.setattr(x, 'a', 1)\n",
        "async def test_foo(caplog):\n    await do_thing()\n    assert len(caplog.records) == 0\n",
        "def test_foo():\n    def test_bar(tmp_path):\n        return tmp_path / 'x'\n",
        "class TestFoo:\n    def test_bar(self, capsys) -> None:\n        assert 'x' in capsys.readouterr().out\n",
        "def test_foo(request):\n    return request.getfixturevalue('client')\n",
        "def test_foo(tmp_path_factory):\n    return tmp_path_factory.mktemp('x')\n",
        "def test_foo(tmp_path_factory):\n    pass\n",
        "def test_foo(connector):\n    return connector\n",
        "def test_foo(**kwargs):\n    return kwargs\n",
        "def test_foo(pos_only, /):\n    return pos_only\n",
        "def test_foo(caplog):\n    def inner(caplog):\n        return caplog\n    return inner\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unused_builtin_fixture_param_violations(tree), f"lens should NOT flag:\n{source}"


_SPLIT_ONCE_WITH_METHODS = frozenset({"assert_called_once_with", "assert_awaited_once_with"})
"""The atomic ``_once_with`` assertion methods. Each verifies "called exactly
once AND with these arguments" in a single statement."""

_SPLIT_ONCE_METHODS = frozenset({"assert_called_once", "assert_awaited_once"})
"""The ``_once`` halves of a split pair — verify only "called exactly once"."""

_SPLIT_WITH_METHODS = frozenset({"assert_called_with", "assert_awaited_with"})
"""The ``_with`` halves of a split pair — verify only "last call args match"."""


def _split_once_with_call_assertions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every split call-assertion pair —
    ``<mock>.assert_called_once()``/``<mock>.assert_awaited_once()``
    immediately followed by ``<mock>.assert_called_with(...)``/
    ``<mock>.assert_awaited_with(...)`` on the same mock. The two statements
    are the split twin of the single atomic ``<mock>.assert_called_once_with
    (...)``/``<mock>.assert_awaited_once_with(...)`` check, and written apart
    they can silently drift out of sync. Only immediately-adjacent statements
    are considered, so unrelated assertions between the two halves are not
    flagged."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for i, stmt in enumerate(node.body[:-1]):
            nxt = node.body[i + 1]
            first = _mock_assert_method(stmt)
            second = _mock_assert_method(nxt)
            if first is None or second is None:
                continue
            first_base, first_attr = first
            second_base, second_attr = second
            if first_attr not in _SPLIT_ONCE_METHODS or second_attr not in _SPLIT_WITH_METHODS:
                continue
            if ast.dump(first_base) != ast.dump(second_base):
                continue
            found.append(
                (
                    stmt.lineno,
                    f"{first_attr}() + {second_attr}(...) on the same mock is a split "
                    f"pair — combine into the single atomic {ast.unparse(first_base)}."
                    f"{second_attr[:-5]}_once_with(...) so 'exactly once with these args' "
                    "cannot silently drift out of sync",
                )
            )
    return found


def _mock_assert_method(stmt: ast.stmt) -> tuple[ast.AST, str] | None:
    """If ``stmt`` is an expression statement calling a mock assertion method
    (``assert_called*``/``assert_awaited*``), return ``(receiver, method)``."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    f = stmt.value.func
    if not isinstance(f, ast.Attribute):
        return None
    if f.attr not in (
        _SPLIT_ONCE_METHODS | _SPLIT_WITH_METHODS | {"assert_called_once_with", "assert_awaited_once_with"}
    ):
        return None
    if not isinstance(f.value, (ast.Name, ast.Attribute, ast.Subscript)):
        return None
    return f.value, f.attr


def test_no_split_once_with_call_assertions():
    """``<mock>.assert_called_once()`` immediately followed by
    ``<mock>.assert_called_with(...)`` on the same mock is the two-line split
    of the single atomic ``<mock>.assert_called_once_with(...)`` check. Written
    apart, the pair can silently drift out of sync — one half edited, the other
    forgotten — so the combined "exactly once with these args" guarantee that
    ``assert_called_once_with`` enforces in one statement is only an accident
    of the current two-line form. Merge the pair into the ``_once_with`` form.
    Only immediately-adjacent statements are flagged, so the deliberate
    "assert the count, then assert the args after more work" idiom is left
    alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _split_once_with_call_assertions(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} split assert_called_once + assert_called_with pair(s).\n"
        "A call-count assertion immediately followed by an args assertion on the same mock is the\n"
        "split twin of the atomic assert_called_once_with/assert_awaited_once_with check. Merge them:\n"
        "    mock.assert_called_once()          ->  mock.assert_called_once_with(expected)\n"
        "    mock.assert_called_with(expected)\n" + "\n".join(violations)
    )


def test_split_once_with_lens_flags_redundant_pairs():
    """Synthetic positive/negative control for the split-assertion lens: it
    must flag an ``assert_called_once``/``assert_awaited_once`` immediately
    followed by an ``assert_called_with``/``assert_awaited_with`` on the same
    mock, and ignore standalone ``_once`` calls, ``_once_with`` calls (already
    atomic), args assertions without the count twin, pairs on *different*
    mocks, and pairs separated by an intermediate statement."""
    positive_sources = [
        "def test_foo():\n    mock.assert_called_once()\n    mock.assert_called_with(1, 2)\n",
        "def test_foo():\n    mock_async.assert_awaited_once()\n    mock_async.assert_awaited_with(record)\n",
        (
            "def test_foo():\n    self.provider.execute.assert_called_once()\n"
            "    self.provider.execute.assert_called_with('x')\n"
        ),
        "def test_foo():\n    mock.assert_awaited_once()\n    mock.assert_awaited_with()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _split_once_with_call_assertions(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    mock.assert_called_once()\n",
        "def test_foo():\n    mock.assert_called_once_with(1, 2)\n",
        "def test_foo():\n    mock.assert_called_with(1, 2)\n",
        "def test_foo():\n    mock_a.assert_called_once()\n    mock_b.assert_called_with(1, 2)\n",
        (
            "def test_foo():\n"
            "    mock.assert_called_once()\n"
            "    result = do_work()\n"
            "    mock.assert_called_with(result)\n"
        ),
        "def test_foo():\n    mock.assert_called_once()\n    mock.assert_not_called()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _split_once_with_call_assertions(tree), f"lens should NOT flag:\n{source}"


def _async_decorator_on_sync_function_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every asynchronous decorator
    applied to a *synchronous* ``def``: ``@pytest.mark.asyncio`` /
    ``@pytest.mark.anyio`` on a plain ``def``, and ``@pytest_asyncio.fixture``
    on a plain ``def`` fixture.

    A plain ``def`` can never suspend — no ``await``, ``async with``, or
    ``async for`` (they are syntax errors), and no ``yield`` that would make
    the marker meaningful. With ``asyncio_mode = auto``, pytest-asyncio
    already infers async behaviour from ``async def``, so an async marker on a
    ``def`` is a needless coroutine boundary: at best a misleading no-op
    (readers expect the body to suspend when it never does) and at worst a
    runtime mismatch (a sync fixture whose value pytest-asyncio expects to
    await). These are almost always the leftover twin of the needlessly-async
    conversion — when an ``async def`` was flipped to a plain ``def``, its
    async decorator should have gone too. ``async def`` functions are never
    flagged.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            name = _decorator_name(func)
            qualname = ast.unparse(func)
            if name in ("asyncio", "anyio"):
                found.append(
                    (
                        dec.lineno,
                        f"sync def {node.name}() marked @{qualname} — the async marker is a "
                        "no-op on a non-coroutine function (asyncio_mode=auto already infers "
                        "async behaviour from async def)",
                    )
                )
            elif name == "fixture" and "asyncio" in qualname:
                found.append(
                    (
                        dec.lineno,
                        f"sync def fixture {node.name}() decorated with @{qualname} — use "
                        "@pytest.fixture for a synchronous fixture body",
                    )
                )
    return found


def test_no_async_decorator_on_sync_function():
    """``@pytest.mark.asyncio``/``@pytest.mark.anyio`` on a plain ``def``, and
    ``@pytest_asyncio.fixture`` on a plain ``def`` fixture, are needless
    coroutine boundaries. A synchronous function can never suspend, so under
    ``asyncio_mode = auto`` the marker is at best a misleading no-op (readers
    expect the body to run on the loop) and at worst a runtime mismatch (a
    sync fixture whose value pytest-asyncio expects to await). These are the
    leftover twin of the needlessly-async conversion: drop the async decorator
    alongside the ``async`` keyword (or, for a sync fixture, switch to plain
    ``@pytest.fixture``)."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _async_decorator_on_sync_function_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} async decorator(s) on synchronous function(s).\n"
        "A plain 'def' can never suspend, so @pytest.mark.asyncio/@pytest.mark.anyio on one is a\n"
        "needless coroutine boundary — and @pytest_asyncio.fixture on a sync fixture should be\n"
        "@pytest.fixture. These are leftovers from flipping async def -> def; remove them.\n" + "\n".join(violations)
    )


def test_async_decorator_on_sync_lens_flags_leftover_markers():
    """Synthetic positive/negative control for the async-decorator-on-sync-
    function lens: it must flag ``@pytest.mark.asyncio``/``@pytest.mark.anyio``
    on a plain ``def`` and ``@pytest_asyncio.fixture`` on a sync fixture, and
    ignore ``async def`` functions with the same decorators, plain ``def``
    without async decorators, and non-async decorators."""
    positive_sources = [
        "def test_foo():\n    @pytest.mark.asyncio\n    def test_bar():\n        assert x == 1\n",
        "def test_foo():\n    @pytest.mark.anyio\n    def test_bar():\n        assert x == 1\n",
        "def test_foo():\n    @pytest.mark.asyncio(loop_scope='session')\n    def test_bar():\n        assert x == 1\n",
        ("def test_foo():\n    @pytest_asyncio.fixture\n    def settings_mock():\n        return MagicMock()\n"),
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _async_decorator_on_sync_function_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "async def test_foo():\n    @pytest.mark.asyncio\n    async def test_bar():\n        await work()\n",
        "def test_foo():\n    def test_bar():\n        assert x == 1\n",
        "def test_foo():\n    @pytest.mark.integration\n    def test_bar():\n        assert x == 1\n",
        ("def test_foo():\n    @pytest_asyncio.fixture\n    async def seeded_db():\n        await work()\n"),
        "def test_foo():\n    @pytest.fixture\n    def settings_mock():\n        return MagicMock()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _async_decorator_on_sync_function_violations(tree), f"lens should NOT flag:\n{source}"


def _equality_chain_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test is a
    *chained equality* — an ``==`` comparison with two or more operators
    (``assert a == b == c``). Range checks (``assert lo <= x <= hi``) use
    ordering operators and are deliberately not matched: a bounds assertion is
    a single fact that reads naturally as a chain, whereas an equality chain
    asserts *N* independent equalities as one expression. When an equality
    chain fails, pytest reports the whole chain and cannot say which link
    broke — a mutation-testing run that severs the middle relationship gets
    the same opaque failure every time. Split each link into its own assert so
    each failure names the pair that broke.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) < 2:
            continue
        if not all(isinstance(op, ast.Eq) for op in test.ops):
            continue
        links = []
        left = test.left
        for right in test.comparators:
            links.append(f"{ast.unparse(left)} == {ast.unparse(right)}")
            left = right
        found.append(
            (
                node.lineno,
                "chained equality in an assert — pytest reports the whole chain and cannot say "
                "which link broke; split into separate asserts:\n        " + "\n        ".join(links),
            )
        )
    return found


def test_no_equality_chain_asserts():
    """An ``assert`` whose test is a chained equality comparison
    (``assert a == b == c``) asserts *N* independent equalities as one
    expression. When the chain fails, pytest rewrites and reports the whole
    chain but cannot say which link broke — the failure message is identical
    whether ``a != b`` or ``b != c``, so a mutation-testing run that severs
    the middle relationship reports the same opaque failure every time. Range
    checks (``assert lo <= x <= hi``) are a single bounds fact and are
    deliberately exempt: only ``==`` chains are flagged. Split each link into
    its own ``assert`` so each failure names the pair that broke."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _equality_chain_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} chained equality assert(s).\n"
        "An equality chain (a == b == c) asserts N independent facts as one expression; pytest\n"
        "reports the whole chain and cannot say which link broke. Split each link into its own\n"
        "assert (range checks like 'lo <= x <= hi' are exempt).\n" + "\n".join(violations)
    )


def test_equality_chain_lens_flags_opaque_chains():
    """Synthetic positive/negative control for the equality-chain lens: it must
    flag ``assert a == b == c`` (attribute, call, subscript, and longer chains
    included) and ignore range checks (ordering operators), single ``==``
    comparisons, and equality chains outside an ``assert``."""
    positive_sources = [
        "def test_foo():\n    assert a == b == c\n",
        "def test_foo():\n    assert result.value == expected.value == 5\n",
        "def test_foo():\n    assert chain.context.trace_id == expected == root.context.trace_id\n",
        "def test_foo():\n    assert get_a() == get_b() == get_c() == get_d()\n",
        "def test_foo():\n    assert keys[0] == keys[1] == 'run-1'\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _equality_chain_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert a == b\n",
        "def test_foo():\n    assert 0 <= a <= 1\n",
        "def test_foo():\n    assert lo < a < hi\n",
        "def test_foo():\n    assert a == b or a == c\n",
        "def test_foo():\n    assert a == b and b == c\n",
        "def test_foo():\n    x = a == b == c\n",
        "def test_foo():\n    if a == b == c:\n        pass\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _equality_chain_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _duplicate_parametrize_case_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize`` whose
    ``argvalues`` holds a *duplicate* case.

    Reuses the shared decorator walk from ``_parametrize_argvalue_lists``
    rather than re-implementing it. ``argvalues`` must be a ``list``/``tuple``
    literal whose elements are all statically evaluable; if any element is not
    (a call, a variable, a ``pytest.param(...)`` wrapper), the whole list is
    skipped because equality with a runtime value cannot be decided
    statically. Cases are compared by value after ``ast.literal_eval`` and
    ``repr``, so only byte-identical values (``1`` vs ``True`` are distinct)
    are flagged.
    """
    found = []
    for lineno, elts in _parametrize_argvalue_lists(tree):
        keys: list[str] = []
        for element in elts:
            try:
                keys.append(repr(ast.literal_eval(element)))
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                keys = []
                break
        if not keys:
            continue
        seen: dict[str, list[int]] = {}
        for key, element in zip(keys, elts, strict=True):
            seen.setdefault(key, []).append(element.lineno)
        for key, lines in seen.items():
            if len(lines) < 2:
                continue
            found.append(
                (
                    lineno,
                    f"parametrize case {key} appears {len(lines)} times (lines {lines}) — "
                    "duplicate cases run the same assertion with identical inputs, "
                    "so the advertised matrix coverage is inflated",
                )
            )
    return found


def test_no_duplicate_parametrize_cases():
    """``@pytest.mark.parametrize`` whose ``argvalues`` holds a duplicate case
    runs the test body twice with *identical* inputs — the second run adds no
    coverage while the parametrize advertises one more distinct case than it
    exercises. It is almost always a copy-paste leftover: a case duplicated
    while editing the list, or a list trimmed in place without removing the
    now-redundant twin. A reader — and a mutation-testing run — believes N
    distinct inputs are covered when only N-1 are, so a regression in the
    behaviour the duplicate case was meant to pin is masked by the twin. Drop
    the duplicate case (and its ``id`` when ``ids=`` is a parallel list). The
    single-case and empty-case lenses guard the degenerate ends of the case
    list; this lens guards the copy-paste trap in the middle."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _duplicate_parametrize_case_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} parametrize case(s) duplicated in argvalues.\n"
        "A duplicate case runs the same assertion with identical inputs — the matrix coverage "
        "is inflated, not extended. Remove the redundant twin (and its id when ids= is parallel).\n"
        + "\n".join(violations)
    )


def test_duplicate_parametrize_lens_flags_repeated_cases():
    """Synthetic positive/negative control for the duplicate-case parametrize
    lens: it must flag a case that repeats in ``argvalues`` (list or tuple,
    scalars, tuples, dicts, declared positionally or via ``argvalues=``) and
    ignore unique case lists, parametrizes with a non-literal case list, and
    parametrizes with a non-evaluable element anywhere in the list."""
    positive_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2, 2])\n    def test_bar(x):\n        assert x == 1\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', ('a', 'a', 'b'))\n    def test_bar(x):\n        assert x\n",
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('a,b', [(1, 2), (3, 4), (1, 2)])\n"
            "    def test_bar(a, b):\n        assert a + b == 3\n"
        ),
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', argvalues=[{'k': 1}, {'k': 1}])\n"
            "    def test_bar(x):\n        assert x['k'] == 1\n"
        ),
        "def test_foo():\n    @pytest.mark.parametrize('x', [0.5, 0.5])\n"
        "    def test_bar(x):\n        assert x == 0.5\n",
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', [1, 2, 1])\n"
            "    @pytest.mark.parametrize('y', [3])\n"
            "    def test_bar(x, y):\n        assert x + y == 4\n"
        ),
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _duplicate_parametrize_case_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, 2, 3])\n    def test_bar(x):\n        assert x == 1\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', [1, True])\n    def test_bar(x):\n        assert x\n",
        "def test_foo():\n    @pytest.mark.parametrize('x', CASES)\n    def test_bar(x):\n        assert x\n",
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', [1, make_case()])\n"
            "    def test_bar(x):\n        assert x == 1\n"
        ),
        "def test_foo():\n    @pytest.mark.parametrize('x', [1])\n    def test_bar(x):\n        assert x == 1\n",
        "def test_foo():\n    parametrize('x', [1, 1])\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _duplicate_parametrize_case_violations(tree), f"lens should NOT flag:\n{source}"


def _skip_condition_truthiness(node: ast.AST) -> bool | None:
    """Return the truthiness of a *literal* skip condition expression, or
    ``None`` when the expression is not statically foldable (a name, call,
    attribute, comparison, binary op, ...). Folding is deliberately shallow:
    constants, container literals, and a ``not`` wrapper are the shapes a
    leftover literal condition takes in practice."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, complex):
            return None
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _skip_condition_truthiness(node.operand)
        return None if inner is None else (not inner)
    return None


def _constant_condition_skip_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@pytest.mark.skipif`` /
    ``@pytest.mark.xfail`` marker whose condition is a statically-foldable
    literal, including the module-level ``pytestmark = pytest.mark.skipif(...)``
    form. A literal condition decides the skip at source time: ``skipif(True)``
    permanently deselects the test from every run and ``skipif(False)`` is a
    dead marker that never triggers — both are almost always leftovers from
    temporarily disabling a test during debugging. Dynamic conditions (names,
    calls, comparisons, attributes) are left alone: they evaluate at collection
    time and are the legitimate form. The skip-without-reason lens already
    owns the missing-``reason`` half of these markers, so only the condition
    is checked here."""

    def _marker_condition(marker: ast.AST) -> tuple[str, ast.expr] | None:
        """Return ``(marker_name, condition_expr)`` for a
        ``pytest.mark.skipif/xfail`` marker application, or ``None``. For
        ``skipif`` the condition is the first positional arg or the
        ``condition=`` keyword; for ``xfail`` only the unambiguous
        ``condition=`` keyword is considered (its positional slot historically
        doubled as ``reason``)."""
        if not isinstance(marker, ast.Call):
            return None
        func = marker.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name not in ("skipif", "xfail"):
            return None
        for kw in marker.keywords:
            if kw.arg == "condition":
                return name, kw.value
        if name == "skipif" and marker.args:
            return name, marker.args[0]
        return None

    found = []

    def _report(marker: ast.AST, context: str, lineno: int) -> None:
        hit = _marker_condition(marker)
        if hit is None:
            return
        name, condition = hit
        truthy = _skip_condition_truthiness(condition)
        if truthy is None:
            return
        if truthy:
            found.append(
                (
                    lineno,
                    f"{context}pytest.mark.{name} with a constant condition {ast.unparse(condition)} — "
                    "always skips, so the item never runs (replace with a real condition or "
                    "delete the marker entirely)",
                )
            )
        else:
            found.append(
                (
                    lineno,
                    f"{context}pytest.mark.{name} with a constant condition {ast.unparse(condition)} — "
                    "never skips, so the marker is dead code (delete it)",
                )
            )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                _report(dec, "@", node.lineno)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            _report(node.value, "pytestmark = ", node.lineno)
    return found


def test_no_constant_condition_skips():
    """A ``@pytest.mark.skipif``/``@pytest.mark.xfail`` (or module-level
    ``pytestmark = ...``) whose condition is a literal constant decides the skip
    at source time and silently weakens the run set. ``skipif(True, ...)``
    permanently deselects the test — the same coverage loss as a plain ``@skip``
    marker, but spelled "conditionally" so it reads as deliberate — and
    ``skipif(False, ...)`` is a dead marker that never triggers while readers
    believe it does. Both are almost always leftovers from temporarily disabling
    a test during debugging. Dynamic conditions (``sys.platform == ...``,
    ``not openssl_available``, an env lookup, ...) are legitimate — they
    evaluate at collection time — and the skip-without-reason lens already
    guards the missing-``reason`` half of these markers, so this lens only
    checks the condition."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _constant_condition_skip_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} skip marker(s) with a literal constant condition.\n"
        "A constant condition decides the skip at source time: always-skip silently deselects "
        "the test from every run, and never-skip is a dead marker. Replace it with a real "
        "condition or delete the marker.\n" + "\n".join(violations)
    )


def test_constant_condition_skip_lens_flags_deterministic_skips():
    """Synthetic positive/negative control for the constant-condition skip lens:
    it must flag ``skipif``/``xfail`` markers whose condition is foldable
    (``True``/``False``, numeric, an empty container, a ``not``-wrapped
    literal, positional or ``condition=``, on a function, class, or module-level
    ``pytestmark``) and ignore dynamic conditions (names, calls, comparisons,
    attribute lookups) plus non-skipif markers."""
    positive_sources = [
        "@pytest.mark.skipif(True, reason='temp')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(False, reason='legacy')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(1, reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif('', reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif([], reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(condition=True, reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(not 0, reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.xfail(condition=False, reason='known bug')\ndef test_foo():\n    assert x\n",
        "class TestFoo:\n    @pytest.mark.skipif(True, reason='x')\n    def test_bar(self):\n        assert x\n",
        "pytestmark = pytest.mark.skipif(True, reason='x')\ndef test_foo():\n    assert x\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _constant_condition_skip_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "@pytest.mark.skipif(sys.version_info < (3, 9), reason='py')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(not openssl_available, reason='openssl')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(sys.platform == 'win32', reason='win')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(platform.system() == 'Windows', reason='win')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(condition=some_flag(), reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(FLAG, reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skip(reason='x')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.xfail(reason='known bug')\ndef test_foo():\n    assert x\n",
        "pytestmark = pytest.mark.skipif(_redis_reachable(), reason='redis')\ndef test_foo():\n    assert x\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _constant_condition_skip_violations(tree), f"lens should NOT flag:\n{source}"


_RAISES_CONTEXT_FUNCS = frozenset({"raises", "warns"})
"""``with`` context-manager names whose expected-exception argument is checked
for broad classes. Custom helpers (``assert_raises``, ``rejects``, ...) are
deliberately not matched: their signature does not necessarily take an
exception class positionally, and only ``pytest.raises``/``pytest.warns``
have the ``match=`` keyword that narrows an ``AssertionError`` expectation."""

_BROAD_EXCEPTION_CLASSES = frozenset({"Exception", "BaseException"})
"""Exception classes that can never be a *specific* expected error: ``Exception``
catches every failure (including ``AssertionError`` and any unrelated bug in
the code under test) and ``BaseException`` widens that to
``KeyboardInterrupt``/``SystemExit``."""


def _broad_exception_catch_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``pytest.raises`` /
    ``pytest.warns`` whose expected exception is a broad class — ``Exception``,
    ``BaseException``, or ``AssertionError`` (raises only) without a ``match=``
    narrow — plus every ``@pytest.mark.xfail(raises=...)`` marker naming a
    broad class.

    For ``raises``/``warns`` the expected exception may be given positionally,
    as an ``expected_exception=`` keyword, or inside a union tuple; any broad
    class inside a union is flagged. ``@pytest.mark.xfail(raises=...)`` is the
    marker twin: the test then xfails on any exception, so it stays green no
    matter which error the code raises. ``pytest.skip.Exception``/
    ``pytest.xfail.Exception`` (attribute access) are skipped: those name the
    precise internal exception ``pytest.skip()``/``pytest.xfail()`` raise and
    are the deliberate assertion those calls happened. An ``AssertionError``
    expectation is only flagged when the marker has no ``match=`` keyword —
    ``match=`` pins the check to a specific message, which is the safe way to
    test an assert-based validator's contract.
    """
    found = []

    def _is_broad(expr: ast.AST) -> bool:
        return isinstance(expr, ast.Name) and expr.id in _BROAD_EXCEPTION_CLASSES

    def _report(lineno: int, detail: str) -> None:
        found.append((lineno, detail))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if name not in _RAISES_CONTEXT_FUNCS:
            continue
        has_match = any(kw.arg == "match" for kw in node.keywords)
        exception_args = list(node.args)
        for kw in node.keywords:
            if kw.arg == "expected_exception":
                exception_args.append(kw.value)
        for arg in exception_args:
            classes: list[str] = []
            if isinstance(arg, ast.Name):
                classes.append(arg.id)
            elif isinstance(arg, ast.Tuple):
                classes.extend(el.id for el in arg.elts if isinstance(el, ast.Name))
            elif isinstance(arg, ast.Attribute):
                continue
            for cls in classes:
                if cls in _BROAD_EXCEPTION_CLASSES:
                    _report(
                        node.lineno,
                        f"pytest.{name}({cls}) catches any {cls} — a regression that raises a "
                        "different exception (or the test's own assertion error) still reports "
                        "green; name the specific exception the code is documented to raise",
                    )
                    break
                if cls == "AssertionError" and name == "raises" and not has_match:
                    _report(
                        node.lineno,
                        "pytest.raises(AssertionError) without match= — an internal assert bug in "
                        "the code under test is swallowed as the expected exception; pin the message "
                        "with match= or expect the specific error",
                    )
                    break

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            dname = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
            if dname != "xfail":
                continue
            for kw in dec.keywords:
                if kw.arg == "raises" and _is_broad(kw.value):
                    _report(
                        dec.lineno,
                        f"@pytest.mark.xfail(raises={ast.unparse(kw.value)}) — xfails on any "
                        "exception, so the test stays green no matter which error the code raises; "
                        "name the concrete exception type",
                    )
    return found


def test_no_broad_exception_catch_asserts():
    """``pytest.raises``/``pytest.warns`` — and ``@pytest.mark.xfail(raises=...)``
    markers — naming ``Exception``, ``BaseException``, or ``AssertionError``
    (raises only, without a ``match=`` narrow) are catch-alls that silently
    mask regressions. ``Exception`` catches every failure, so a bug that makes
    the code under test raise the *wrong* exception (or an unrelated error
    entirely) still reports green; pytest itself documents ``pytest.raises`` as
    "strongly encouraged" to be used with a specific exception type, precisely
    because a broad catch turns the test into a smoke test. ``BaseException``
    widens the mask to ``KeyboardInterrupt``/``SystemExit``. ``AssertionError``
    without ``match=`` is the sharpest hazard: an *internal* assert bug in the
    code under test is swallowed as the expected exception, so a validator that
    regresses to raise for the wrong reason passes. A ``match=`` narrows the
    message, not the type, so it does not rescue the call. Name the specific
    exception, and pin assert-based validator contracts with ``match=``
    instead."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _broad_exception_catch_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} broad-exception pytest.raises/pytest.warns/xfail catch(es).\n"
        "pytest.raises(Exception)/BaseException catches every failure — a regression that raises\n"
        "the wrong exception, and the test's own assertion errors — so the test reports green instead\n"
        "of failing. Name the specific exception the code is documented to raise, and pin assert-based\n"
        "validators with match= instead of bare pytest.raises(AssertionError).\n" + "\n".join(violations)
    )


def test_broad_exception_catch_lens_flags_broad_catches():
    """Synthetic positive/negative control for the broad-exception-catch lens,
    mirroring the constant-condition-skip lens pattern: it must flag
    ``pytest.raises``/``pytest.warns`` whose expected exception is ``Exception``
    or ``BaseException`` (positional, in a union tuple, or via
    ``expected_exception=``), ``pytest.raises(AssertionError)`` without
    ``match=``, and ``@pytest.mark.xfail(raises=...)`` markers naming a broad
    class; and ignore specific exceptions, unioned specific exceptions,
    ``AssertionError`` narrowed by ``match=``, ``pytest.skip.Exception`` /
    ``pytest.xfail.Exception`` attribute forms, and arg-less ``pytest.warns()``."""
    positive_sources = [
        "def test_foo():\n    with pytest.raises(Exception):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(BaseException):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(AssertionError):\n        _assert_valid(block)\n",
        "def test_foo():\n    with pytest.raises(expected_exception=Exception):\n        foo()\n",
        "def test_foo():\n    with pytest.raises((Exception, ValueError)):\n        foo()\n",
        "def test_foo():\n    with pytest.warns(Exception):\n        foo()\n",
        "def test_foo():\n    with pytest.warns(BaseException):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(AssertionError):\n        _validate(record)\n",
        "def test_foo():\n    pytest.raises(Exception)\n",
        "def test_foo():\n    with pytest.raises(Exception, match='boom'):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(BaseException) as exc_info:\n        foo()\n",
        "class TestFoo:\n    def test_bar(self):\n        with pytest.raises(Exception):\n            foo()\n",
        "@pytest.mark.xfail(raises=Exception, reason='known')\ndef test_foo():\n    foo()\n",
        "@pytest.mark.xfail(raises=BaseException, reason='known')\ndef test_foo():\n    foo()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _broad_exception_catch_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    with pytest.raises(ValueError):\n        foo()\n",
        "def test_foo():\n    with pytest.raises((ValueError, TypeError)):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(ValueError, match='boom'):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(KeyError):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(AssertionError, match='too many context elements'):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(CustomError):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(pytest.skip.Exception):\n        pytest.skip('not implemented')\n",
        "def test_foo():\n    with pytest.raises(pytest.xfail.Exception):\n        pytest.xfail('known bug')\n",
        "def test_foo():\n    with pytest.raises(asyncio.CancelledError):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(HTTPException):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(EXC):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(ExceptionGroup('e', [])):\n        foo()\n",
        "def test_foo():\n    with pytest.warns(UserWarning):\n        foo()\n",
        "def test_foo():\n    with pytest.warns():\n        foo()\n",
        "@pytest.mark.xfail(raises=ValueError, reason='known')\ndef test_foo():\n    foo()\n",
        "@pytest.mark.xfail(reason='known')\ndef test_foo():\n    foo()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _broad_exception_catch_violations(tree), f"lens should NOT flag:\n{source}"


def _unentered_raises_context_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``pytest.raises`` /
    ``pytest.warns`` call that stands as a bare expression statement.

    The call constructs the ``RaisesContext``/``WarningsChecker`` context
    manager but never enters it, so the exception (or warning) it claims to
    expect is never actually checked — the test passes whether the code under
    test raises the expected error, raises the *wrong* error, or raises
    nothing at all. It is the missing-``with`` twin of the broad-exception
    lens: ``pytest.raises(X)`` as a statement is a silent false green that the
    broad lens only happens to catch when ``X`` is ``Exception``/
    ``BaseException``. The deprecated functional form ``pytest.raises(X, func,
    *args)`` (two or more positional arguments) actually executes the check
    and is deliberately left alone, as are the ``with``-entered and decorator
    spellings — those never appear as a bare expression statement.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        f = value.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if name not in _RAISES_CONTEXT_FUNCS:
            continue
        if len(value.args) > 1:
            continue
        found.append(
            (
                node.lineno,
                f"pytest.{name}(...) as a bare statement — the raises/warns context manager is "
                "never entered with 'with', so no exception/warning is ever checked and the test "
                "passes whether or not the code under test raises it; wrap the call in "
                f"'with {name}(...):'",
            )
        )
    return found


def test_no_unentered_raises_contexts():
    """A ``pytest.raises``/``pytest.warns`` call that stands as its own bare
    expression statement constructs the context manager but never enters it,
    so the exception (or warning) it claims to expect is never actually
    checked. The test passes whether the code under test raises the expected
    error, raises the *wrong* error, or raises nothing at all — the call is
    dead code that looks like a verification. Wrap it in ``with ...:`` so the
    expected exception or warning actually fails the test when it does not
    occur."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unentered_raises_context_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} bare pytest.raises/pytest.warns statement(s) whose context "
        "manager is never entered.\n"
        "pytest.raises(...)/pytest.warns(...) must be wrapped in 'with ...:' — as a bare statement "
        "the exception/warning it expects is never checked and the test passes regardless of "
        "whether the code under test raises it.\n" + "\n".join(violations)
    )


def test_unentered_raises_context_lens_flags_dead_statements():
    """Synthetic positive/negative control for the unentered-raises-context
    lens: it must flag ``pytest.raises``/``pytest.warns`` standing as bare
    expression statements (any expected-exception spelling, attribute or
    imported-name form, with or without ``match=``), and ignore the
    ``with``-entered and decorator spellings, the deprecated functional form
    (which actually executes), and calls that are assigned or passed as
    arguments instead of being statements."""
    positive_sources = [
        "def test_foo():\n    pytest.raises(ValueError)\n    assert foo() == 1\n",
        "def test_foo():\n    pytest.raises(KeyError, match='boom')\n",
        "def test_foo():\n    pytest.raises(expected_exception=RuntimeError)\n",
        "def test_foo():\n    pytest.warns(UserWarning)\n",
        "def test_foo():\n    pytest.warns()\n",
        "def test_foo():\n    from pytest import raises\n    raises(ValueError)\n",
        "def test_foo():\n    pytest.raises(asyncio.CancelledError)\n    assert True\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unentered_raises_context_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    with pytest.raises(ValueError):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(ValueError) as exc_info:\n        foo()\n",
        "def test_foo():\n    with pytest.warns(UserWarning):\n        foo()\n",
        "def test_foo():\n    pytest.raises(ValueError, foo)\n",
        "def test_foo():\n    pytest.raises(ValueError, foo, 1, key=2)\n",
        "@pytest.raises(ValueError)\ndef test_foo():\n    foo()\n",
        "def test_foo():\n    cm = pytest.raises(ValueError)\n    with cm:\n        foo()\n",
        "def test_foo():\n    mock.assert_called_with(pytest.raises(ValueError))\n",
        "def test_foo():\n    result = pytest.raises(ValueError)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unentered_raises_context_violations(tree), f"lens should NOT flag:\n{source}"


_MOCK_CONSTRUCTOR_NAMES = frozenset(
    {"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "PropertyMock"}
)


def _is_mock_constructor_call(node: ast.AST) -> bool:
    """True when ``node`` is a direct call to a ``unittest.mock`` / pytest-mock
    Mock constructor — ``Mock()``, ``MagicMock()``, ``AsyncMock()``, ... — in
    any of its spellings (bare ``Mock``, ``mock.Mock``, ``mocker.MagicMock``,
    ``unittest.mock.AsyncMock``). ``PropertyMock`` is included even though it
    is only valid as a ``return_value``/``side_effect`` spec: it is still a
    Mock instance and has no business appearing in an ``assert`` expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _MOCK_CONSTRUCTOR_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in _MOCK_CONSTRUCTOR_NAMES
    return False


def _mock_constructor_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression — or a direct operand of it — *is* a freshly-constructed Mock.

    Only direct constructor-call positions are checked: the assertion whose
    test *is* the call, a ``not``-wrapped call, and the operands of a
    single-operator comparison. No name resolution is involved, so a mock
    bound to a variable elsewhere in the file is never implicated and the
    lens has no false positives from mocking assignments or ``patch``
    bindings."""
    found: list[tuple[int, str]] = []

    def _report(lineno: int, detail: str) -> None:
        found.append(
            (
                lineno,
                f"assert {detail} — a fresh Mock instance is always truthy and compares by "
                "identity, so the outcome is fixed at source time",
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if _is_mock_constructor_call(test):
            _report(node.lineno, ast.unparse(test))
            continue
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _is_mock_constructor_call(test.operand):
            _report(node.lineno, ast.unparse(test))
            continue
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            for operand in (test.left, test.comparators[0]):
                if _is_mock_constructor_call(operand):
                    _report(node.lineno, ast.unparse(test))
                    break
    return found


def test_no_mock_constructor_in_asserts():
    """An ``assert`` that constructs a Mock object directly in its test
    expression is dead code with a fixed outcome, so it reports green — or
    red — regardless of the behaviour under test. Mock instances are always
    truthy, so ``assert Mock()`` is a silent-false-green and ``assert not
    Mock()`` can never pass; two fresh Mock instances also compare by
    identity (``__eq__`` defaults to ``is``), so ``assert x == Mock()``
    always fails and ``assert x != Mock()`` always passes no matter what ``x``
    evaluates to. These are almost always a leftover from inlining a double
    while debugging, where the intended comparison target was accidentally
    replaced by the constructor call. The double should be *configured*
    (``return_value``/``side_effect``) and asserted through
    ``assert_called*``/attribute checks, never compared to directly. The
    identity-with-container-literal and literal-constant lenses own the
    neighbouring shapes; this lens owns the Mock-constructor position."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _mock_constructor_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a freshly-constructed Mock.\n"
        "A Mock instance is always truthy and compares by identity, so the outcome is "
        "fixed at source time. Configure the double (return_value/side_effect) and assert "
        "through assert_called* instead of comparing to a constructor call.\n" + "\n".join(violations)
    )


def test_mock_constructor_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the Mock-constructor lens: it
    must flag an ``assert`` that constructs a Mock directly in the test
    expression (bare, ``not``-wrapped, or as a comparison operand, in any
    constructor spelling) and ignore asserts over already-bound names, mocks
    assigned earlier in the same test, ``assert_called*`` double-verification
    calls, and ``patch``/``with ... as mock`` bindings."""
    positive_sources = [
        "def test_foo():\n    assert Mock()\n",
        "def test_foo():\n    assert MagicMock()\n",
        "def test_foo():\n    assert AsyncMock()\n",
        "def test_foo():\n    assert not MagicMock()\n",
        "def test_foo():\n    assert result == Mock()\n",
        "def test_foo():\n    assert result != MagicMock()\n",
        "def test_foo():\n    assert Mock(spec=int)\n",
        "def test_foo():\n    assert mock.Mock()\n",
        "def test_foo():\n    assert mocker.MagicMock()\n",
        "def test_foo():\n    assert unittest.mock.AsyncMock()\n",
        "def test_foo():\n    assert result == mock.Mock(return_value=3)\n",
        "def test_foo():\n    assert Mock() == Mock()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _mock_constructor_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    mock = Mock()\n    assert mock\n",
        "def test_foo():\n    m = MagicMock()\n    assert not m\n",
        "def test_foo():\n    assert x == mock\n",
        "def test_foo():\n    assert x != mock_var\n",
        "def test_foo():\n    assert result == expected\n",
        "def test_foo():\n    with patch('x') as m:\n        m.assert_called_once()\n",
        "def test_foo():\n    m.assert_called_with(1)\n",
        "def test_foo():\n    assert result is not None\n",
        "def test_foo():\n    assert len(items) == 0\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _mock_constructor_assert_violations(tree), f"lens should NOT flag:\n{source}"


_MOCK_ASSERT_METHOD_NAMES = frozenset(
    {
        "assert_called",
        "assert_called_once",
        "assert_called_with",
        "assert_called_once_with",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_not_called",
        "assert_not_awaited",
        "assert_has_calls",
        "assert_has_awaits",
        "assert_any_call",
        "assert_any_await",
    }
)
"""Attribute names of ``unittest.mock`` verification methods, all of which
return ``None``. ``assert_called*``/``assert_called_with``/``assert_awaited*``
verifications raise ``AssertionError`` on mismatch and return ``None`` on
success; ``assert_not_called``/``assert_not_awaited`` and the ``assert_has_*``/
``assert_any_*`` families return ``None`` unconditionally on success."""


def _mock_assert_in_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression *is* — or ``not``-wraps — a call to a mock verification method.

    Only the two whole-expression positions are checked, so a verification
    call that happens to appear inside a larger expression is never implicated
    and the lens has no false positives. No name resolution is involved: any
    attribute method with one of the ``assert_*`` names in either position is
    a call that returns ``None``, which is exactly the dead-assert hazard."""
    found: list[tuple[int, str]] = []

    def _is_mock_assert_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MOCK_ASSERT_METHOD_NAMES
        )

    def _report(lineno: int, negated: bool, source: ast.AST) -> None:
        verb = "ALWAYS FAILS" if not negated else "ALWAYS PASSES"
        found.append(
            (
                lineno,
                f"assert {'not ' if negated else ''}{ast.unparse(source)} — "
                f"a mock verification method returns None, so this is assert "
                f"None, which {verb} regardless of the recorded calls",
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        if _is_mock_assert_call(node.test):
            _report(node.lineno, negated=False, source=node.test)
            continue
        if (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and _is_mock_assert_call(node.test.operand)
        ):
            _report(node.lineno, negated=True, source=node.test)
    return found


def test_no_mock_assert_as_assertion_expression():
    """An ``assert`` whose test expression is a call to a ``unittest.mock``
    verification method is a fixed-outcome assertion: every ``assert_called*``/
    ``assert_awaited*``/``assert_not_called``/``assert_has_calls`` method
    returns ``None``, so ``assert mock.assert_called()`` evaluates as
    ``assert None`` and ALWAYS FAILS (the test stays red even when the double
    verified its calls correctly), while ``assert not mock.assert_called()``
    evaluates as ``assert not None`` and ALWAYS PASSES no matter what the code
    under test or the double did. Both spellings falsely report on behaviour
    that the verification method itself is the only thing capable of judging,
    and a mutation-testing run trusts the green ``assert not`` form as real
    coverage. Verification methods already raise ``AssertionError`` on mismatch
    — call them as their own statement — and assert on a mock attribute
    (``call_count``/``called``/``call_args``) for the value-level check. The
    concurrent-constructor and redundant-call lenses own the neighbouring
    shapes (fresh ``Mock()`` in a test expression; weak ``assert .called``
    immediately before a recorded-calls access); this lens owns the
    None-returning verification call as the assertion itself."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _mock_assert_in_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) whose expression is a mock verification call.\n"
        "assert_called*/assert_awaited*/assert_not_called/assert_has_calls all return None, so "
        "'assert mock.assert_called()' is always False and 'assert not mock.assert_called()' is "
        "always True. Call the verification method as its own statement (it raises on mismatch) "
        "and assert on a mock attribute for the value.\n" + "\n".join(violations)
    )


def test_mock_assert_lens_flags_none_returning_asserts():
    """Synthetic positive/negative control for the mock-verification-assert
    lens: it must flag an ``assert`` whose test expression is a call to any
    of the None-returning mock verification methods (``assert_called``,
    ``assert_called_once_with``, ``assert_awaited``, ``assert_not_called``,
    ``assert_has_calls``, ...) in either the direct or ``not``-wrapped shape,
    and ignore verification calls used as standalone statements, asserts over
    mock attributes (``called``/``call_count``/``call_args``), ``is not None``
    checks, and any call whose function is not a verification method."""
    positive_sources = [
        "def test_foo():\n    assert mock.assert_called()\n",
        "def test_foo():\n    assert not mock.assert_called()\n",
        "def test_foo():\n    assert mock.assert_called_once()\n",
        "def test_foo():\n    assert mock.assert_called_once_with(1, 2)\n",
        "def test_foo():\n    assert mock.assert_not_called()\n",
        "def test_foo():\n    assert mock.assert_awaited()\n",
        "def test_foo():\n    assert not mock.assert_awaited_once()\n",
        "def test_foo():\n    assert mock.assert_awaited_once_with(x=1)\n",
        "def test_foo():\n    assert mock.assert_not_awaited()\n",
        "def test_foo():\n    assert mock.assert_has_calls([call(1)])\n",
        "def test_foo():\n    assert mock.assert_has_awaits([call(2)])\n",
        "def test_foo():\n    assert mock.assert_any_call(3)\n",
        "def test_foo():\n    assert mocker.patch('x').assert_called_with(1)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _mock_assert_in_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    mock.assert_called()\n",
        "def test_foo():\n    mock.assert_not_called()\n",
        "def test_foo():\n    mock.assert_called_once_with(1)\n",
        "def test_foo():\n    assert mock.called\n",
        "def test_foo():\n    assert not mock.called\n",
        "def test_foo():\n    assert mock.call_count == 1\n",
        "def test_foo():\n    assert mock.call_args.args[0] == 1\n",
        "def test_foo():\n    assert result is not None\n",
        "def test_foo():\n    assert result.compute_total() == 3\n",
        "def test_foo():\n    mock.assert_called()\n    assert mock.call_count == 1\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _mock_assert_in_assert_violations(tree), f"lens should NOT flag:\n{source}"


_MOCK_CALL_VERIFY_METHODS = frozenset(
    {
        "assert_called_with",
        "assert_called_once_with",
        "assert_any_call",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_awaited_any_call",
    }
)
"""The mock call-assertion methods that compare recorded call arguments
against *expected* arguments. ``assert_has_calls``/``assert_has_awaits`` are
deliberately excluded: their expected argument is a list of ``call()``
objects (nested a level down), so a Mock in that position is a different,
less direct shape."""


def _fresh_mock_in_call_assertions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every freshly-constructed Mock
    passed as an *expected* argument to a mock call-assertion
    (``assert_called_with``, ``assert_called_once_with``, ``assert_any_call``,
    and their awaited twins).

    The recorded call is whatever object the code under test actually passed,
    and a fresh Mock ``__eq__`` defaults to identity, so the expected tuple can
    never equal the recorded one — the assertion is dead code that always
    FAILS (and for ``assert_any_call`` no recorded call ever matches). Only
    direct argument positions are checked (positional and keyword values), so
    bound names, already-configured mocks, and mocks nested inside containers
    or ``call(...)`` wrappers are never implicated. This is the
    expected-argument twin of the ``assert``-position lens in
    ``_mock_constructor_assert_violations``.
    """
    found: list[tuple[int, str]] = []

    def _flag(arg: ast.AST, lineno: int, method: str) -> None:
        found.append(
            (
                lineno,
                f"{ast.unparse(arg)} passed as an expected-call argument to {method}() — "
                "a fresh Mock compares by identity, so the recorded call can never equal it "
                "and the assertion always FAILS; configure the double and pass the configured "
                "instance, or assert on the real expected value",
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _MOCK_CALL_VERIFY_METHODS:
            continue
        for arg in node.args:
            if _is_mock_constructor_call(arg):
                _flag(arg, arg.lineno, node.func.attr)
        for kw in node.keywords:
            if kw.arg and _is_mock_constructor_call(kw.value):
                _flag(kw.value, kw.value.lineno, node.func.attr)
    return found


def test_no_fresh_mock_in_call_assertions():
    """``<mock>.assert_called_with(Mock())`` (and ``assert_called_once_with``,
    ``assert_any_call``, plus the awaited twins) declares a fresh Mock as the
    *expected* call argument. The recorded call holds whatever object the code
    under test actually passed, and a new Mock compares by identity (``__eq__``
    defaults to ``is``), so the expected tuple can never equal the recorded
    one: the assertion is dead code that always fails, and an ``assert_any_call``
    with a fresh Mock can never match any recorded call either. This is the
    expected-argument twin of the assert-``Mock()`` lens, and is almost always
    a leftover from inlining a double while debugging — the intended comparison
    target was replaced by the constructor call. Configure the double and pass
    the configured instance (bound to a name), or assert on the real expected
    value."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _fresh_mock_in_call_assertions(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} fresh Mock instance(s) in call-assertion expected arguments.\n"
        "A fresh Mock compares by identity, so the recorded call can never equal it and the "
        "assertion always FAILS. Configure the double (return_value/side_effect), bind it to "
        "a name, and pass that — or assert on the real expected value.\n" + "\n".join(violations)
    )


def test_fresh_mock_in_call_assertions_lens_flags_impossible_expectations():
    """Synthetic positive/negative control for the fresh-Mock-in-call-
    assertion lens: it must flag a Mock constructor in any expected-argument
    position (positional, keyword, sync or awaited method, any constructor
    spelling) and ignore already-bound mock names, configured instances,
    non-assertion mock calls, statements outside the verify methods, and mocks
    nested inside container/call wrappers."""
    positive_sources = [
        "def test_foo():\n    mock.assert_called_with(Mock())\n",
        "def test_foo():\n    mock.assert_called_once_with(MagicMock())\n",
        "def test_foo():\n    mock.assert_any_call(mock.Mock())\n",
        "def test_foo():\n    mock_async.assert_awaited_with(AsyncMock(call_count=2))\n",
        "def test_foo():\n    mock_async.assert_awaited_once_with(return_value=MagicMock())\n",
        "def test_foo():\n    mock_async.assert_awaited_any_call(unittest.mock.NonCallableMock())\n",
        "def test_foo():\n    mocker.client.post.assert_called_once_with(Mock(spec=HTTPResponse))\n",
        "def test_foo():\n    mock.assert_called_once_with(session, Mock(), user.org_id)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _fresh_mock_in_call_assertions(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    mock.assert_called_with(mock_var)\n",
        "def test_foo():\n    mock.assert_called_with(session, user.org_id, ANY)\n",
        "def test_foo():\n    double = Mock()\n    mock.assert_called_once_with(double)\n",
        "def test_foo():\n    mock.assert_called_with(mock.return_value)\n",
        "def test_foo():\n    mock.assert_called_with([Mock()])\n",
        "def test_foo():\n    mock.assert_called_with(call(Mock()))\n",
        "def test_foo():\n    mock.assert_called()\n",
        "def test_foo():\n    mock.call_count == 1\n",
        "def test_foo():\n    mock.side_effect = Mock()\n",
        "def test_foo():\n    mocker.patch('x', return_value=Mock())\n",
        "def test_foo():\n    mock.assert_called_with(await fetch())\n",
        "def test_foo():\n    with patch('x') as m:\n        m.assert_called_once()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _fresh_mock_in_call_assertions(tree), f"lens should NOT flag:\n{source}"


def _complementary_boolean_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assert whose test expression
    is a ``BoolOp`` (or a ``not``-wrapped ``BoolOp``) that joins a value with
    its own syntactic negation.

    An ``and`` conjunction containing a complementary pair is a contradiction
    that always evaluates False (the whole assert ALWAYS FAILS), and an ``or``
    disjunction containing a complementary pair is a tautology that always
    evaluates True (the whole assert ALWAYS PASSES); the ``not``-wrapped twins
    invert the verdict. Either way the outcome is fixed at source time, so the
    assert is dead code that never exercises the code under test. Only direct
    operands of the top-level ``BoolOp`` are compared: complementarity nested
    inside an operand (``assert (x and not x) or y``) does not fix the whole
    outcome and is deliberately left alone."""
    found: list[tuple[int, str]] = []

    def _is_negation(candidate: ast.AST, plain: ast.AST) -> bool:
        return (
            isinstance(candidate, ast.UnaryOp)
            and isinstance(candidate.op, ast.Not)
            and ast.dump(candidate.operand) == ast.dump(plain)
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        if negated:
            test = test.operand
        if not isinstance(test, ast.BoolOp):
            continue
        values = test.values
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                a, b = values[i], values[j]
                if not (_is_negation(a, b) or _is_negation(b, a)):
                    continue
                if isinstance(test.op, ast.And):
                    kind = "contradiction"
                    verdict = "always FAILS" if not negated else "always PASSES"
                else:
                    kind = "tautology"
                    verdict = "always PASSES" if not negated else "always FAILS"
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(node.test)} — operand {ast.unparse(a)} is the "
                        f"negation of {ast.unparse(b)}, a {kind}: {verdict} regardless of the "
                        "code under test",
                    )
                )
                break
            else:
                continue
            break
    return found


def test_no_complementary_boolean_assertions():
    """An ``assert`` whose test expression joins a value with its own negation
    is dead code with a fixed outcome. ``x and not x`` is a contradiction that
    can never be true, so ``assert x and not x`` ALWAYS FAILS no matter what
    the code under test does (the suite is unconditionally red — the same dead
    class as ``assert mock.assert_called()``), and ``x or not x`` is a
    tautology that is always true, so ``assert x or not x`` ALWAYS PASSES and a
    mutation-testing run trusts the green as real verification — the silent
    false green this lens exists to guard. The ``not``-wrapped twins
    (``assert not (x and not x)``, ``assert not (x or not x)``) invert the
    verdict. These are almost always a leftover from pasting a condition into
    an assert while debugging: the code under test has no bearing on the
    outcome, so the assertion either breaks CI unconditionally or reports green
    with zero coverage. Operands are compared by syntax, so attribute paths and
    subscripts are caught too (``assert row['x'] and not row['x']``), while
    complementarity written with mirrored operators (``assert x == y or x !=
    y``) needs operator algebra to prove and is deliberately left alone; the
    literal-constant, self-comparison, and negated-comparison lenses own the
    neighbouring shapes."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _complementary_boolean_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) joining a value with its own negation.\n"
        "x and not x is always False (ALWAYS FAILS) and x or not x is always True "
        "(ALWAYS PASSES), so the outcome never depends on the code under test.\n"
        "Assert the real condition, or drop the dead check.\n" + "\n".join(violations)
    )


def test_complementary_boolean_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the complementary-boolean lens:
    must flag an ``assert`` whose top-level ``BoolOp`` joins a value with its
    own syntactic negation (either operand order, ``and``/``or``, direct or
    ``not``-wrapped, over names/attributes/subscripts) and ignore non-
    complementary conjunctions/disjunctions, single operands, complementarity
    that is nested inside an operand rather than fixing the whole outcome, and
    value-complementarity expressed with mirrored operators."""
    positive_sources = [
        "def test_foo():\n    assert x and not x\n",
        "def test_foo():\n    assert not x and x\n",
        "def test_foo():\n    assert x or not x\n",
        "def test_foo():\n    assert not x or x\n",
        "def test_foo():\n    assert x and y and not x\n",
        "def test_foo():\n    assert not (x and not x)\n",
        "def test_foo():\n    assert not (x or not x)\n",
        "def test_foo():\n    assert row['active'] and not row['active']\n",
        "def test_foo():\n    assert result.enabled or not result.enabled\n",
        "def test_foo():\n    assert not result.enabled and result.enabled\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _complementary_boolean_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x and not y\n",
        "def test_foo():\n    assert x or not y\n",
        "def test_foo():\n    assert x and y\n",
        "def test_foo():\n    assert x or y\n",
        "def test_foo():\n    assert not x\n",
        "def test_foo():\n    assert not (a == 1 and b == 2)\n",
        "def test_foo():\n    assert (x and not x) or y\n",
        "def test_foo():\n    assert x == y or x != y\n",
        "def test_foo():\n    assert x is y or x is not y\n",
        "def test_foo():\n    assert x in y and x not in y\n",
        "def test_foo():\n    assert enabled or not disabled\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _complementary_boolean_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _computed_wall_clock_sleep_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``time.sleep(<name>)`` /
    ``asyncio.sleep(<name>)`` whose duration is a bare name rather than a
    literal constant.

    A sleep whose duration is computed from other values (a refill interval,
    a delay variable, a backoff counter) is a timing-contract check that
    depends on real wall-clock passage: it is slower than needed and flakes
    under load. Literal durations are deliberately NOT flagged — ``sleep(0)``
    is a deterministic event-loop yield and ``sleep(60)`` is deliberate
    hang-simulation in timeout/cancellation tests. Attribute paths, subscripts
    and binary expressions are left alone too (``sleep(wait / 50)`` bounds a
    computed yield, ``sleep(cfg.delay)`` may be an override). Only sleeps in
    the body of a ``test_*`` function count: a helper that builds a slow node
    or a hanging double takes a ``delay`` parameter by design and is the
    legitimate way to simulate latency, so flagging it would force the tests
    back onto literals for no coverage gain.
    """
    found: list[tuple[int, str]] = []

    def _is_sleep_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "sleep"):
            return False
        if not isinstance(func.value, ast.Name):
            return False
        return func.value.id in ("time", "asyncio")

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue
        for node in ast.walk(fn):
            if not _is_sleep_call(node):
                continue
            if len(node.args) != 1:
                continue
            arg = node.args[0]
            if not isinstance(arg, ast.Name):
                continue
            found.append(
                (
                    node.lineno,
                    f"{ast.unparse(node.func.value)}.sleep({arg.id}) — duration is a computed name; "
                    "inject the time source the code under test reads and advance it deterministically",
                )
            )
    return found


def test_no_computed_wall_clock_sleep():
    """``asyncio.sleep(refill)`` / ``time.sleep(delay)`` — a sleep whose
    duration is a bare name — turns the test into a real wall-clock wait whose
    outcome depends on machine load and timing luck. The suite keeps such
    tests green only by being generous with the duration, and flakes when it is
    not. The deterministic fix is to inject the time source the code under
    test reads (a ``clock`` callable) and advance it in the test instead of
    sleeping. Literal durations are the deliberate hang/yield forms and are
    left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _computed_wall_clock_sleep_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} wall-clock sleep(s) with a computed duration.\n"
        "A sleep whose duration is a name depends on real wall-clock passage and can flake "
        "under load. Inject the time source the code under test reads (e.g. a monotonic "
        "clock callable) and advance it deterministically instead.\n" + "\n".join(violations)
    )


def test_computed_wall_clock_sleep_lens_flags_computed_durations():
    """Synthetic positive/negative control for the computed-wall-clock-sleep
    lens: it must flag ``time.sleep(<name>)``/``asyncio.sleep(<name>)`` with a
    bare-name duration in a ``test_*`` body and ignore literal durations,
    attribute/subscript/expression durations, unrelated calls, and the same
    sleep shape inside a non-test helper (where a ``delay`` parameter is the
    legitimate way to simulate latency)."""
    positive_sources = [
        "def test_foo():\n    asyncio.sleep(refill)\n",
        "def test_foo():\n    time.sleep(delay)\n",
        "async def test_foo():\n    await asyncio.sleep(backoff)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _computed_wall_clock_sleep_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    asyncio.sleep(0)\n",
        "def test_foo():\n    asyncio.sleep(0.05)\n",
        "def test_foo():\n    time.sleep(60)\n",
        "def test_foo():\n    asyncio.sleep(wait / 50)\n",
        "def test_foo():\n    asyncio.sleep(cfg.delay)\n",
        "def test_foo():\n    asyncio.sleep(delays[attempt])\n",
        "def test_foo():\n    random_sleep()\n",
        "def test_foo():\n    await asyncio.wait_for(coro(), 1)\n",
        "def _make_slow_node(delay):\n    async def _fn():\n        await asyncio.sleep(delay)\n    return _fn\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _computed_wall_clock_sleep_violations(tree), f"lens should NOT flag:\n{source}"


def _is_any_operand(node: ast.AST) -> bool:
    """Return True for the two spellings of ``unittest.mock.ANY`` — the bare
    imported name (``ANY``, the ``from unittest.mock import ANY`` form) and the
    attribute-qualified path (``mock.ANY`` / ``unittest.mock.ANY``)."""
    if isinstance(node, ast.Name) and node.id == "ANY":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "ANY"


def _any_equality_tautologies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose test
    expression compares a value against ``unittest.mock.ANY``.

    ``ANY.__eq__`` returns ``True`` for any value, so ``x == ANY`` is ALWAYS
    True and ``x != ANY`` ALWAYS False — no matter what ``x`` evaluates to, an
    ``assert x == ANY`` is a silent false green and ``assert x != ANY`` can
    never pass, both decided at source time. The membership twin is covered
    too: a list/tuple literal that *contains* ``ANY`` (``[a, ANY]``) makes
    ``in`` always PASS and ``not in`` always FAIL, because the element match
    short-circuits on the ``x == ANY`` that ANY is guaranteed to satisfy.
    Dict literals are deliberately not considered for membership — dict ``in``
    tests *keys*, which ANY never appears as, so the outcome still depends on
    the code under test.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare) or len(sub.ops) != 1:
                continue
            op = sub.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                if not any(_is_any_operand(side) for side in (sub.left, *sub.comparators)):
                    continue
                verdict = "ALWAYS PASSES" if isinstance(op, ast.Eq) else "ALWAYS FAILS"
                found.append(
                    (
                        sub.lineno,
                        f"{ast.unparse(sub)} — compares a value against unittest.mock.ANY, whose "
                        f"__eq__ always returns True, so this {verdict} regardless of the operand",
                    )
                )
                continue
            if not isinstance(op, (ast.In, ast.NotIn)):
                continue
            for comparator in sub.comparators:
                if not isinstance(comparator, (ast.List, ast.Tuple)):
                    continue
                if not any(_is_any_operand(element) for element in comparator.elts):
                    continue
                verdict = "ALWAYS PASSES" if isinstance(op, ast.In) else "ALWAYS FAILS"
                found.append(
                    (
                        sub.lineno,
                        f"{ast.unparse(sub)} — tests membership in a container that holds "
                        f"unittest.mock.ANY, which matches any value, so this {verdict}",
                    )
                )
                break
    return found


def test_no_any_equality_comparisons():
    """An ``assert`` that compares a value against ``unittest.mock.ANY`` is a
    fixed-outcome assertion: ``ANY.__eq__`` returns ``True`` unconditionally
    (that is what makes it match any expected argument inside
    ``assert_called_with``/``assert_awaited_with``), so ``assert x == ANY`` is
    a silent false green and ``assert x != ANY`` can never pass, no matter
    what ``x`` evaluates to. ``ANY`` is only meaningful where a mock framework
    presides over the comparison — pass it as the *expected* argument to a
    mock verification (``mock.assert_called_with(ANY)``) — never compare it to
    a value with ``==``/``!=`` yourself. The membership twin is covered too: a
    list/tuple literal that contains ``ANY`` (``[a, ANY]``) makes ``in``
    always PASS and ``not in`` always FAIL, because the element match
    short-circuits on ``x == ANY``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _any_equality_tautologies(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) comparing a value against unittest.mock.ANY.\n"
        "ANY.__eq__ always returns True, so '== ANY' is always True and '!= ANY' is always "
        "False — the assertion is decided at source time, never by the code under test. Pass "
        "ANY as the expected argument to a mock verification "
        "(mock.assert_called_with(ANY)); don't ==/!= against it.\n" + "\n".join(violations)
    )


def test_any_equality_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the ANY-equality lens: it must
    flag ``==``/``!=`` against ``ANY`` in either operand position (bare name or
    attribute-qualified) and ``in``/``not in`` against a list/tuple literal
    that holds ANY (in either operand order), and ignore ``ANY`` passed as a
    call argument (the legitimate mock-verification spelling), comparisons
    over bound names and other values, membership against non-ANY containers,
    dict membership (keys, not values), the ``=='ANY'`` string-literal spelling,
    and the neighbouring lens shapes."""
    positive_sources = [
        "def test_foo():\n    assert result == ANY\n",
        "def test_foo():\n    assert ANY == result\n",
        "def test_foo():\n    assert result != ANY\n",
        "def test_foo():\n    assert ANY != result\n",
        "def test_foo():\n    assert result == mock.ANY\n",
        "def test_foo():\n    assert result == unittest.mock.ANY\n",
        "def test_foo():\n    assert result in [ANY, 'a']\n",
        "def test_foo():\n    assert result not in ('a', ANY)\n",
        "def test_foo():\n    assert result == ANY and ok\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _any_equality_tautologies(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    m.assert_called_with(ANY)\n",
        "def test_foo():\n    m.assert_awaited_once_with(1, ANY)\n",
        "def test_foo():\n    m.assert_has_calls([call(ANY)])\n",
        "def test_foo():\n    assert result in [1, 2]\n",
        "def test_foo():\n    assert result not in [1, 2]\n",
        "def test_foo():\n    assert [ANY] in result\n",
        "def test_foo():\n    assert [ANY] not in result\n",
        "def test_foo():\n    assert result in {'k': ANY}\n",
        "def test_foo():\n    assert result in some_list\n",
        "def test_foo():\n    assert result == expected\n",
        "def test_foo():\n    assert result is None\n",
        "def test_foo():\n    assert 'ANY' in result\n",
        "def test_foo():\n    assert result > ANY\n",
        "def test_foo():\n    assert 'a' == 'b'\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _any_equality_tautologies(tree), f"lens should NOT flag:\n{source}"


_MUTATING_ENVIRON_METHODS = frozenset({"pop", "update", "setdefault", "clear", "__setitem__", "__delitem__"})
# Method names on ``os.environ`` that mutate the mapping in place. The
# ``__setitem__``/``__delitem__`` twins cover the pydantic-spelled
# ``os.environ["K"] = ...`` / ``del os.environ["K"]`` subscripts written as
# method calls.


def _is_environ_reference(node: ast.AST) -> bool:
    """Return True for either spelling of the process-environment mapping:
    the attribute path ``os.environ`` (``import os``) and the bare ``environ``
    name (``from os import environ``)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _environ_mutation_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every direct ``os.environ``
    mutation made without the ``monkeypatch`` fixture in scope.

    A test that mutates the process environment and never restores it leaks
    state into every test that runs afterwards, so the suite becomes
    order-dependent: a test can pass alone and silently corrupt a sibling (or
    be corrupted by one) in the full run. ``monkeypatch.setenv()`` /
    ``monkeypatch.delenv()`` restore the value at teardown automatically and
    are the pytest-blessed form — a function that requests ``monkeypatch`` is
    left alone even when it mutates ``os.environ`` directly. The recognised
    mutation spellings are the subscript store/delete on either environ
    spelling (``os.environ[key] = ...`` / ``del os.environ[key]``), the
    mutating ``environ`` methods (``pop``/``update``/``setdefault``/``clear``
    and their ``__*__`` twins), and the ``os.putenv()`` / ``os.unsetenv()``
    builtins. Reads (``os.getenv``, ``os.environ.get``, subscript loads) are
    left alone, and each function scope decides its own guard — a nested
    helper without ``monkeypatch`` stays flagged even inside a guarded test.
    Only mutations *inside a function body* are flagged: a module-level
    ``os.environ.setdefault(...)`` bootstrap (the ``conftest.py`` pattern that
    pins ``DATABASE_URL``/``FERNET_KEY`` once at import time) is idempotent
    environment configuration, not between-test leakage, and is deliberately
    left alone.
    """
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    found: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()

    def _record(node: ast.AST, fn: ast.AST, kind: str) -> None:
        key = (node.lineno, ast.unparse(node))
        if key in seen:
            return
        seen.add(key)
        found.append(
            (
                node.lineno,
                f"{ast.unparse(node)} in {fn.name} mutates the process environment without monkeypatch ({kind})",
            )
        )

    for fn in functions:
        guarded = any(arg.arg == "monkeypatch" for arg in fn.args.args)
        pending = list(fn.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not guarded:
                if isinstance(node, ast.Subscript) and _is_environ_reference(node.value):
                    if isinstance(node.ctx, (ast.Store, ast.Del)):
                        _record(node, fn, "subscript set/del")
                elif isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and _is_environ_reference(func.value)
                        and func.attr in _MUTATING_ENVIRON_METHODS
                    ):
                        _record(node, fn, f"environ.{func.attr}()")
                    elif (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "os"
                        and func.attr in ("putenv", "unsetenv")
                    ):
                        _record(node, fn, f"os.{func.attr}()")
            pending.extend(ast.iter_child_nodes(node))
    return found


def test_no_environ_mutation_without_monkeypatch():
    """A test that mutates ``os.environ`` and never restores it leaks process
    state into every later test in the same run, so the suite becomes
    order-dependent: a test can pass on its own (or in the first hop of a
    --randomly shuffle) and silently corrupt the sibling that runs after it,
    and the failure surfaces at the wrong test far from the offending
    mutation. ``monkeypatch.setenv()``/``monkeypatch.delenv()`` restore the
    original value at teardown automatically and are the pytest-blessed form,
    so a function that requests ``monkeypatch`` is trusted even when it writes
    ``os.environ`` directly. This guards the subscript set/delete spellings,
    the mutating ``environ`` methods, and ``os.putenv``/``os.unsetenv``.
    Reads (``os.getenv``, ``os.environ.get``, subscript loads) and the
    module-level ``conftest.py`` bootstrap are deliberately left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _environ_mutation_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} process-environment mutation(s) made without monkeypatch.\n"
        "A direct os.environ mutation never restores the prior value, so it leaks state into\n"
        "every later test in the run — the suite becomes order-dependent and fails at the wrong\n"
        "test. Use monkeypatch.setenv()/monkeypatch.delenv(), which restore at teardown.\n" + "\n".join(violations)
    )


def test_environ_mutation_lens_flags_unguarded_mutations():
    """Synthetic positive/negative control for the environ-mutation lens: it
    must flag every direct mutation spelling (subscript store/delete on either
    ``os.environ`` spelling, the ``environ`` mutating methods, and
    ``os.putenv``/``os.unsetenv``) when the enclosing function does not request
    ``monkeypatch``, and ignore reads, module-level bootstraps, mutations
    inside a ``monkeypatch``-guarded function, nested helpers that *are*
    guarded, and unrelated mutations."""
    positive_sources = [
        "def test_foo():\n    os.environ['K'] = 'v'\n",
        "def test_foo():\n    del os.environ['K']\n",
        "def test_foo():\n    environ['K'] = 'v'\n",
        "def test_foo():\n    os.environ.pop('K')\n",
        "def test_foo():\n    os.environ.update({'K': 'v'})\n",
        "def test_foo():\n    os.environ.setdefault('K', 'v')\n",
        "def test_foo():\n    os.environ.clear()\n",
        "def test_foo():\n    os.putenv('K', 'v')\n",
        "def test_foo():\n    os.unsetenv('K')\n",
        "def test_foo():\n    def inner():\n        os.environ['K'] = 'v'\n",
        "def test_foo():\n    os.environ['K'] = os.environ.get('OLD', '')\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _environ_mutation_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    m = monkeypatch\n    m.setenv('K', 'v')\n",
        "def test_foo(monkeypatch):\n    os.environ['K'] = 'v'\n",
        "def test_foo(monkeypatch):\n    os.environ.pop('K', None)\n",
        "def test_foo(monkeypatch):\n    os.putenv('K', 'v')\n",
        "def test_foo():\n    v = os.environ.get('K')\n",
        "def test_foo():\n    v = os.getenv('K')\n",
        "def test_foo():\n    v = os.environ['K']\n",
        "def test_foo():\n    x = config['K'] = 'v'\n",
        "def test_foo():\n    d = {}\n    d['K'] = 'v'\n",
        "def test_foo():\n    os.makedirs('/tmp/x')\n",
        "os.environ.setdefault('DATABASE_URL', 'sqlite://')\n",
        "import os\nos.environ.setdefault('FERNET_KEY', 'x')\n",
        "def test_foo(monkeypatch):\n    def inner(monkeypatch):\n        os.environ['K'] = 'v'\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _environ_mutation_violations(tree), f"lens should NOT flag:\n{source}"
