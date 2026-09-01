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
- ``assert x == b""`` / ``assert x != b""`` against an empty bytes literal —
  the bytes twin of the empty-string lens; an empty ``b""`` is falsy, so these
  should read ``assert not x`` / ``assert x``. The membership forms
  ``assert x in b""`` / ``assert x not in b""`` are dead too — an empty bytes
  can never contain anything, so ``in`` always FAILS and ``not in`` always
  PASSES no matter what ``x`` evaluates to. The sibling ``bytes()``
  call-form lens and the ``b""`` constant truthiness lens cannot see the
  ``b""`` literal because it parses as a plain ``ast.Constant`` (not a call or
  a standalone constant assertion)
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
- ``@pytest.mark.parametrize`` with a *large* literal case list (``>= 8``
  cases) and no way to name the cases — no ``ids=`` keyword and not every
  element carries a per-case ``pytest.param(..., id=...)``. pytest renders
  each item's nodeid as ``test_x[arr0]``..``test_x[arrN-1]``, so a failure
  report (and a ``.quarantine.yml`` entry, which records exactly those
  nodeids) forces the reader to count from the top of the case list to learn
  which input failed. ``ids=`` naming every case — or per-case
  ``pytest.param(id=...)`` on the elements that matter — restores
  self-documenting nodeids; small matrices, non-literal case lists, and
  matrices where every element already carries ``pytest.param(id=...)`` are
  left alone
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
- ``assert x or True`` / ``assert x and False`` (their constant-literal
  cousins, in either operand position, in a chain, and the ``not``-wrapped
  twins) — a boolean assertion whose test expression couples a value with a
  *literal constant* that fixes the verdict. A truthy constant under ``or``
  (``True``, ``1``, ``"y"``, ``2.5``, ...) is an identity element that absorbs
  the other operand(s), so the assert ALWAYS PASSES whatever the code under
  test does; a falsy constant under ``and`` (``False``, ``0``, ``None``, ``""``,
  ...) is an absorbent element that makes the assert ALWAYS FAIL. ``not``
  wrapped around the compound inverts the verdict. Only a literal
  ``ast.Constant`` operand counts (complex values excluded, mirroring the
  literal-constant lens), and a constant in the *wrong* position (falsy under
  ``or``, truthy under ``and``) does not pin the outcome and is left alone —
  that is the legitimate default/refinement idiom
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
 - a freshly-constructed Mock nested *inside* a container literal in an
   ``assert`` — ``assert result == {'status': MagicMock()}``,
   ``assert result != [AsyncMock()]``, ``assert {'k': Mock()} in x``. A fresh
   Mock compares by identity (``__eq__`` defaults to ``is``), so ``==`` against
   a container it can never equal ALWAYS FAILS and ``!=`` ALWAYS PASSES, and
   ``assert [Mock()]`` / ``assert (Mock(),)`` (a non-empty container is always
   truthy) ALWAYS PASS — every one decided at source time, never by the code
   under test. This is the nested-or-direct-container twin of the Mock-
   constructor lens, which owns only the *direct* positions (the assert's test
   expression, a ``not``-wrap, or a single comparison operand): a fresh
   constructor buried in a list/dict/tuple/set literal is a different AST shape
   that the direct lens provably misses. The configure-then-assert fix is the
   same — configure the double (``return_value``/``side_effect``) and verify
   through ``assert_called*``/attribute checks instead of comparing to a
   constructor call
 - an ``assert x in <mock>`` / ``assert x not in <mock>`` membership probe whose
   container side is a ``unittest.mock`` double — a mock factory call
   (``MagicMock()``/``AsyncMock()``/``Mock()`` or the ``mocker.``/
   ``mock.``-qualified twins) or a plain attribute chain rooted at a mock-flagged
   name (``mock``, ``mocker``, ``_mock_create``, ``mock_run``, ...). A MagicMock
   supports ``__contains__`` by generating a fresh child double and returning it
   as a truthy sentinel — never by consulting the recorded calls — so ``assert x
   in <mock>`` ALWAYS PASSES and ``assert x not in <mock>`` ALWAYS FAILS no
   matter what the code under test recorded: the behavioural check is dead. (A
   plain ``Mock`` without ``__contains__`` fails with a confusing ``TypeError``
   instead.) This is the ``__contains__`` sibling of the ``__eq__``-identity
   Mock lenses: membership on a double is almost always a broken attempt to ask
   "was this argument recorded?", which should be
   ``mock.assert_any_call(...)``/``assert_not_called()`` or membership against
   the *real* recorded-call containers ``mock.call_args_list``/``mock.method_calls``/
   ``mock.mock_calls``/``mock.await_args_list`` that the double documents.
   Attribute-path accessors that legitimately serve real containers
   (``call_args``/``call_args_list``/``method_calls``/``mock_calls``/
   ``await_args``/``await_args_list``/``return_value``/``side_effect``/
   ``kwargs``/``args``) are left alone, as are subscripts
   (``mock.return_value['plugins']``), method calls on the double, and
   non-mock containers
 - ``assert`` on a *container literal* whose truthiness is fixed at source time
  — ``assert [x]``, ``assert {}``, ``assert not [y, z]``, and their
  ``list``/``dict``/``set``/``tuple`` literal twins. A literal container is
  truthy exactly when non-empty and falsy when empty, so ``assert
  <non-empty literal>`` ALWAYS PASSES and ``assert not <non-empty literal>``
  ALWAYS FAILS no matter what the elements evaluate to — ``assert (a, b)``
  (or ``assert [a, b]``) is the classic forgot-``and`` bug where a
  tuple/list-wrapped condition silently becomes an always-true assertion,
  while ``assert <empty literal>`` ALWAYS FAILS and
  ``assert not <empty literal>`` ALWAYS PASSES. This is the *truthiness* twin
  of the container *equality* lenses: those catch ``== []``/``== {}`` against
  an empty literal, this one catches the container standing alone (or under
  ``not``) as the assert operand. Comprehension forms
  (``[x for x in y]``/``{k: v for ...}``) and unpacked dicts (``{**cfg}``)
  are left alone — their emptiness depends on the iterable the code under
  test provides
- direct ``os.environ`` mutation without the ``monkeypatch`` fixture in scope
  — ``os.environ[key] = ...`` / ``del os.environ[key]`` /
  ``os.environ.pop()``/``update()``/``setdefault()``/``clear()`` /
  ``os.putenv()`` / ``os.unsetenv()`` (including the `from os import environ`
  spellings). A test that mutates the process environment and never restores
  it leaks state into every test that runs afterwards, so the suite becomes
  order-dependent: the test can pass alone and silently corrupt a sibling
  (or be corrupted by one) in the full run, and a mutation-testing run
  believes each test is isolated when it is not.
  ``monkeypatch.setenv()``/``delenv()`` restore the value at teardown
  automatically and are the pytest-blessed form; a function that requests
  ``monkeypatch`` is left alone even when it mutates ``os.environ`` directly
  (reads — ``os.getenv``/``os.environ.get``/subscript loads — and the
  module-level ``os.environ.setdefault(...)`` bootstrap that pins
  ``DATABASE_URL`` once at import time are deliberately left alone, since
  those are idempotent configuration rather than between-test leakage)
- an ``assert`` that is *unreachable* because an earlier top-level statement
  in the same function body unconditionally ``return``s or ``raise``s before
  it — the verification can never execute, so the test reports green no
  matter how broken the guarded behaviour is. The no-op lens is blind to it
  because the body *contains* an assert; pytest is blind to it because the
  assert parses fine. Reachability only through a preceding branch
  (``if cond: return``/``try: return ... except: pass``) is the legitimate
  early-exit idiom and is left alone
- a ``test_*`` function whose *entire* body is a single unconditional
  ``pytest.skip(...)`` call — the test permanently deselects itself from
  every run but still *reads* as a live test (pytest reports it as skipped,
  not failed, so the suite stays green). This is the same coverage loss as
  ``@pytest.mark.skip``, hidden behind an in-body spelling that the
  skip-without-reason lens (only bare ``skip()`` calls) and the
  constant-condition-skip lens (only ``skipif``/``xfail`` marker conditions)
  do not reach. Turn it into ``@pytest.mark.skip(reason=...)`` /
  ``@pytest.mark.xfail(reason=...)`` with the reason spelled out, or delete
  it

- ``test_*`` functions *nested inside another function* — pytest only
  collects module/class-level ``test_*`` functions, so a ``def test_*``
  defined inside a test (or helper) body is never collected and silently
  drops whatever regression coverage it carries without any warning. It is
  almost always an accidental indentation or a helper miscast as a test, and
  nothing in the normal run reports it — the suite just runs a few tests
  fewer than a reader believes. Hoist the nested test to module scope, or
  rename a helper that only happens to start with ``test_``. ``@pytest.mark``-
  decorated functions are covered too (same non-collection), while nested
  ``def``/``class``/``@pytest.fixture`` helpers are left alone — those are the
   legitimate local-helper spellings

- a reseed of the process-global random generator made without the
  ``monkeypatch`` fixture in scope — ``random.seed(...)`` (the ``import random``
  attribute form and its ``from random import seed`` twin). Seeding resets the
  module-global ``random.Random`` singleton every test shares, changing the
  sequence that every later test calling ``random.*`` observes, so the suite
  becomes order-dependent: a test that guards on a drawn random value can pass
  alone and silently change (or be changed by) a sibling in the full run.
  ``random.seed`` is also almost always pointless — the blessed deterministic
  form is to inject a dedicated ``random.Random(N)`` instance so nothing global
  is touched, and a function that requests ``monkeypatch`` is trusted (it can
  restore the prior generator at teardown). A module-level ``random.seed(...)``
  bootstrap is left alone, and ``numpy.random.seed`` is deliberately out of
   scope (it seeds a separate generator with its own namespace)

- a direct mutation of the process *working directory* made without the
  ``monkeypatch`` fixture in scope — ``os.chdir(...)`` (either spelling),
  ``os.fchdir(...)``, and ``Path.chdir()``. A test that changes the current
  working directory and never restores it leaks that directory into every test
  that runs afterwards, so the suite becomes order-dependent: a test that (say)
  resolves a config template relative to ``os.getcwd()``, or writes a file into
  ``Path.cwd()``, behaves one way alone in CI and another way after a sibling
  that ``os.chdir``'d (or that itself depends on the pristine repo-root CWD).
  ``monkeypatch.chdir(tmp_path)`` restores the original directory at teardown
  automatically and is the pytest-blessed form — a function that requests
  ``monkeypatch`` is trusted even when it ``os.chdir``'s directly. Reads of the
  working directory (``os.getcwd()``, ``Path.cwd()``) are left alone, as are
  directory *creation* calls (``os.makedirs``/``os.mkdir``/``Path.mkdir``)
   that never change the process CWD
- an assertion that constructs a *fresh non-deterministic value* directly in
   its test expression — ``uuid.uuid4()``/``uuid.uuid1()``/``uuid4()`` (and
   their ``uuid3``/``uuid5`` siblings), ``secrets.token_hex()``/``token_bytes()``/
   ``token_urlsafe()``, ``time.time()``/``time.monotonic()``/``time.perf_counter()``/
   ``time.process_time()`` (and ``_ns`` twins), or ``datetime.now()``/
   ``datetime.utcnow()``, in the assert's truthiness position, under a ``not``,
   or as an operand of an equality/inequality comparison. Every such call
   returns a *fresh* value on every evaluation, so the outcome is decided at
   source time, never by the code under test: ``assert uuid.uuid4()`` is a
   silent false green (a UUID string is always truthy), ``assert result ==
   uuid.uuid4()`` ALWAYS FAILS (the freshly minted UUID can never equal the one
   the code under test produced and stored), and the ``!=``/``assert not`` twins
   ALWAYS PASS. These are the non-deterministic twin of the Mock-constructor
   lenses and are almost always a broken attempt to compare the code's output
   against a value generated by the *test itself*; the fix is to capture the
   generated value in a variable first and pass it to the code under test (or
   into the mock), then compare against that same bound name. Ordering
   comparisons (``assert t < time.time()``), subsystem-qualified methods
   (``clock.now()``/``ticker.time()``), and calls bound to a variable earlier in
   the test (the deliberate "place under test then assert on its captured value"
   pattern) are left alone
- a fresh *random-value draw* in the assert's checked position —
  ``random.randint(...)``/``random.choice(...)``/``random.sample(...)``/``random()``
  (and siblings, in the ``random.<fn>`` spelling) standing as the assertion
  operand (bare, under ``not``, or as a side of a ``==``/``!=`` comparison), or
  the bare ``from random import <fn>`` twin for the drawing names that are
  never plausible local helpers. Every draw returns a *fresh* value on each
  evaluation, so the outcome is decided at source time: ``assert random.random()``
  is a silent false green, ``assert not random.choice(lst)`` can never pass,
  and ``assert result == random.randint(0, 9)`` compares code output against a
  value the test itself draws at assert time — the flaky expected-value case.
  This is the randomness twin of the fresh-value (UUID/token/time) lens: the
  fix is to capture the drawn value in a variable first, feed it into the code
  under test, then assert against that bound name. Ordering comparisons
  (``assert t < random.random()``), draws passed *into* a function being tested
  (property-style random-input checks like ``median(random.sample(items, 5))``),
  injected ``rng`` instances, ``random.seed`` reseeds (owned by the reseed
  lens), and the ``random.<shuffle/choice/sample>`` bare-name spellings
  (plausible local-helper names) are deliberately left alone
- an *unconditional* ``pytest.skip(reason)``/``pytest.xfail(reason)``
   (or the bare imported ``skip(...)``/``xfail(...)``) placed as a direct
   statement of the test body. The call always executes, so whatever follows
   never runs and the test never verifies anything — it is indistinguishable in
   source from a runtime gate yet not gated (no surrounding ``if``/loop, no
   marker), so the coverage loss is identical to deleting the test but reported
   green. The marker and reason-less twins are owned by the
   ``skip-without-reason`` and ``constant-condition-skip`` lenses; this lens
   owns the statement form that carries a reason and slips past both. A skip
   nested under an explicit ``if``/loop (a real runtime gate) is left alone
 - an *unconditional* ``@pytest.mark.skip`` **marker** (or the bare imported
   ``@skip`` decorator), and its module-level ``pytestmark = pytest.mark.skip``
   twin that deselects every test in the module. A ``skip`` marker has no
   condition argument to gate on, so the decorated test is permanently removed
   from every run while still reporting green — the decorator twin of the
   unconditional body-skip statement lens above, which explicitly disclaims the
   marker form ("The marker and reason-less twins are owned by the
   ``skip-without-reason`` and ``constant-condition-skip`` lenses") but neither
   of those reaches it either: ``skip-without-reason`` only flags markers that
   *lack* a ``reason=``, and ``constant-condition-skip`` only handles
   ``@skipif``/``@xfail`` whose *condition* is foldable. A ``skip`` marker
   carrying a ``reason=`` therefore slips past every sibling unpacked. These are
   almost always a leftover from disabling a test while debugging — the repo's
   sanctioned alternate is ``@pytest.mark.skipif(<real condition>, ...)`` or the
   flaky-test quarantine registry. ``@skipif``/``@xfail`` are deliberately left
   alone: ``skipif`` is inherently conditional and ``xfail`` is the visible,
   reviewable "known-failing" pin (the test still runs to report XPASS)
 - an ``assert`` whose *entire* test expression is a container literal or a
   zero-argument empty-container builtin call — ``assert []``, ``assert [1, 2]``,
   ``assert {'k': 'v'}``, ``assert ()``, ``assert list()``, ``assert set()``.
   The truthiness of a container literal is decided by its arity alone (an empty
   container is falsy, a non-empty one is truthy) and a zero-argument builtin
   call always yields an empty container, so ``assert <container>`` ALWAYS FAILS
   for empty containers, ALWAYS PASSES for non-empty ones, and ``assert not
   <container>`` is the mirror — the outcome is fixed at source time, never by
   the code under test. This is the direct-test-position twin of the empty-
   container *equality* lens: ``assert x == []`` is already flagged, but a bold
   container standing alone as the assertion (``assert []`` shadowing the value
   that should have been checked, ``assert [1]`` after a debug edit) has a
   different AST shape that the literal-constant lens provably misses (a
   list/dict/set/tuple literal is ``ast.List``/``ast.Dict``/..., not
   ``ast.Constant``). Comprehensions and ``*args``/``**kwargs``-unpacked
   literals are left alone — those can legitimately be empty *or* non-empty.
   Container literals appearing as an *operand* of a comparison or ``in`` (the
   ``x == []`` / ``x in [...]`` shapes) are owned by their own lenses
 - ``time.sleep(...)`` inside an ``async def`` — an ``async`` test or fixture
   that calls the *blocking* time.sleep (the ``import time`` attribute spelling)
   freezes the entire event loop for the duration, so no other coroutine on that
   loop — teardowns, concurrent tasks, the run loop itself — can make progress,
   and a N-second literal sleep is N real seconds of CI on a single core, not a
   cooperative yield. The duration does not matter to the lens: even
   ``time.sleep(0)`` is the wrong idiom (it should be ``await asyncio.sleep(0)``,
   which yields control instead of hogging it). This is the async-twin of the
   computed-wall-clock-sleep lens: that one flags sleeps with a *computed*
   duration regardless of blocking or async spelling, this one flags the
   *blocking* spelling regardless of duration. Calls in a plain ``def`` (where a
   blocking sleep is the only way to wait) are left alone, and the ``from time
   import sleep`` bare-name spelling is deliberately not matched because a local
   ``sleep`` helper (e.g. an asyncio-driven retry) cannot be distinguished
   statically
- an ``assert`` placed in a ``finally:`` clause — an assertion that only runs
  during unwinding either masks the failure that triggered the unwind (an
  ``AssertionError`` raised from ``finally`` *replaces* the exception
  propagating out of ``try``/``except``, discarding the original traceback a
  reader needs to find the real regression) or verifies nothing that a check
  on the normal path could not express more clearly. This is the unwind-twin
  of the assert-inside-``except`` lens: there the assert masks the exception
  that dispatched the handler, here it masks whatever exception the
  ``finally`` is unwinding past. Move the check to the normal path (after the
  ``try``/``except``/``else``/``finally`` block) or into the ``except``
  handler that owns the failure being asserted, so an assertion failure names
  the real error instead of replacing it
- a *name-spelled* ``except BaseException:`` handler — the bare ``except:``
  spelling is already owned by the bare-except lens, but naming
  ``BaseException`` explicitly is the same swallow wearing a mask:
  ``BaseException`` is the base of ``KeyboardInterrupt``/``SystemExit``/
  ``GeneratorExit`` as well as ``Exception``, so a handler that names it
  catches the control-flow signals a test can never want, silently converting
  an interrupt during a hang into a false green. The tuple twin
  (``except (ValueError, BaseException):``) is covered too; ``except
  Exception:`` and tuples of concrete exceptions are the sanctioned narrow
  form and are left alone
- an unbounded ``while`` loop with a statically-foldable constant-true
  condition (``while True:``, ``while 1:``, ...) in test-support code — a
  loop whose condition is a language-level constant can never become false on
  its own and terminates only via a ``break`` targeting it or a
  ``return``/``raise`` unwinding the enclosing function, so one with none of
  those reachable is an infinite loop: it hangs CI indefinitely the way an
  unbounded subprocess or thread join does, and the failure is opaque (the
  runner just stops). Add an explicit ``break`` on a completion condition, or
  drive the loop with a real (non-constant) guard. Loops whose condition is a
  name, call, or comparison are left alone — those can change through side
  effects
- ``@pytest.mark.skip``/``@pytest.mark.skipif``/``@pytest.mark.xfail`` (and the
  bare imported ``@skip``/``@skipif``/``@xfail`` twins) applied to a
  ``@pytest.fixture`` function — pytest only honours selection markers on
  *collected test items*, and a fixture is not one, so the marker is silently
  ignored: a ``skipif`` that was meant to gate the tests built from the
  fixture never triggers, and the coverage loss is invisible because nothing
  reports the marker as dead. This is the fixture twin of the
  ``unconditional-skip-marker`` lens, which deliberately leaves fixtures
  alone. Hoist the gate into the fixture body (``pytest.skip(...)`` inside an
  ``if``), where it takes effect when the fixture is requested
- ``asyncio.wait_for(...)`` / ``asyncio.wait(...)`` without a timeout bound —
  ``asyncio.wait_for(coro)`` with no ``timeout`` argument, or an explicit
  ``timeout=None`` (the API default, meaning "wait forever"), suspends until
  the awaited coroutine finishes with no bound, so a coroutine that never
  completes hangs the test — and every test after it on the same event loop —
  indefinitely, and the failure is opaque (the runner just stops). The same
  applies to ``asyncio.wait(tasks)``, whose ``timeout`` also defaults to
  ``None``. This is the asyncio sibling of the unbounded-subprocess and
  unbounded-thread-``join`` lenses, which guard the child-process and
  in-process versions of the identical hazard. Always pass an explicit
  numeric ``timeout=<secs>`` (``wait_for(coro, 5)`` or the keyword form);
``asyncio.wait_for(coro, 0)`` is bounded-by-construction and allowed.
   Only the ``asyncio.*`` attribute spelling is matched — a local helper named
   ``wait_for``/``wait`` (e.g. a retry wrapper) cannot be distinguished
   statically and is deliberately left alone
- an ``assert`` statement that is a direct statement of a ``with
  pytest.raises(...)`` body whose expected exception is *not* ``AssertionError``
  — the assertion sits inside the region that ``pytest.raises`` owns. When the
  assert appears *after* the call expected to raise, it is unreachable: the
  expected exception fires first and the assert never executes, so the check is
  dead no matter how broken it is; when it appears *before* (or instead of)
  the raising call, a failure raises ``AssertionError`` — almost never the
  exception ``pytest.raises`` expects — producing a confusing mismatch instead
  of pointing at the broken condition. An assert whose only role is to force an
  attribute evaluation (``assert module.lazy_name`` driving a module
  ``__getattr__``) is the same hazard with a side-effect vehicle. Blocks that
  expect ``AssertionError`` are left alone: there the assert *is* the intended
  trigger (the blessed validator-trip idiom). ``pytest.warns(...)`` bodies are
  left alone too — warnings don't raise, so the body runs to completion and an
  assert inside is reachable and meaningful. Move the assertion after the
  ``with`` block (asserting on the recorded ``exc_info.value`` is the canonical
  form), or make it the intentional trigger with ``pytest.raises(AssertionError)``
- a ``dict`` literal that *repeats the same key* more than once —
  ``{'a': 1, 'a': 2}``, ``{key: 1, key: 2}``. Python evaluates the duplicate
  keys in source order and silently keeps only the LAST value, so the first
  occurrence is dead data: an expected-value dict, a mock ``side_effect``
  table, a request payload, or a config overlay holds an entry that never
  applies while a reader (and a mutation-testing run) believes both are used.
  Two identical keys are almost always copy-paste from editing one case into
  an existing dict — and when the duplicate sits in a *rewrite* (the value the
  code under test is compared against), the dead first entry desynchronizes
  the test's expectation from its source. This is the dict-data twin of the
  duplicate-membership-element lens, which owns repeated elements in
  list/tuple/set membership containers; a dict literal has a different shape
  (``ast.Dict`` pairs, not elements) that lens cannot see. Byte-identical
  pure keys (constants, names, attribute paths, subscripts — the
  ``_stable_dump`` family) are flagged; call/comprehension keys (may carry
  side effects or non-determinism) and ``**other`` unpacking (dynamic by
  nature) are left alone
- an *unseeded* random-number generator constructed in test code —
  ``random.Random()``/``random.Random(seed=None)`` (and the ``from random
  import Random`` bare-name twin), ``numpy.random.RandomState()``, and
  ``numpy.random.default_rng()`` (with ``np`` the alias spelling). An RNG
  constructed with no seed draws its state from OS entropy, so every run of
  the test produces DIFFERENT data: a failing run cannot be re-run with the
  same inputs (the failure is unreproducible by construction), and a
  mutation-testing run observes inputs that no real run ever drew. This is
  the construction twin of the fresh-random-draw lens (which owns a draw
  standing in an assertion) and the random-reseed lens (which owns the shared
  global generator) — nowhere else does this file bless ``Random(N)`` as the
  deterministic form, so an unseeded construction defeats that contract. Pass
  an explicit seed (``random.Random(0)``, ``default_rng(seed=0)``) so the run
  is reproducible. Calls carrying any positional argument or a non-``None``
  ``seed=`` are seeded by definition and left alone; the bare ``Random(...)``
   spelling is only judged when the module imports the name from ``random``
- a wall-clock *elapsed* measure compared inside an ``assert`` —
  ``assert time.monotonic() - started < 1.0``, ``assert deadline -
   time.time() > 0.1``, ``assert (time.perf_counter() - t0) == 0.5``. An
   ``assert`` whose compare operand is a subtraction that reads ``time.<clock>()``
   on either side embeds real wall-clock passage into the verdict: the test
   passes or fails on how long the suite *actually took* between the two clock
   reads, so a loaded CI runner, a preempted process, or a slow sandbox flakes
   it, and an artificially fast run can pass without exercising the slow path
   it was written to bound. This is the assertion twin of the computed-wall-clock
   sleep lens, which guards the ``sleep(<computed-duration>)`` half of the same
   hazard. The fix is to inject the time source the code under test reads (a
   monotonic ``now`` callable) and advance it deterministically, or to drag the
   elapsed measure out of the assertion and compare pinned timestamps. Bare
   ordering reads (``assert started < time.monotonic()`` — a monotonicity check
   that cannot flake), subscriptions and ``_ns`` twin reads not in a subtraction,
   and a ``Sub`` whose children are *not* ``time.<clock>()`` reads (``elapsed() -
   started``, ``abs(a) - b``) are deliberately left alone, as are wall-clock reads
   inside subtraction buried more than one level under the compare operand (the
   lens owns the top-level operand shape only)
- a *fresh non-deterministic value* passed as the *expected* argument to a mock
  call-assertion — ``<mock>.assert_called_with(id=uuid.uuid4())``,
  ``assert_awaited_once_with(event_time=datetime.now(UTC))``,
  ``assert_any_call(time.monotonic())`` — the expected-argument twin of the
  fresh-value lens. Every UUID/secrets-token/wall-clock/`datetime.now()` call
  mints a *new* value on each evaluation, so the recorded call (whatever the
  code under test actually passed) can never equal the freshly-regenerated
  expectation: for ``assert_called_with``/``assert_called_once_with`` and the
  awaited twins the assertion ALWAYS FAILS, and for ``assert_any_call`` no
  recorded call ever matches. These are almost always a broken attempt to
  assert against a value the test re-generated at assert time instead of
  capturing it in a variable, feeding it into the code under test, and
  comparing against the same bound name. The recognised spellings are exactly
  the fresh-value lens's set (bare UUID/token names, the ``uuid.``/``secrets.``
  attribute paths, the ``time.*`` wall-clock reads, and ``datetime.now()``/
  ``datetime.utcnow()``), and only *direct* positional/keyword argument
  positions of the verify methods are checked — a fresh value nested inside a
  container or ``call(...)`` wrapper is a less direct shape and is left alone,
  mirroring the fresh-Mock-in-call-assertion lens
- membership against an *empty-container builtin call* — ``assert x in set()``,
  ``assert x not in list()``, ``assert x in dict()``, ``assert x in tuple()``.
  A zero-argument ``set()``/``list()``/``dict()``/``tuple()``/``frozenset()``/
  ``bytearray()`` always yields an *empty* container, and an empty container can
  never contain anything, so ``in`` ALWAYS FAILS and ``not in`` ALWAYS PASSES no
  matter what ``x`` evaluates to — the same dead assertion as ``assert x in []``,
  but in the call spelling the empty-container *literal* membership lens and the
  empty-builtin *equality* lens can provably miss. This is the membership twin
  of the ``== set()``/``== list()`` equality lens. ``bytes()`` is deliberately
  excluded: bytes ``in`` uses *substring* semantics, so ``assert b'' in bytes()``
  is TRUE (an empty bytes contains itself) and the `empty_bytes_tautologies`
  lens already owns the ``in b""`` shape. A bare-name other operand is left
  alone (mirroring the sibling lenses), and literals on the other side are owned
  by the literal-comparison lens
- a fresh non-deterministic value buried inside a *container literal* that is
  a comparison operand of an ``assert`` — ``assert result == {'id': uuid.uuid4()}``,
  ``assert result != [token_hex()]``, ``assert tag in (time.monotonic(),)``,
  ``assert result not in {secrets.token_urlsafe()}``. The direct fresh-value
  lens owns only the bare/``not``/single-comparison-operand positions; a fresh
  UUID/token/wall-clock/``datetime.now()`` call nested inside a list/dict/tuple/
  set literal is a different ``ast`` shape it provably misses, exactly the gap
  the container-nested Mock lens closes for ``Mock()``. Every evaluation re-mints
  the call, so the freshly-constructed container can never equal the one the code
  under test produced and stored: ``==`` ALWAYS FAILS and ``!=`` ALWAYS PASSES
  no matter what the other operand evaluates to. For membership, the verdict is
  fixed only when *every* candidate element (or dict key) of a non-empty literal
  container is a fresh call — then no value the code under test produced can ever
  match, so ``in`` ALWAYS FAILS and ``not in`` ALWAYS PASSES. Capture the
  generated value in a variable first, feed it into the code under test, and
  compare against that bound name. Mixed containers (``assert x in [uuid.uuid4(),
  'fallback']``), membership against a container whose fresh value sits in a
  non-candidate slot (a dict *value*, which ``in`` never consults), ``**``-spread
  dicts and ``*``-starred lists (dynamic memberships), and fresh values nested
  inside ``call(...)`` wrappers are not provable and are left alone
- a fresh *random-value draw* passed as the *expected* argument to a mock
  call-assertion — ``<mock>.assert_called_with(random.randint(0, 9))``,
  ``assert_awaited_once_with(name=random.choice(names))``,
  ``assert_any_call(random.random())`` — the expected-argument twin of the
  random-draw lens, in the same relationship the fresh-value-in-call-assertion
  lens holds to the fresh-value lens. Every ``random.<fn>`` draw returns a *new*
  value on each evaluation, so the recorded call (whatever the code under test
  actually passed) can never equal the re-drawn expectation: for
  ``assert_called_with``/``assert_called_once_with`` and the awaited twins the
  assertion ALWAYS FAILS, and for ``assert_any_call`` no recorded call ever
  matches. These are the flaky expected-value variant of comparing code output
  against a value the test itself draws at verify time instead of capturing it
  into a variable and passing the bound name. The recognised spellings and
  *direct* positional/keyword argument positions mirror the fresh-value-in-call-
  assertion lens; draws nested inside a container or ``call(...)`` wrapper are a
  less direct shape and are left alone
- an assertion whose operand is a freshly-built *iterator object* — a
  generator expression (``assert (x for x in results)``) or an
  iterator-producing builtin call (``map()``/``filter()``/``zip()``/``iter()``/
  ``reversed()``/``enumerate()``) standing as the assert's truthiness position
  (bare or ``not``-wrapped) or compared with ``==``/``!=`` against a
  freshly-allocated container literal. Iterator objects have no ``__bool__``/
  ``__len__``, so a generator that yields nothing — or a ``filter()`` that
  matches nothing — is still a *truthy* object: ``assert <iterator>`` ALWAYS
  PASSES (a silent false green when the code under test produced an empty
  result) and ``assert not <iterator>`` ALWAYS FAILS. The equality form is
  fixed too: a fresh iterator can never equal a freshly-allocated
  list/dict/set/tuple literal (``assert map(...) == [...]`` ALWAYS FAILS,
  ``!=`` ALWAYS PASSES), and two fresh iterators compare by identity, so
  ``assert map(...) == filter(...)`` ALWAYS FAILS. These are almost always a
  forgotten materialization — ``assert map(...)`` instead of ``assert
  list(map(...))``, ``assert (x for x in y)`` instead of ``assert any(x for x
  in y)`` — the lazy-iterator twin of the container-literal-truthiness lens,
  which provably misses both shapes (a generator expression parses as
  ``ast.GeneratorExp``, not a container literal, and a bare builtin call is
  ``ast.Call``, not a literal). Consume or reduce the iterator before
  asserting. Attribute spellings (``df.map``/``conn.iter``), iterators nested
  as an argument to a materializing call (``assert list(map(...))``), and
  compares against a *name* (which cannot be proven unequal — a bound mock's
  ``__eq__`` can always return truthy) are left alone
- ``type(x) == X`` / ``type(x) != X`` equality on the builtin ``type`` —
  ``assert type(err) == ValueError``, ``assert ValueError == type(result)``,
  ``assert type(a) != type(b)``. ``type()`` returns the exact runtime class,
  and comparing it with ``==``/``!=`` (rather than ``is``/``is not``)
  silently rejects an instance of a *subclass* of ``X`` — the unidiomatic
  typecheck a subclass-aware ``isinstance(x, X)`` (or the identity-safe
  exact-type ``type(x) is X``) was meant to be — and a mocked or replaced
  class defeats the check outright. Only the builtin ``type`` name with a
  single argument is matched (the three-argument ``type(name, bases, ns)``
  form *creates* a class and is left alone), the ``is``/``is not`` exact-type
  spellings are blessed and left alone, and ``type(a) == type(b)`` is
  covered (prefer ``type(a) is type(b)``)
- ``isinstance``/``issubclass`` checks whose types argument fixes the verdict
  at source time — the bare ``object`` class (ALWAYS True: every object is an
  instance of ``object``, and every class a subclass of it), an empty types
  tuple ``isinstance(x, ())`` (ALWAYS False: nothing to match), or a types
  tuple containing ``object`` alongside other types (the tuple is a
  disjunction, so the ``object`` element alone forces the whole check ALWAYS
  True). A check that can never change its outcome is dead assertion code —
  assert against a specific type or drop the check entirely
- ``assert <verdict-A> if <cond> else <verdict-B>`` — a conditional expression
  (ternary ``IfExp``) standing as the *entire* assertion verdict, where both
  branches are themselves full boolean verdicts (plain comparisons, boolean
  combinations of them, or a ``not``-wrapped verdict — or a literal ``True``/
  ``False`` constant on one side). Two expectations are being pinned by one
  assertion whose outcome depends on ``<cond>``: when it fails pytest reports
  the whole ternary as an opaque boolean and cannot say which branch broke, and
  a reader cannot see which expectation each branch requires. A literal
  ``True``/``False`` branch is worse — it makes the assertion a fixed outcome
  whenever that branch is selected, the IfExp twin of the constant-absorbed
  boolean hazard. Conditional *operands* are deliberately left alone:
  ``assert x in (err if err else "")`` computes a single value and then asserts
  one fact about it, which is the legitimate form. Compute the expected value
  first (``expected = ... if ... else ...`` followed by ``assert x ==
  expected``) or split into one ``assert`` per branch
- ``assert x in mapping.keys()`` / ``assert x not in mapping.keys()`` —
  membership against the redundant ``.keys()`` dict view. ``k in d`` *already*
  tests key membership, so ``k in d.keys()`` computes the same verdict through
  an extra call that ruff SIM118 flags as redundant; the spelling also sits one
  typo away from the value-view confusion (``k in d.values()``) that silently
  flips the assertion's meaning from key to value. Membership against the
  ``.items()``/``.values()`` views is deliberately left alone — those views DO
  change the meaning and are the correct spellings when present

Every lens is written so it reports actionable file:line violations instead
of a bare "assert not violations", mirroring the sibling architecture tests.
"""

import ast
import functools
import operator
import re
from fractions import Fraction
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent

#: Test packages that are tooling rather than assertions and may legitimately
#: emit progress output or take long pauses (load/benchmark harnesses).
EXCLUDED_PACKAGES = {"load", "performance"}


def _callable_name(node: ast.AST) -> str | None:
    """Return the bare callable name behind ``node``.

    Accepts either a call (``pytest.raises(...)`` -> ``raises``) or a bare
    callable expression (``pytest.raises`` -> ``raises``, ``raises`` ->
    ``raises``), so the ``node`` and ``node.func`` spellings of this lookup
    collapse onto one implementation. Returns ``None`` for anything that is
    neither an attribute nor a plain name (e.g. a subscript, or a call whose
    callee is itself a call)."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


#: Domain-specific alias of :func:`_callable_name` that keeps decorator call
#: sites readable; the extraction rules are identical.
def _decorator_name(dec: ast.AST) -> str | None:
    """Return the bare name of a decorator (``pytest.fixture`` -> ``fixture``)."""
    return _callable_name(dec)


def _is_mark_decorator(dec: ast.AST) -> bool:
    """Return True when ``dec`` is a ``@pytest.mark.*`` marker.

    Unlike ``_decorator_name(d) == "mark"`` (which only matches a bare
    ``@pytest.mark`` / ``@mark``), this walks the whole attribute chain so a
    realistically-spelled marker such as ``@pytest.mark.asyncio`` is also
    recognised — its terminal attribute is ``asyncio``, but the ``mark``
    attribute sits further up the chain."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    node: ast.AST = dec
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return "mark" in parts


def _iter_test_modules():
    for path in sorted(TESTS.rglob("*.py")):
        if any(part in EXCLUDED_PACKAGES for part in path.parts):
            continue
        yield path


@functools.cache
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
                        name = _callable_name(f)
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
            name = _callable_name(func)
            if name in ("skip", "skipped") and not node.args and not node.keywords:
                violations.append(f"  {path.relative_to(TESTS)}:{node.lineno}  pytest.skip() without reason")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                f = dec.func
                dname = _callable_name(f)
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
            if used_names.get(node.name):
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


def _empty_bytes_tautologies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose outcome is
    fixed by an empty ``bytes`` literal (``b""``).

    Two shapes are owned:

    - equality/inequality against ``b""`` — an empty bytes is falsy, so
      ``assert x == b""`` should read ``assert not x`` and ``assert x != b""``
      should read ``assert x`` (the literal twin of the ``bytes()`` call-form
      lens, which cannot see ``b""`` because it parses as an ``ast.Constant``
      rather than a call)
    - membership against ``b""`` — an empty bytes can never contain anything,
      so ``x in b""`` always FAILS and ``x not in b""`` always PASSES; but the
      check is *asymmetric*: ``b"" in x`` is always True (the empty sequence is a
      subsequence of every ``bytes`` value), so ``b"" in x`` always PASSES and
      ``b"" not in x`` always FAILS. The verdict therefore depends on which side
      the empty literal sits on (element vs container), and must not be derived
      from the operator alone.

    Equality uses the same exclusions as the empty-string/tuple lenses: a bare
    name is left alone (it may bind ``None``), and a ``.get(...)`` lookup is
    left alone (``None`` vs ``b""`` is a meaningful distinction). Membership
    flags all operands except a literal constant — literal-vs-literal
    membership (``b"" in b""``) is owned by the literal-comparison lens."""
    found: list[tuple[int, str]] = []

    def _is_empty_bytes(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, bytes) and node.value == b""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        op = test.ops[0]
        sides = [(test.left, test.comparators[0]), (test.comparators[0], test.left)]
        if isinstance(op, (ast.Eq, ast.NotEq)):
            for operand, literal in sides:
                if not _is_empty_bytes(literal):
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
                op_name = "==" if isinstance(op, ast.Eq) else "!="
                prefer = "assert not ..." if isinstance(op, ast.Eq) else "assert ..."
                found.append((node.lineno, f"asserts value {op_name} b'' — prefer '{prefer}'"))
                break
        elif isinstance(op, (ast.In, ast.NotIn)):
            # `in`/`not in` are asymmetric: `A in B` asks whether A is a member of
            # B. The left operand is the *element* and the comparator is the
            # *container*. The empty literal flips the verdict depending on which
            # side it occupies, so we must not derive the verdict from `op` alone.
            element_is_empty = _is_empty_bytes(test.left)
            container_is_empty = _is_empty_bytes(test.comparators[0])
            if not (element_is_empty or container_is_empty):
                continue
            # The other operand must not itself be a literal constant — literal-vs-
            # literal membership is owned by the literal-comparison lens.
            other = test.comparators[0] if element_is_empty else test.left
            if isinstance(other, ast.Constant):
                continue
            op_name = "in" if isinstance(op, ast.In) else "not in"
            if element_is_empty:
                # b"" in x is always True; b"" not in x is always False.
                verdict = "always PASSES" if isinstance(op, ast.In) else "always FAILS"
            else:
                # x in b"" is always False; x not in b"" is always True.
                verdict = "always FAILS" if isinstance(op, ast.In) else "always PASSES"
            found.append(
                (
                    node.lineno,
                    f"asserts value {op_name} b'' — {verdict} (an empty bytes can never contain anything)",
                )
            )
    return found


def test_no_empty_bytes_tautologies():
    """``assert x == b""`` / ``assert x != b""`` compare a value against an empty
    bytes literal — the bytes twin of the empty-string lens. An empty ``b""`` is
    falsy, so ``assert x == b""`` should read ``assert not x`` and ``assert x !=
    b""`` should read ``assert x``. The membership forms ``assert x in b""`` /
    ``assert x not in b""`` are already dead as written: an empty bytes can never
    contain an element, so ``in`` always FAILS (an unconditionally red test) and
    ``not in`` always PASSES (a silent false green). The sibling lenses miss
    ``b""`` entirely — the empty-container lens matches ``list``/``dict``/
    ``set``/``tuple`` literal nodes, the empty-string lens matches ``str``
    constants, the ``bytes()`` call-form lens matches a zero-argument ``ast.Call``,
    the constant-literal lens only fires when ``b""`` is the *entire* assert test
    expression, and literal-vs-literal membership is owned by the
    literal-comparison lens."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_bytes_tautologies(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} empty-bytes assertion(s).\n"
        "An empty b'' is falsy and can never contain anything; write "
        "'assert not <expr>' / 'assert <expr>' instead of '== b\"\"' / '!= b\"\"'\n"
        "and drop the dead membership check.\n" + "\n".join(violations)
    )


def test_empty_bytes_tautology_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the empty-bytes lens: must flag
    ``== b""``/``!= b""`` on attribute/subscript/call/await operands (either
    operand order) and ``in b""``/``not in b""`` on any non-literal operand;
    ignore bare names, ``.get(...)``, non-empty bytes, empty str/tuple/etc
    literals owned by sibling lenses, and literal-vs-literal membership."""
    positive_sources = [
        "def test_foo():\n    assert result.blob == b''\n",
        "def test_foo():\n    assert result['blob'] != b''\n",
        "def test_foo():\n    assert fetch_blob() == b''\n",
        "def test_foo():\n    assert await fetch_blob() == b''\n",
        "def test_foo():\n    assert b'' != result['blob']\n",
        "def test_foo():\n    assert needle in b''\n",
        "def test_foo():\n    assert needle not in b''\n",
        "def test_foo():\n    assert b'' not in haystack\n",
        "def test_foo():\n    assert result.blob[:1] == b''\n",
    ]
    # Membership sources mapped to their *correct* verdict (element-side empty
    # literal flips the In/NotIn outcome relative to container-side empty).
    positive_verdicts = {
        "def test_foo():\n    assert needle in b''\n": "always FAILS",
        "def test_foo():\n    assert needle not in b''\n": "always PASSES",
        "def test_foo():\n    assert b'' not in haystack\n": "always FAILS",
    }
    for source in positive_sources:
        tree = ast.parse(source)
        findings = _empty_bytes_tautologies(tree)
        assert findings, f"lens should flag:\n{source}"
        if source in positive_verdicts:
            expected = positive_verdicts[source]
            assert any(expected in detail for _, detail in findings), (
                f"lens should report '{expected}' for:\n{source}\n got: {findings}"
            )

    negative_sources = [
        "def test_foo():\n    assert x == b''\n",
        "def test_foo():\n    assert load_config().get('blob') == b''\n",
        "def test_foo():\n    assert result.blob == b'\\x00\\x01'\n",
        "def test_foo():\n    assert result.blob == b''.join(parts)\n",
        "def test_foo():\n    assert b'' in b''\n",
        "def test_foo():\n    assert needle in b'abc'\n",
        "def test_foo():\n    assert result.blob == ''\n",
        "def test_foo():\n    assert result.blob == ()\n",
        "def test_foo():\n    assert result.blob == bytes()\n",
        "def test_foo():\n    assert b''\n",
        "def test_foo():\n    assert not b''\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_bytes_tautologies(tree), f"lens should NOT flag:\n{source}"


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


def _assert_inside_finally(tree: ast.AST) -> list[ast.Assert]:
    """Return the ``ast.Assert`` nodes reachable from any ``finally`` body.

    Mirrors the except-handler lens: a ``finally`` body runs on *every* exit
    path — success, ``return``/``break``/``continue``, and exceptions alike —
    so an assertion placed there also runs when the ``try`` body already
    failed. When both the body and the cleanup assertion fail, the
    ``AssertionError`` from the ``finally`` block silently replaces the
    original exception (a ``finally`` body's exception always takes precedence
    over the one being propagated), discarding the traceback that explains why
    the code under test broke."""
    found: list[ast.Assert] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        for stmt in node.finalbody:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assert):
                    found.append(sub)
    return found


def test_no_assert_inside_finally():
    """An ``assert`` nested in a ``finally`` block is the masking hazard twin of
    the except-handler lens (``test_no_assert_inside_except``): the block runs
    on every exit path, so its assertions execute even when the ``try`` body
    already failed, and if such an assertion fires while an exception is
    propagating, the ``AssertionError`` silently replaces the original
    exception — losing the traceback that explains why the code under test
    raised. Assert cleanup invariants in the ``try``/``with`` body *before* the
    ``finally`` handles teardown, or assert on the specific path the check
    verifies."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for assert_node in _assert_inside_finally(tree):
            violations.append(f"  {rel}:{assert_node.lineno}  assert inside finally block")
    assert not violations, (
        f"Found {len(violations)} assertion(s) inside finally block(s).\n"
        "A finally block runs on every exit path; an assert there can mask the\n"
        "original exception when both the body and the cleanup check fail.\n"
        "Assert cleanup invariants in the try/with body, before the finally teardown.\n" + "\n".join(violations)
    )


def test_assert_inside_finally_lens_flags_masking_hazard():
    """Synthetic positive/negative control for the assert-in-finally lens: it
    must flag an assert reachable from a ``finally`` body (however deeply
    nested) and ignore asserts in the ``try`` body, an ``except`` handler, a
    ``finally`` that only runs cleanup, or a plain ``pytest.raises`` context."""
    positive_sources = [
        "def test_foo():\n    try:\n        foo()\n    finally:\n        assert cleaned\n",
        (
            "def test_foo():\n"
            "    try:\n"
            "        foo()\n"
            "    finally:\n"
            "        with ctx:\n"
            "            bar()\n"
            "            assert bar.done\n"
        ),
        (
            "def test_foo():\n"
            "    try:\n"
            "        foo()\n"
            "    except ValueError:\n"
            "        pass\n"
            "    finally:\n"
            "        assert cleanup()\n"
        ),
        (
            "def test_foo():\n"
            "    try:\n"
            "        foo()\n"
            "    finally:\n"
            "        def helper():\n"
            "            assert finished()\n"
            "        helper()\n"
        ),
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _assert_inside_finally(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    try:\n        assert foo()\n    finally:\n        cleanup()\n",
        "def test_foo():\n    try:\n        foo()\n    finally:\n        cleanup()\n",
        "def test_foo():\n    try:\n        foo()\n    except ValueError:\n        assert err\n",
        "def test_foo():\n    try:\n        foo()\n    finally:\n        pass\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        foo()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _assert_inside_finally(tree), f"lens should NOT flag:\n{source}"


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
                name = _callable_name(f)
                if name in _RAISES_CONTEXT_NAMES:
                    return True
        if isinstance(sub, ast.Call):
            f = sub.func
            name = _callable_name(f)
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
            if not (node.name.startswith("test_") or any(_is_mark_decorator(d) for d in node.decorator_list)):
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


def _call_has_constant_args(call: ast.Call) -> bool:
    """True when a Call's positional/keyword args are pure constant literals.

    Names, attribute paths, subscripts, starred values and comprehensions
    reference or bind state the call could depend on, so ``f(x) == f(x)`` is a
    legitimate determinism check. A bare-name callee called ONLY with constant
    literals can never exercise distinct inputs — ``f({'a': 1}) ==
    f({'a': 1})`` is as vacuous as ``x == x``, just with more ceremony. Method
    calls (``obj.method(...)``) are never considered: the receiver holds state.

    A zero-argument call (``get_time() == get_time()``, ``uuid4() ==
    uuid4()``) is deliberately NOT considered constant: the lens cannot tell a
    deterministic identity from a non-deterministic value without
    interprocedural analysis, and such comparisons are legitimate determinism
    checks (they CAN fail), so an empty arg list returns False.
    """
    args = list(call.args) + [kw.value for kw in call.keywords]
    if not args:
        return False
    for arg in args:
        for n in ast.walk(arg):
            if isinstance(
                n,
                (
                    ast.Name,
                    ast.Attribute,
                    ast.Subscript,
                    ast.Starred,
                    ast.ListComp,
                    ast.SetComp,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.Lambda,
                    ast.FormattedValue,
                ),
            ):
                return False
    return True


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
        if not (isinstance(left, (ast.Name, ast.Attribute, ast.Subscript, ast.Call))):
            continue
        if isinstance(left, ast.Call):
            # Identical bare-name calls with constant-literal args only — a
            # determinism check of a constant, which never exercises distinct
            # inputs. Calls with variable args (``f(x) == f(x)``), zero-arg
            # calls (``get_time() == get_time()``, ``uuid4() == uuid4()``), and
            # method calls (``obj.method(a) == obj.method(a)``) stay unflagged:
            # the first two are legitimate determinism checks the lens cannot
            # prove redundant without interprocedural analysis, and the last
            # depends on receiver state.
            if not isinstance(left.func, ast.Name):
                continue
            if not isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                continue
            if not _call_has_constant_args(left):
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

    The lens flags syntactically identical operands whose type is a
    variable, attribute path, or subscript — expressions that re-evaluate to
    the same object. It also flags identical bare calls that are fed ONLY
    constant literals (``assert fn(1) == fn(1)``, ``assert digest({'a': 1}) ==
    digest({'a': 1})``): a determinism check of a constant never exercises
    distinct inputs, so it is dead code no matter how broken the code under
    test is. ``Call`` operands with variable arguments (``assert
    signal_fingerprint(a) == signal_fingerprint(a)``) are deliberately NOT
    flagged — that is a legitimate determinism/stability check of a (pure)
    function, so the lens cannot know a call is redundant without
    interprocedural analysis. Method calls (``obj.method(...)``) are likewise
    not flagged because the receiver holds state.
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
    paths, subscripts) plus identical bare calls fed only constant literals,
    and ignore comparisons that could involve distinct values, side-effecting
    calls, or stateful method receivers."""
    positive_sources = [
        "def test_foo():\n    assert x == x\n",
        "def test_foo():\n    assert result.value != result.value\n",
        "def test_foo():\n    assert row['key'] is row['key']\n",
        "def test_foo():\n    assert a.b.c <= a.b.c\n",
        "def test_foo():\n    assert items[0] > items[0]\n",
        "def test_foo():\n    assert x is not x\n",
        "def test_foo():\n    assert fn(1) == fn(1)\n",
        "def test_foo():\n    assert h({'name': 'café'}) == h({'name': 'café'})\n",
        "def test_foo():\n    assert build_item(1, 'x') != build_item(1, 'x')\n",
        "def test_foo():\n    assert f({'a': [1, 2], 'b': ()}) is f({'a': [1, 2], 'b': ()})\n",
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
        "def test_foo():\n    assert items.get('k') == items.get('k')\n",
        "def test_foo():\n    assert get_time() < get_time()\n",
        "def test_foo():\n    assert fn(1) == fn(2)\n",
        "def test_foo():\n    assert h({'name': 'café'}) == h(json.loads('{\"name\": \"\\\\u00e9\"}'))\n",
        # Zero-arg calls are legitimate determinism checks the lens cannot prove
        # redundant without interprocedural analysis (get_time()/uuid4()/randint()
        # CAN yield distinct values), so they must stay unflagged.
        "def test_foo():\n    assert get_time() == get_time()\n",
        "def test_foo():\n    assert uuid4() != uuid4()\n",
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


#: pytest-framework fixtures / ``unittest.mock`` attributes whose values are *real*
#: containers (recorded call history, configured return values, captured kwargs) —
#: membership against them is meaningful and must not be flagged by the
#: mock-membership lens.
_MOCK_REAL_CONTAINER_ACCESSORS = frozenset(
    {
        "args",
        "await_args",
        "await_args_list",
        "call_args",
        "call_args_list",
        "call_count",
        "called",
        "kwargs",
        "method_calls",
        "mock_calls",
        "return_value",
        "side_effect",
    }
)

#: ``unittest.mock`` factory constructors: a call to any of these produces a fresh
#: double whose ``__contains__`` (for the magic variants) returns a truthy sentinel.
_MOCK_FACTORY_CONSTRUCTORS = frozenset({"Mock", "MagicMock", "AsyncMock"})


def _is_mock_flagged_name(name: str) -> bool:
    """Return True when ``name`` (a bare root identifier) reads as a mock double
    — ``mock``/``mocker``/``mocks``, ``_mock*``, ``*_mock``, and ``mock*``
    spellings cover the suite's conventional double variable names
    (``mock_run``, ``mock_create``, ``_mock_canary``, ...)."""
    root = name.lower()
    return root in ("mocker", "mocks") or root.startswith(("mock", "_mock")) or root.endswith("_mock")


def _mock_container_expression(node: ast.AST) -> str | None:
    """Return the source spelling of ``node`` when it denotes a ``unittest.mock``
    double used as the *container* side of a membership test.

    Accepts a mock factory call (``MagicMock()``, ``mocker.MagicMock()``,
    ``mock.AsyncMock()``, ...) or a plain name/attribute chain rooted at a
    mock-flagged name and made only of plain attribute links — ``mock``,
    ``mock.calls``, ``mocker.recorded``, ``_mock_create.history``. Returns
    ``None`` for anything else, so the real recorded-call containers and
    configured values are never wrongly taken for doubles:

    - attribute chains that pass through a real-container accessor (the
      ``call_args``/``call_args_list``/``kwargs``/``return_value`` family in
      ``_MOCK_REAL_CONTAINER_ACCESSORS``) — membership there is meaningful
    - subscripts (``mock.return_value['plugins']``) — the subscript is resolved
      against whatever the double's attribute actually held
    - method calls on the double (``mock.items()``) and non-factory calls
      (``mock_run.error_detail.lower()``) — the trailing call resolves the real
      value
    - any expression not rooted at a mock-flagged name
    """
    if isinstance(node, ast.Call):
        if _callable_name(node.func) in _MOCK_FACTORY_CONSTRUCTORS:
            return ast.unparse(node)
        return None
    if isinstance(node, (ast.Name, ast.Attribute)):
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            if cur.attr in _MOCK_REAL_CONTAINER_ACCESSORS:
                return None
            cur = cur.value
        if isinstance(cur, ast.Name) and _is_mock_flagged_name(cur.id):
            return ast.unparse(node)
    return None


def _mock_membership_probe_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test probes
    membership (``in``/``not in``) against a ``unittest.mock`` double.

    ``MagicMock`` implements ``__contains__`` by minting a fresh child double and
    returning it — a value that is always truthy — so the probe never inspects
    the recorded calls: ``assert x in <mock>`` ALWAYS PASSES (a silent false
    green a mutation-testing run believes verifies behaviour) and ``assert x not
    in <mock>`` ALWAYS FAILS. Plain ``Mock`` lacks ``__contains__`` and instead
    dies with a confusing ``TypeError`` at runtime. Only the container side is
    examined (the *right* operand of ``in``); the probed value may be anything.
    Only assertions are covered — an ``in`` probe used as a branch condition is
    a different (control-flow) statement."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare):
                continue
            for op, right in zip(sub.ops, sub.comparators, strict=True):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                container = _mock_container_expression(right)
                if container is None:
                    continue
                verdict = "ALWAYS PASSES" if isinstance(op, ast.In) else "ALWAYS FAILS"
                detail = (
                    f"assert {ast.unparse(node.test)} — membership against the mock "
                    f"{container!r}; a mock's __contains__ returns an always-truthy "
                    f"child double, so this check {verdict} regardless of the recorded calls"
                )
                if (node.lineno, detail) not in found:
                    found.append((node.lineno, detail))
    return found


def test_no_mock_membership_probe():
    """``assert x in <mock>`` / ``assert x not in <mock>`` probe membership
    against a ``unittest.mock`` double — the container side is the double, and
    its ``__contains__`` (for the magic variants) mints a fresh child double and
    returns it as an always-truthy sentinel rather than consulting the recorded
    calls. ``assert x in <mock>`` therefore ALWAYS PASSES (a silent false green
    that a mutation-testing run believes verifies behaviour) and ``assert x not
    in <mock>`` ALWAYS FAILS, both decided at source time; a plain ``Mock``
    without magic-method support dies with a confusing ``TypeError`` instead.
    This is the ``__contains__`` sibling of the ``__eq__``-identity Mock lenses:
    the recorded-argument question is ``mock.assert_any_call(...)``/
    ``assert_not_called()`` or membership against the documented recorded-call
    containers (``mock.call_args_list``/``mock.method_calls``/``mock.mock_calls``/
    ``mock.await_args_list``), never ``in`` on the double itself."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _mock_membership_probe_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} mock-membership assertion(s).\n"
        "A mock's __contains__ returns an always-truthy child double, so 'assert x in <mock>'\n"
        "always passes and 'assert x not in <mock>' always fails no matter what was recorded.\n"
        "Use mock.assert_any_call(...)/assert_not_called(), or the recorded-call containers\n"
        "(call_args_list/method_calls/mock_calls/await_args_list) instead.\n" + "\n".join(violations)
    )


def test_mock_membership_lens_flags_mock_containers():
    """Synthetic positive/negative control for the mock-membership lens: must
    flag ``in``/``not in`` against a mock factory call or a plain attribute
    chain rooted at a mock-flagged name, and ignore membership against the
    real recorded-call/configured-value containers, subscripts, method calls
    on the double, non-mock containers, and non-``assert`` branch probes."""
    positive_sources = [
        "def test_foo():\n    assert 'x' in mock\n",
        "def test_foo():\n    assert 'x' not in mock\n",
        "def test_foo():\n    assert result.value in _mock_create\n",
        "def test_foo():\n    assert 'x' not in mocker.recorded\n",
        "def test_foo():\n    assert 'x' in mock.calls\n",
        "def test_foo():\n    assert 'x' in MagicMock()\n",
        "def test_foo():\n    assert 'x' not in mocker.MagicMock()\n",
        "def test_foo():\n    assert 'x' in empty_async_mock.history\n",
        "def test_foo():\n    assert 'a' in mock and 'b' not in await_mock\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _mock_membership_probe_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert 'x' in mock.call_args_list\n",
        "def test_foo():\n    assert 'x' not in mock.method_calls\n",
        "def test_foo():\n    assert x in mock.kwargs\n",
        "def test_foo():\n    assert 'x' in mock.return_value\n",
        "def test_foo():\n    assert 'x' in mock.await_args_list[0].kwargs\n",
        "def test_foo():\n    assert 'health' in mock_run.error_detail.lower()\n",
        "def test_foo():\n    assert '_run_overrides' in mock_create.await_args_list[k].kwargs['input_payload']\n",
        "def test_foo():\n    assert 'x' in mock.items()\n",
        "def test_foo():\n    assert 'x' in mocker.patch('a').return_value\n",
        "def test_foo():\n    assert x in real_list\n",
        "def test_foo():\n    assert 'x' in response.json()['data']\n",
        "def test_foo():\n    assert 'x' in 'mock string'\n",
        "def test_foo():\n    mock.assert_any_call('x', 'y')\n",
        "def test_foo():\n    if 'x' in mock:\n        pytest.skip('n/a')\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _mock_membership_probe_violations(tree), f"lens should NOT flag:\n{source}"


def _parametrize_argvalue_lists(tree: ast.AST) -> list[tuple[int, list[ast.expr], ast.Call]]:
    """Return ``(lineno, argvalues.elts, decorator_call)`` for every
    ``@...parametrize`` decorator whose ``argvalues`` is a statically-known
    ``list``/``tuple`` literal. Only decorator applications are considered — a
    bare ``parametrize(...)`` call inside a body is not pytest parametrization
    and belongs to a different lens. The parametrize-adjacent lenses derive
    their signal from the decorator call (its ``ids=`` keyword), from
    ``len(elts)`` (``== 0``, ``== 1``, ...) or from the elements themselves
    (duplicate detection), so a new lens never re-copies the decorator walk."""
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
            found.append((dec.lineno, argvalues.elts, dec))
    return found


def _single_case_parametrize_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize``
    decorator whose ``argvalues`` holds exactly one case. Only decorator
    applications are considered — a bare ``parametrize(...)`` call inside a
    body is not pytest parametrization and belongs to a different lens."""
    return [
        (lineno, "parametrize with a single case in argvalues — collapse to a plain test")
        for lineno, elts, _dec in _parametrize_argvalue_lists(tree)
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
        for lineno, elts, _dec in _parametrize_argvalue_lists(tree)
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


def _unbounded_async_wait_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``asyncio.wait_for`` /
    ``asyncio.wait`` call without a timeout bound.

    ``asyncio.wait_for(coro)`` with no ``timeout`` argument — or an explicit
    ``timeout=None``, the API default meaning "wait forever" — suspends until
    the awaited coroutine finishes with no upper bound, so a coroutine that
    never completes hangs the test, and every test after it on the same event
    loop, indefinitely. ``asyncio.wait(tasks)`` has the same contract and the
    same ``None`` default. This is the asyncio twin of the unbounded-subprocess
    and unbounded-thread-``join`` safety nets: bound the wait so a hang fails
    loudly as a ``TimeoutError`` with a named bound instead of stalling the
    whole run. ``wait_for(coro, 0)`` and any non-``None`` ``timeout`` (literal,
    name, call) are bounded and left alone. Only the ``asyncio.*`` attribute
    spelling is matched; a local helper named ``wait_for``/``wait`` cannot be
    distinguished statically and is deliberately not flagged."""
    found: list[tuple[int, str]] = []

    def _timeout_is_bounded(call: ast.Call) -> bool:
        if call.keywords:
            for kw in call.keywords:
                if kw.arg == "timeout" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                    return True
        if len(call.args) >= 2:
            second = call.args[1]
            if not (isinstance(second, ast.Constant) and second.value is None):
                return True
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("wait_for", "wait"):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "asyncio":
            continue
        if not node.args:
            continue
        if _timeout_is_bounded(node):
            continue
        found.append(
            (
                node.lineno,
                f"{ast.unparse(node)} — unbounded asyncio {'wait_for' if node.func.attr == 'wait_for' else 'wait'} "
                "with no timeout; pass timeout=<secs> so a hung coroutine fails loudly "
                "instead of stalling the whole test run",
            )
        )
    return found


def test_no_unbounded_async_wait():
    """An ``asyncio.wait_for``/``asyncio.wait`` called without a timeout bound
    can hang the whole test run: a coroutine or task that never completes makes
    the await wait forever, the runner simply stops, and every test after it on
    the same event loop is lost without a trace. This is the asyncio sibling of
    the unbounded-subprocess and unbounded-thread-``join`` lenses, which guard
    the child-process and in-process versions of the same hazard. Always pass
    an explicit numeric ``timeout=<secs>`` so a hang surfaces as a bounded
    ``TimeoutError`` instead of stalling CI."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unbounded_async_wait_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unbounded asyncio wait call(s).\n"
        "Give every asyncio.wait_for / asyncio.wait an explicit timeout bound: "
        "pass timeout=<secs> (a None or omitted timeout means 'wait forever' "
        "and can hang the whole test run).\n" + "\n".join(violations)
    )


def test_unbounded_async_wait_lens_flags_hang_risks():
    """Synthetic positive/negative control for the unbounded-async-wait lens:
    it must flag ``asyncio.wait_for``/``asyncio.wait`` calls with an omitted or
    ``None`` timeout (positional or keyword, awaited or not), and ignore
    bounded calls (numeric literal or bound name), ``wait_for(coro, 0)``,
    non-``asyncio`` callers, and local helpers that merely share the name."""
    positive_sources = [
        "async def test_foo():\n    await asyncio.wait_for(coro())\n",
        "async def test_foo():\n    await asyncio.wait_for(coro(), None)\n",
        "async def test_foo():\n    await asyncio.wait_for(coro(), timeout=None)\n",
        "async def test_foo():\n    await asyncio.wait(futures)\n",
        "async def test_foo():\n    await asyncio.wait(futures, timeout=None)\n",
        "async def test_foo():\n    asyncio.wait_for(coro())\n",
        "async def test_foo():\n    task = asyncio.create_task(coro())\n    await asyncio.wait_for(task)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unbounded_async_wait_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "async def test_foo():\n    await asyncio.wait_for(coro(), 5)\n",
        "async def test_foo():\n    await asyncio.wait_for(coro(), timeout=5)\n",
        "async def test_foo():\n    await asyncio.wait_for(coro(), timeout=TIMEOUT)\n",
        "async def test_foo():\n    await asyncio.wait_for(coro(), 0)\n",
        "async def test_foo():\n    await asyncio.wait(futures, timeout=10)\n",
        "async def test_foo():\n    await asyncio.wait(futures, timeout=SHORT)\n",
        "async def test_foo():\n    await asyncio.wait_for(proc.communicate(), 10)\n",
        "def test_foo():\n    wait_for(a)\n",
        "def test_foo():\n    wait(a)\n",
        "async def test_foo():\n    await socket.wait()\n",
        "async def test_foo():\n    await asyncio.gather(coro())\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unbounded_async_wait_violations(tree), f"lens should NOT flag:\n{source}"


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
        if not (node.name.startswith("test_") or any(_is_mark_decorator(d) for d in node.decorator_list)):
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
    for lineno, elts, _dec in _parametrize_argvalue_lists(tree):
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


#: Parametrize case-list sizes at/above which a missing ``ids=`` is flagged.
#: Below this threshold a handful of auto-indexed ids (``arr[0]``..``arr[3]``)
#: are still tractable to map by hand; past it, counting from the top of the
#: case list to identify a failed input is exactly the chore ``ids=`` exists
#: to remove. A reader (or a ``.quarantine.yml`` entry, which records these
#: nodeids verbatim) cannot distinguish the cases of an anonymous matrix.
_PARAMETRIZE_IDS_MIN_CASES = 8


def _parametrize_without_ids_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@...parametrize`` whose
    literal ``argvalues`` holds ``>= _PARAMETRIZE_IDS_MIN_CASES`` cases and
    that never names them: no ``ids=`` keyword and not every element carries a
    per-case ``pytest.param(..., id=...)``. A large matrix without case names
    reports failures as ``test_x[arr0]``..``test_x[arrN-1]`` — a triage must
    count from the top of the case list to learn which input failed, and if
    the matrix is later quarantined the recorded nodeid identifies nothing.
    ``ids=`` naming every case, or per-case ``pytest.param(id=...)`` on the
    elements that matter, restores self-documenting nodeids. Parametrizes with
    a non-literal case list are left alone (the count is not statically
    known); small matrices are left alone; a matrix where EVERY element is a
    ``pytest.param(..., id=...)`` is already self-documented."""
    violations = []
    for _lineno, elts, dec in _parametrize_argvalue_lists(tree):
        if any(kw.arg == "ids" for kw in dec.keywords):
            continue
        if len(elts) < _PARAMETRIZE_IDS_MIN_CASES:
            continue
        per_case_ids = sum(
            1
            for el in elts
            if isinstance(el, ast.Call) and _decorator_name(el) == "param" and any(kw.arg == "id" for kw in el.keywords)
        )
        if per_case_ids == len(elts):
            continue
        violations.append(
            (
                dec.lineno,
                f"parametrize with {len(elts)} cases and no ids= — failure nodeids are "
                f"auto-indexed (arr[0]..arr[{len(elts) - 1}]), so triage must count "
                "cases by hand",
            )
        )
    return violations


def test_no_large_parametrize_without_ids():
    """A ``@pytest.mark.parametrize`` whose literal ``argvalues`` holds a large
    case matrix (``>= 8`` cases) and that never names its cases leaves failure
    reporting opaque: pytest renders each item as ``test_x[arr0]``,
    ``test_x[arr1]``, ... and the reader must count from the top of the case
    list to learn which input failed. That opacity is not merely cosmetic:
    the auto-indexed nodeid is the identifier that surfaces in CI logs and the
    exact string a ``.quarantine.yml`` entry would record, so an anonymous
    matrix is indistinguishable from one whose ``ids=`` were never written.
    ``ids=`` naming every case (or per-case ``pytest.param(..., id=...)`` when
    only a few elements need names) restores self-describing nodeids. Small
    matrices, non-literal case lists, and parametrizes where every element
    already carries ``pytest.param(id=...)`` are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _parametrize_without_ids_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} parametrize decorator(s) with a large case list and no ids.\n"
        "A large unlabelled matrix reports failures as test_x[arr0]..test_x[arrN-1] — readers "
        "and quarantine entries cannot tell the cases apart. Add ids= naming every case "
        "(or per-case pytest.param(id=...) when only some need names).\n" + "\n".join(violations)
    )


def test_parametrize_without_ids_lens_flags_large_matrices():
    """Synthetic positive/negative control for the parametrize-without-ids
    lens: must flag a large (>= 8 case) literal matrix with neither ``ids=``
    nor per-case ``pytest.param(id=...)`` on every element, and ignore small
    matrices, matrices with ``ids=``, fully self-documented matrices, non-
    literal case lists, and a bare ``parametrize(...)`` body call."""
    positive_sources = [
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', [1,2,3,4,5,6,7,8])\n"
            "    def test_bar(x):\n        assert x\n"
        ),
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('a,b', [(1,1),(2,2),(3,3),(4,4),(5,5),(6,6),(7,7),(8,8),(9,9)])\n"
            "    def test_bar(a, b):\n        assert a == b\n"
        ),
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', argvalues=['a','b','c','d','e','f','g','h','i','j'])\n"
            "    def test_bar(x):\n        assert x\n"
        ),
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _parametrize_without_ids_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    @pytest.mark.parametrize('x', [1,2,3])\n    def test_bar(x):\n        assert x\n",
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', [1,2,3,4,5,6,7,8],\n"
            "        ids=['a','b','c','d','e','f','g','h'])\n"
            "    def test_bar(x):\n        assert x\n"
        ),
        (
            "def test_foo():\n"
            "    @pytest.mark.parametrize('x', [pytest.param(1, id='a'), pytest.param(2, id='b'), \n"
            "        pytest.param(3, id='c'), pytest.param(4, id='d'), pytest.param(5, id='e'), \n"
            "        pytest.param(6, id='f'), pytest.param(7, id='g'), pytest.param(8, id='h'), \n"
            "        pytest.param(9, id='i')])\n"
            "    def test_bar(x):\n        assert x\n"
        ),
        "def test_foo():\n    @pytest.mark.parametrize('x', CASES)\n    def test_bar(x):\n        assert x\n",
        "def test_foo():\n    parametrize('x', [1,2,3,4,5,6,7,8])\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _parametrize_without_ids_violations(tree), f"lens should NOT flag:\n{source}"


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
        name = _callable_name(func)
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
"""``pytest.raises``/``pytest.warns`` names, matched on the final name of the
attribute chain (``pytest.raises`` -> ``raises``) or on a bare imported
``raises(...)``/``warns(...)``. Custom helpers (``assert_raises``, ``rejects``,
...) and aliases that rename the context manager are deliberately not matched:
their signature does not necessarily take an exception class positionally, and
only ``pytest.raises``/``pytest.warns`` have the ``match=`` keyword that
narrows an ``AssertionError`` expectation. Shared by the lenses that inspect
the call's expected-exception argument for broad classes and by the lens that
checks the ``with``/``async with`` body is not empty."""

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
        name = _callable_name(f)
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
            dname = _callable_name(func)
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
        name = _callable_name(f)
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


def _mock_constructor_container_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression nests a freshly-constructed Mock *inside* a container literal
    (``[Mock()]``, ``{'k': MagicMock()}``, ``(AsyncMock(),)``, ``{Mock()}``).

    A fresh Mock is only meaningful as an obligation for ``assert_called_with``
    to match; inside a container it is compared by identity (``__eq__``
    defaults to ``is``), so ``assert result == {'s': MagicMock()}`` ALWAYS
    FAILS and ``assert result != [...]`` ALWAYS PASSES no matter what
    ``result`` evaluates to, while ``assert [Mock()]`` (a non-empty container
    is always truthy) ALWAYS PASSES. The outcome is fixed at source time in
    every case. Only the *container-nesting* position is flagged here —
    a fresh constructor that is the assert's test expression, a ``not``-wrap,
    or the direct operand of a single-operator comparison belongs to the
    Mock-constructor lens, so the two never double-report the same line. No
    name resolution is involved, so a mock bound to a name elsewhere is never
    implicated and the lens has no false positives from mocking assignments or
    ``patch`` bindings."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        # Hunt for a fresh-mock constructor nested anywhere inside a container
        # literal that itself sits inside the assert's test expression.
        for container in ast.walk(node.test):
            if not isinstance(container, (ast.List, ast.Dict, ast.Tuple, ast.Set)):
                continue
            elements = (
                list(container.elts)
                if isinstance(container, (ast.List, ast.Tuple, ast.Set))
                else (list(container.keys) + list(container.values))
            )
            if any(_is_mock_constructor_call(el) for el in elements):
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(node.test)} — a fresh Mock nested inside a "
                        "container literal is compared by identity, so the outcome is fixed "
                        "at source time",
                    )
                )
                break
    return found


def test_no_mock_constructor_in_container_asserts():
    """An ``assert`` that nests a freshly-constructed Mock *inside* a container
    literal is dead code with a fixed outcome, like the direct Mock-constructor
    lens. A fresh Mock compares by identity (``__eq__`` defaults to ``is``), so
    ``assert result == {'s': MagicMock()}`` always fails and
    ``assert result != [AsyncMock()]`` always passes regardless of what
    ``result`` evaluates to, and ``assert [Mock()]`` always passes because a
    non-empty container is truthy. The nested-container spelling is a distinct
    AST shape from the direct positions the Mock-constructor lens owns, so it
    needs its own detector. The double should be configured
    (``return_value``/``side_effect``) and asserted through ``assert_called*``
    /attribute checks, never compared to through a container wrapper."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _mock_constructor_container_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a newly-constructed Mock nested in a container.\n"
        "A fresh Mock compares by identity and a non-empty container is always truthy, so the\n"
        "outcome is decided at source time, never by the code under test. Configure the double\n"
        "(return_value/side_effect) and assert through assert_called* instead.\n" + "\n".join(violations)
    )


def test_mock_constructor_container_lens_flags_nested_constructors():
    """Synthetic positive/negative control for the container-nested Mock lens: it
    must flag an ``assert`` that nests a fresh Mock constructor inside a
    list/dict/tuple/set literal in any operand position (equality with the
    container, membership against it, bare truthiness) and ignore the direct
    constructor positions owned by the Mock-constructor lens (bare, ``not``-wrapped,
    single-comparison operand), already-bound mock names, mocks passed as call
    arguments, and containers built from non-mock values (including ``ANY``)."""
    positive_sources = [
        "def test_foo():\n    assert result == [MagicMock()]\n",
        "def test_foo():\n    assert result != [AsyncMock()]\n",
        "def test_foo():\n    assert result == {'status': MagicMock()}\n",
        "def test_foo():\n    assert result in (Mock(), 'x')\n",
        "def test_foo():\n    assert result not in {MagicMock()}\n",
        "def test_foo():\n    assert [Mock()]\n",
        "def test_foo():\n    assert (AsyncMock(),)\n",
        "def test_foo():\n    assert {'k': mocker.MagicMock()} == result\n",
        "def test_foo():\n    assert result == [mock.Mock()]\n",
        "def test_foo():\n    assert result == {'a': 1, 'b': unittest.mock.AsyncMock()}\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _mock_constructor_container_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert Mock()\n",
        "def test_foo():\n    assert result == Mock()\n",
        "def test_foo():\n    assert not MagicMock()\n",
        "def test_foo():\n    assert result is not None\n",
        "def test_foo():\n    mock = MagicMock()\n    assert result == [mock]\n",
        "def test_foo():\n    assert result == [m]\n",
        "def test_foo():\n    assert result == [1, 2]\n",
        "def test_foo():\n    assert result == {'k': 'v'}\n",
        "def test_foo():\n    assert result == [ANY]\n",
        "def test_foo():\n    mock.assert_called_with([1, MagicMock(return_value=2)])\n",
        "def test_foo():\n    assert result == []\n",
        "def test_foo():\n    assert result == {}\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _mock_constructor_container_violations(tree), f"lens should NOT flag:\n{source}"


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


def _constant_boolean_absorbent_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression is — or ``not``-wraps — a ``BoolOp`` whose verdict a literal
    constant operand pins.

    An ``or`` disjunction that contains a truthy literal constant (``assert x or
    True``, ``assert x or 1``, ``assert x or "y"``) short-circuits to a truthy
    value for every input, so the assert ALWAYS PASSES no matter what the code
    under test does; an ``and`` conjunction that contains a falsy literal
    constant (``assert x and False``, ``assert x and 0``, ``assert x and None``)
    short-circuits to a falsy value, so the assert ALWAYS FAILS. Such a constant
    is an identity/absorbent element for the operator: it pins the outcome and
    absorbs the value's contribution, a fixed result exactly like the
    ``assert True``/``assert False`` literal forms but spelled through a
    compound. ``not`` around the compound (``assert not (x or True)``,
    ``assert not (x and False)``) inverts the verdict. Only *literal*
    ``ast.Constant`` operands of the top-level ``BoolOp`` are counted;
    complex-valued constants are deliberately excluded (their truthiness is an
    edge case the sibling literal-constant lens also skips). A falsy constant
    under ``or`` (``assert x or None``) and a truthy constant under ``and``
    (``assert x and True``) do NOT pin the outcome — those are the legitimate
    default/refinement idioms and are left alone. These are almost always a
    leftover from stripping a real condition down while debugging, where the
    operator was pasted in with a throwaway literal."""
    found: list[tuple[int, str]] = []

    def _absorbing_constant(node: ast.BoolOp) -> ast.Constant | None:
        is_and = isinstance(node.op, ast.And)
        for value in node.values:
            if not isinstance(value, ast.Constant) or isinstance(value.value, complex):
                continue
            constant = value.value
            if (is_and and not constant) or (not is_and and constant):
                return value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        negated = False
        test = node.test
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            negated = True
            test = test.operand
        if not isinstance(test, ast.BoolOp):
            continue
        absorbing = _absorbing_constant(test)
        if absorbing is None:
            continue
        is_and = isinstance(test.op, ast.And)
        # ``or``+truthy and ``and``+falsy pass; the negated twins invert.
        verdict = "always PASSES" if is_and == negated else "always FAILS"
        operator = "and" if is_and else "or"
        found.append(
            (
                node.lineno,
                f"assert {'not ' if negated else ''}{ast.unparse(test)} — the "
                f"{'falsy ' if is_and else 'truthy '}constant {ast.unparse(absorbing)} under "
                f"'{operator}' absorbs the other operand(s), so the assert {verdict} "
                "regardless of the code under test",
            )
        )
    return found


def test_no_constant_absorbed_boolean_assertions():
    """An ``assert`` whose test expression is a ``BoolOp`` coupling a value
    with a literal constant that pins the verdict is dead code with a fixed
    outcome. ``or`` with a truthy constant (``assert x or True``,
    ``assert x or 1``) ALWAYS PASSES and ``and`` with a falsy constant
    (``assert x and False``, ``assert x and 0``) ALWAYS FAILS, no matter what
    the code under test does — a constant-absorbed twin of the literal-constant
    and complementary-boolean lenses that needs its own AST shape (a ``BoolOp``
    with a ``Constant`` operand, which the literal-constant lens provably
    misses because it only reads the whole test expression, and the
    complementary lens misses because it hunts paired operands). These are
    almost always a leftover from stripping a condition down while debugging;
    assert the real behaviour instead."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _constant_boolean_absorbent_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) whose verdict a constant boolean operand absorbs.\n"
        "'or' joined with a truthy constant is always True (ALWAYS PASSES) and 'and' joined with "
        "a falsy constant is always False (ALWAYS FAILS), so the outcome never depends on the "
        "code under test.\nAssert the real condition instead.\n" + "\n".join(violations)
    )


def test_constant_absorbed_boolean_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the constant-absorbed-boolean
    lens: must flag an ``assert`` whose top-level ``BoolOp`` (or a single
    ``not`` around it) couples a value with a literal constant that pins the
    result — truthy constants under ``or``, falsy constants under ``and``, in
    either operand position and inside a longer chain — and ignore constant
    operands that do NOT pin the verdict (falsy under ``or``, truthy under
    ``and``), single operands, comparisons/``not``-wrapped comparisons, and the
    complementary shapes owned by the sibling lens."""
    positive_sources = [
        "def test_foo():\n    assert x or True\n",
        "def test_foo():\n    assert 1 or x\n",
        "def test_foo():\n    assert x or 2.5\n",
        "def test_foo():\n    assert x or 'y'\n",
        "def test_foo():\n    assert x and False\n",
        "def test_foo():\n    assert 0 and x\n",
        "def test_foo():\n    assert x and None\n",
        "def test_foo():\n    assert x and ''\n",
        "def test_foo():\n    assert not (x or True)\n",
        "def test_foo():\n    assert not (x and False)\n",
        "def test_foo():\n    assert x or status or 1\n",
        "def test_foo():\n    assert x and status and 0\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _constant_boolean_absorbent_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x or None\n",
        "def test_foo():\n    assert x or False\n",
        "def test_foo():\n    assert x and True\n",
        "def test_foo():\n    assert 1 and x\n",
        "def test_foo():\n    assert x or y\n",
        "def test_foo():\n    assert x and y\n",
        "def test_foo():\n    assert x\n",
        "def test_foo():\n    assert True\n",
        "def test_foo():\n    assert not x\n",
        "def test_foo():\n    assert x == 1 or y == 2\n",
        "def test_foo():\n    assert result is not None and result.status == 'ok'\n",
        "def test_foo():\n    assert x and not x\n",
        "def test_foo():\n    assert x or not x\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _constant_boolean_absorbent_assert_violations(tree), f"lens should NOT flag:\n{source}"


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


_CONTAINER_LITERAL_NODES = (ast.List, ast.Dict, ast.Set, ast.Tuple)


def _container_literal_truthiness_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression is a *container literal* standing alone (positionally or under
    a ``not``).

    A list/dict/set/tuple *literal* is truthy exactly when non-empty and falsy
    exactly when empty — the container's own structure decides the outcome at
    source time, never the values inside (an empty container is falsy even when
    every element would be truthy, and a non-empty container is truthy even
    when every element is falsy). So ``assert [x]`` ALWAYS PASSES and
    ``assert not [x]`` ALWAYS FAILS regardless of ``x``, while ``assert []``
    ALWAYS FAILS and ``assert not []`` ALWAYS PASSES. The ``assert (a, b)`` /
    ``assert [a, b]`` shape is the classic forgot-``and`` bug: the condition is
    wrapped so the assertion measures the container, not the conditions.

    ``ast.List``/``ast.Tuple``/``ast.Set``/``ast.Dict`` nodes are exactly the
    literal forms; comprehensions (``ast.ListComp``/``ast.SetComp``/
    ``ast.DictComp``) and unpacked dicts (``ast.Dict`` with a ``None`` key from
    ``{**cfg}``) are left alone because their emptiness depends on a runtime
    iterable.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        negated = False
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            negated = True
            test = test.operand
        if not isinstance(test, _CONTAINER_LITERAL_NODES):
            continue
        if isinstance(test, ast.Dict):
            if any(key is None for key in test.keys):
                continue
            empty = not test.keys
        else:
            empty = not test.elts
        if empty:
            verdict = "ALWAYS FAILS" if not negated else "ALWAYS PASSES"
            shape = "empty"
        else:
            verdict = "ALWAYS PASSES" if not negated else "ALWAYS FAILS"
            shape = "non-empty"
        found.append(
            (
                node.lineno,
                f"assert {ast.unparse(test)} — a {shape} container literal is "
                f"always {'' if (empty is not negated) else 'not '}truthy, so this {verdict} "
                f"regardless of its elements; assert against the behaviour under test instead",
            )
        )
    return found


def test_no_container_literal_truthiness_assertions():
    """A container *literal* standing alone as an ``assert`` operand is a
    fixed-outcome assertion: the container's own emptiness decides the
    truthiness at source time, never the code under test. ``assert [x]`` /
    ``assert {}`` ALWAYS PASS and ``assert not [x]`` ALWAYS FAILS no matter
    what the elements evaluate to — the ``assert (a, b)`` shape is the classic
    forgot-``and`` bug that silently becomes an always-true assertion — and an
    *empty* literal (``assert []`` / ``assert not ()``) is decided by the
    literal too. The sibling always-pass/fail lens only folds ``ast.Constant``
    operands and ducked ``[]``/``{}`` *equality* comparisons have their own
    lenses; this is the gap where the container stands alone."""

    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _container_literal_truthiness_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a container literal.\n"
        "A literal container is truthy iff non-empty, so the outcome is fixed at write time.\n"
        "Assert against the behaviour under test (e.g. assert a and b, not assert (a, b)).\n" + "\n".join(violations)
    )


def test_container_literal_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the container-literal lens,
    mirroring the constant-literal lens pattern: it must flag every ``assert``
    whose operand is a list/dict/set/tuple literal (empty or not, bare or
    ``not``-wrapped, nested) and ignore comparisons, comprehension forms,
    unpacked dicts, attribute/name operands, and runtime containers."""
    positive_sources = [
        "def test_foo():\n    assert [x]\n",
        "def test_foo():\n    assert not {y: z}\n",
        "def test_foo():\n    assert []\n",
        "def test_foo():\n    assert not ()\n",
        "def test_foo():\n    assert (a, b)\n",
        "def test_foo():\n    assert [a, b, c]\n",
        "def test_foo():\n    assert {1}\n",
        "def test_foo():\n    assert not [[[]]]\n",
        "def test_foo():\n    result = f()\n    assert {}\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _container_literal_truthiness_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x\n",
        "def test_foo():\n    assert not x\n",
        "def test_foo():\n    assert x == []\n",
        "def test_foo():\n    assert x != {}\n",
        "def test_foo():\n    assert x in []\n",
        "def test_foo():\n    assert [] == []\n",
        "def test_foo():\n    assert [x for x in y]\n",
        "def test_foo():\n    assert {k: v for k, v in items()}\n",
        "def test_foo():\n    assert {**cfg}\n",
        "def test_foo():\n    assert result.error == []\n",
        "def test_foo():\n    assert list()\n",
        "def test_foo():\n    assert ns.items\n",
        "def test_foo():\n    assert (x == 1)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _container_literal_truthiness_violations(tree), f"lens should NOT flag:\n{source}"


def _nested_test_functions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``test_*`` (or
    ``@pytest.mark``-decorated) function defined inside another function.

    pytest only collects ``test_*`` functions at module and class scope, so a
    test defined inside a function body is never collected and never runs —
    silently dropping its coverage with no warning. ``@pytest.fixture`` and
    other local helpers are deliberately excluded: those are the legitimate
    nested-helper spellings and pytest asyncio/plugins may reference them.
    ``@pytest.mark``-decorated nested functions are included, because the
    decorator marks intent to be a test that pytest still will not collect.
    """
    stack: list[tuple[str, ast.AST]] = []  # (kind, node) for enclosing defs/classes

    def _is_fixture(decs: list[ast.AST]) -> bool:
        return any(_decorator_name(d) == "fixture" for d in decs)

    found: list[tuple[int, str]] = []

    def _visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inside_fn = any(kind == "fn" for kind, _ in stack)
            is_here_a_test = node.name.startswith("test_") or any(_is_mark_decorator(d) for d in node.decorator_list)
            if inside_fn and is_here_a_test and not _is_fixture(node.decorator_list):
                found.append((node.lineno, f"{node.name}() defined inside another function — pytest never collects it"))
            stack.append(("fn", node))
            for child in node.body:
                _visit(child)
            stack.pop()
            return
        if isinstance(node, ast.ClassDef):
            stack.append(("cls", node))
            for child in node.body:
                _visit(child)
            stack.pop()
            return
        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(tree)
    return found


def test_no_nested_test_functions():
    """A ``test_*`` function (or ``@pytest.mark``-decorated function) defined
    *inside another function* is never collected by pytest, so the coverage it
    carries silently drops from every run with no warning. pytest only
    collects ``test_*`` at module and class scope; a test nested inside a test
    or helper body is dead code that a reader — and a mutation-testing run —
    believes is running. These are almost always an indentation accident or a
    helper miscast as a ``test_``. Hoist a real test to module scope, or
    rename a helper that merely happens to start with ``test_``. Nested
    ``@pytest.fixture`` helpers and non-test local defs/classes are
    deliberately left alone."""

    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _nested_test_functions(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} test function(s) nested inside another function.\n"
        "pytest only collects test_* at module/class scope, so a nested test never runs —\n"
        "its coverage silently drops from every run. Hoist it to module scope, or rename a\n"
        "helper that only happens to start with 'test_'.\n" + "\n".join(violations)
    )


def test_nested_test_lens_flags_uncollected_tests():
    """Synthetic positive/negative control for the nested-test lens: must flag
    a ``test_*`` (or ``@pytest.mark``-decorated) function nested inside any
    function (sync, async, or another test), and ignore nested fixtures,
    non-``test_`` local helpers, and module/class-scope tests (which pytest
    does collect)."""
    positive_sources = [
        "def test_foo():\n    def test_bar():\n        assert 1 == 1\n",
        "def test_foo():\n    def helper():\n        def test_bar():\n            assert 1 == 1\n",
        "async def test_foo():\n    def test_bar():\n        assert 1 == 1\n",
        "def test_foo():\n    @pytest.mark.asyncio\n    def test_bar():\n        assert 1 == 1\n",
        # Mark-detection branch exercised with a NON-``test_`` name: this only
        # flags because ``@pytest.mark.asyncio`` is recognised as a marker.
        "def test_foo():\n    @pytest.mark.asyncio\n    def _coro():\n        assert 1 == 1\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _nested_test_functions(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert 1 == 1\n",
        "def test_foo():\n    def helper():\n        return 1\n    assert helper() == 1\n",
        "def test_foo():\n    @pytest.fixture\n    def fxt():\n        return 1\n",
        "def test_bar():\n    assert 1 == 1\n",
        "class TestSuite:\n    def test_bar(self):\n        assert 1 == 1\n",
        "def test_bar():\n    assert 1 == 1\n",
        "@pytest.mark.parametrize('x', [1])\ndef test_bar(x):\n    assert x == 1\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _nested_test_functions(tree), f"lens should NOT flag:\n{source}"


_MUTATING_ENVIRON_METHODS = frozenset({"pop", "update", "setdefault", "clear", "__setitem__", "__delitem__"})


def _is_environ_reference(node: ast.AST) -> bool:
    """Return True for the two spellings of the process-environment mapping:
    the attribute path ``os.environ`` and the bare ``environ`` name (the
    ``from os import environ`` form)."""
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
    mutation spellings are ``os.environ[key] = ...`` /
    ``del os.environ[key]`` (subscript store/delete on either environ
    spelling), the ``os.environ`` mutating methods (``pop``/``update``/
    ``setdefault``/``clear`` — the ``__*__`` twins exist in pydantic-spelled
    code and are covered too), and the ``os.putenv()`` / ``os.unsetenv()``
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
    """Direct ``os.environ`` mutation (subscript set/delete, the mutating
    methods, ``os.putenv``/``os.unsetenv``) on either environ spelling leaks
    state into every test that runs afterwards unless the function also
    requests the ``monkeypatch`` fixture, whose teardown restores the value
    automatically. A mutation without that guard makes the suite
    order-dependent: the test can pass alone and silently corrupt a sibling
    (or be corrupted by one) in the full run."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _environ_mutation_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} direct os.environ mutation(s) without monkeypatch.\n"
        "Unrestored environment changes leak into every later test and make the suite\n"
        "order-dependent. Use monkeypatch.setenv()/delenv() to get automatic teardown.\n" + "\n".join(violations)
    )


def test_environ_mutation_lens_flags_unguarded_mutations():
    """Synthetic positive/negative control for the environ-mutation lens: it
    must flag every mutation spelling in a function that does not request
    ``monkeypatch`` and ignore the same spellings inside a ``monkeypatch``
    function, reads from the mapping, other ``os`` calls, module-level
    definitions, and unrelated statements."""
    positive_sources = [
        "def test_foo():\n    os.environ['K'] = 'v'\n",
        "def test_foo():\n    del os.environ['K']\n",
        "def test_foo():\n    os.environ.pop('K')\n",
        "def test_foo():\n    os.environ.update({'K': 'v'})\n",
        "def test_foo():\n    environ.setdefault('K', 'v')\n",
        "def test_foo():\n    os.environ.clear()\n",
        "def test_foo():\n    os.putenv('K', 'v')\n",
        "def test_foo():\n    os.unsetenv('K')\n",
        "def test_foo():\n    environ['K'] = 'v'\n",
        "def test_foo():\n    os.environ['K'] = value\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _environ_mutation_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo(monkeypatch):\n    os.environ['K'] = 'v'\n",
        "def test_foo(monkeypatch):\n    del os.environ['K']\n",
        "def test_foo(monkeypatch):\n    os.environ.pop('K')\n",
        "def test_foo(monkeypatch):\n    os.putenv('K', 'v')\n",
        "def test_foo():\n    assert os.environ['K'] == 'v'\n",
        "def test_foo():\n    assert os.getenv('K')\n",
        "def test_foo():\n    os.makedirs('/tmp/x')\n",
        "def test_foo():\n    shutil.rmtree(d)\n",
        "def test_foo():\n    os.environ\n",
        "def test_foo():\n    return os.path.join('a', 'b')\n",
        "import os\nX = os.environ.copy()\n",
        "def test_foo():\n    os.environ = {}\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _environ_mutation_violations(tree), f"lens should NOT flag:\n{source}"


def _random_seed_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every reseed of the process-global
    random generator made without the ``monkeypatch`` fixture in scope.

    Seeding resets the module-global ``random.Random`` singleton that every
    test shares, changing the sequence that every later test calling ``random.*``
    observes, so the suite becomes order-dependent: a test that guards on a
    drawn random value can pass alone and silently change (or be changed by) a
    sibling in the full run. ``random.seed`` is also almost always pointless —
    the determinism it provides is rarely the point of an assertion — and the
    blessed deterministic form is to inject a dedicated ``random.Random(N)``
    instance so nothing global is touched; a function that requests
    ``monkeypatch`` is trusted because it can restore the prior generator at
    teardown. The recognised spellings are ``random.seed(...)`` (the
    ``import random`` attribute form) and the bare ``seed(...)`` name (the
    ``from random import seed`` twin). Only reseeds *inside a function body*
    are flagged: a module-level ``random.seed(N)`` bootstrap that pins the
    generator once at import time is idempotent setup, not between-test
    leakage. ``numpy.random.seed`` (a separate generator namespace) and reads
    like ``random.randrange``/``random.uniform`` are deliberately out of scope.
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
                f"{ast.unparse(node)} in {fn.name} reseeds the global random generator without monkeypatch ({kind})",
            )
        )

    for fn in functions:
        guarded = any(arg.arg == "monkeypatch" for arg in fn.args.args)
        pending = list(fn.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not guarded and isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "seed":
                    receiver = func.value
                    if isinstance(receiver, ast.Name) and receiver.id == "random":
                        _record(node, fn, "random.seed()")
                elif isinstance(func, ast.Name) and func.id == "seed":
                    _record(node, fn, "bare seed()")
            pending.extend(ast.iter_child_nodes(node))
    return found


def _unreachable_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` that is dead
    because an earlier statement at the same body level unconditionally
    ``return``s or ``raise``s before it.

    The no-op lens cannot see these: the body *contains* an assert, so it
    counts as verifying, yet the assert can never execute. Only direct
    statement-sequence reachability is checked — an ``assert`` whose
    reachability depends on a preceding branch (``if cond: return`` /
    ``try: return ... except: pass``) stays live and is left alone.
    """
    found: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        ancestor = False
        for stmt in fn.body:
            if ancestor:
                if isinstance(stmt, ast.Assert):
                    found.append(
                        (
                            stmt.lineno,
                            f"assert {ast.unparse(stmt.test)} in {fn.name} is unreachable — "
                            "preceded by an unconditional return/raise in the same body",
                        )
                    )
            elif isinstance(stmt, (ast.Return, ast.Raise)):
                ancestor = True
    return found


def test_no_unreachable_assert_after_unconditional_exit():
    """An ``assert`` that sits after an unconditional ``return``/``raise`` at
    the same body level is permanent dead code: it can never execute, so the
    test reports green no matter how broken the guarded behaviour is. The no-op
    lens is blind here because the body *contains* an assert; pytest collects
    it fine; a reader believes the behaviour is verified. Reachability only
    through a preceding branch is the legitimate early-exit idiom and is left
    alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unreachable_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unreachable assertion(s) after an unconditional return/raise.\n"
        "The assert can never run, so it verifies nothing. Move it before the exit, or delete it.\n"
        + "\n".join(violations)
    )


def test_unreachable_assert_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the unreachable-assert lens: it
    must flag an ``assert`` that follows an unconditional top-level
    ``return``/``raise`` in the same body and ignore early-exit-via-branch
    idioms, asserts reached through a ``try``/``if``, module-level code, and
    asserts in a different function from the exit."""
    positive_sources = [
        "def test_foo():\n    return value\n    assert x == 1\n",
        "def test_foo():\n    raise RuntimeError('boom')\n    assert x\n",
        "def test_foo():\n    assert a\n    return\n    assert b\n",
        "async def test_foo():\n    return None\n    assert isinstance(x, int)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unreachable_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    if cond:\n        return\n    assert x == 1\n",
        "def test_foo():\n    try:\n        return a\n    except Exception:\n        pass\n    assert x == 1\n",
        "def test_foo():\n    return value\n",
        "def test_foo():\n    assert x\n    return\n",
        "def test_foo():\n    for x in xs:\n        return x\n    assert not xs\n",
        "def test_foo():\n    return x\n\ndef test_bar():\n    assert z\n",
        "assert x\n\nclass C:\n    def test_c(self):\n        return\n\nassert y\n",
        "def test_foo():\n    if cond:\n        return\n    else:\n        assert x == 1\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unreachable_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _chdir_reference(node: ast.AST) -> bool:
    """Return True for the ``os.chdir`` spelling — ``import os`` + the
    attribute path ``os.chdir(...)`` (and the ``os.fchdir`` twin)."""
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return node.value.id == "os" and node.attr in ("chdir", "fchdir")


def _cwd_mutation_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every direct working-directory
    mutation made without the ``monkeypatch`` fixture in scope.

    A test that changes the process working directory and never restores it
    leaks that directory into every test that runs afterwards, so the suite
    becomes order-dependent: a sibling that (say) resolves a config template
    relative to ``os.getcwd()``, or writes into ``Path.cwd()``, behaves
    differently after the offender runs. ``monkeypatch.chdir(tmp_path)``
    restores the original directory at teardown automatically and is the
    pytest-blessed form — a function that requests ``monkeypatch`` is left
    alone even when it changes the directory directly. The recognised
    mutation spellings are ``os.chdir(path)``/``os.fchdir(fd)`` (the
    ``import os`` attribute form), the bare ``chdir(path)`` name (the
    ``from os import chdir`` twin), and the ``Path.chdir()`` method. Reads of
    the working directory (``os.getcwd()``, ``Path.cwd()``) and directory
    *creation* calls (``os.makedirs``/``os.mkdir``/``Path.mkdir``) never
    change the process CWD and are deliberately left alone. Only mutations
    *inside a function body* are flagged: a module-level ``os.chdir(...)``
    bootstrap that pins the process CWD once at import time is idempotent
    setup, not between-test leakage.
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
                f"{ast.unparse(node)} in {fn.name} changes the working directory without monkeypatch ({kind})",
            )
        )

    for fn in functions:
        guarded = any(arg.arg == "monkeypatch" for arg in fn.args.args)
        pending = list(fn.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not guarded and isinstance(node, ast.Call):
                func = node.func
                if _chdir_reference(func):
                    _record(node, fn, f"os.{func.attr}()")
                elif isinstance(func, ast.Name) and func.id == "chdir":
                    _record(node, fn, "bare chdir()")
                elif isinstance(func, ast.Attribute) and func.attr == "chdir":
                    receiver = func.value
                    if not (isinstance(receiver, ast.Name) and receiver.id == "monkeypatch"):
                        _record(node, fn, "Path.chdir()")
            pending.extend(ast.iter_child_nodes(node))
    return found


def test_no_global_random_reseed_without_monkeypatch():
    """Reseeding the process-global random generator resets the module-level
    ``random.Random`` singleton that every test shares, so the suite becomes
    order-dependent: a test that guards on a drawn random value can pass alone
    and silently change the results a sibling observes (or be changed by it) in
    the full run. ``random.seed`` is also almost always pointless — the
    determinism it confers rarely bears on the assertion it precedes. This
    guards both the ``random.seed(...)`` attribute spelling and the bare
    ``seed(...)`` name. A function that requests ``monkeypatch`` is trusted
    (it can restore the prior generator at teardown), the module-level
    ``random.seed(N)`` bootstrap is left alone, and ``numpy.random.seed`` (a
    separate generator namespace) is out of scope."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _random_seed_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} global random-generator reseed(s) made without monkeypatch.\n"
        "A direct random.seed never restores the prior generator, so it leaks state into every\n"
        "later test in the run — the suite becomes order-dependent and fails at the wrong\n"
        "test. Inject a dedicated random.Random(N) instance instead of touching the global, or\n"
        "request monkeypatch so the prior generator is restored at teardown.\n" + "\n".join(violations)
    )


def test_random_reseed_lens_flags_unguarded_reseeds():
    """Synthetic positive/negative control for the global-random-reseed lens: it
    must flag every reseed spelling (``random.seed`` and the bare ``seed()``
    name) when the enclosing function does not request ``monkeypatch``, and
    ignore reads of the generator, reseeds inside a ``monkeypatch``-guarded
    function, nested helpers that *are* guarded, module-level bootstraps, and
    unrelated calls."""
    positive_sources = [
        "def test_foo():\n    random.seed(42)\n",
        "def test_foo():\n    import random\n    random.seed(42)\n",
        "def test_foo():\n    seed(42)\n",
        "def test_foo():\n    from random import seed\n    seed(42)\n",
        "def test_foo():\n    def inner():\n        random.seed(42)\n",
        "def test_foo():\n    random.seed()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _random_seed_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo(monkeypatch):\n    random.seed(42)\n",
        "def test_foo():\n    rng = random.Random(42)\n    rng.seed(7)\n",
        "def test_foo():\n    numpy.random.seed(42)\n",
        "def test_foo():\n    np.random.seed(42)\n",
        "def test_foo():\n    x = random.uniform(0, 1)\n",
        "def test_foo():\n    x = random.randrange(10)\n",
        "def test_foo():\n    x = random.random()\n",
        "def test_foo():\n    cfg.seed(42)\n",
        "def test_foo():\n    obj.seed = 42\n",
        "def test_foo(monkeypatch):\n    def inner(monkeypatch):\n        random.seed(42)\n",
        "import random\nrandom.seed(42)\n",
        "def test_foo():\n    random.Random(42)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _random_seed_violations(tree), f"lens should NOT flag:\n{source}"


def test_no_cwd_mutation_without_monkeypatch():
    """A test that changes the process working directory and never restores it
    leaks that directory into every later test in the same run, so the suite
    becomes order-dependent: a test can pass on its own and silently corrupt a
    sibling that (say) resolves config relative to ``os.getcwd()`` or writes
    into ``Path.cwd()``. ``monkeypatch.chdir(tmp_path)`` restores the original
    directory at teardown automatically and is the pytest-blessed form, so a
    function that requests ``monkeypatch`` is trusted even when it changes the
    directory directly. This guards the ``os.chdir``/``os.fchdir`` attribute
    spellings, the bare ``chdir(...)`` name, and ``Path.chdir()``. Reads of
    the working directory (``os.getcwd()``/``Path.cwd()``) and directory
    *creation* calls (``os.makedirs``/``os.mkdir``) are deliberately left
    alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _cwd_mutation_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} working-directory mutation(s) made without monkeypatch.\n"
        "A direct os.chdir never restores the prior directory, so it leaks state into every\n"
        "later test in the run — the suite becomes order-dependent and fails at the wrong\n"
        "test. Use monkeypatch.chdir(tmp_path), which restores at teardown.\n" + "\n".join(violations)
    )


def test_cwd_mutation_lens_flags_unguarded_mutations():
    """Synthetic positive/negative control for the cwd-mutation lens: it must
    flag every direct spelling (``os.chdir``/``os.fchdir``, the bare
    ``chdir(...)`` name, and ``Path.chdir()``) when the enclosing function does
    not request ``monkeypatch``, and ignore reads of the working directory,
    directory *creation* calls, ``monkeypatch.chdir`` (the blessed form),
    mutations inside a ``monkeypatch``-guarded function, nested helpers that
    *are* guarded, and unrelated calls."""
    positive_sources = [
        "def test_foo():\n    os.chdir(tmp_path)\n",
        "def test_foo():\n    import os\n    os.chdir('/tmp/x')\n",
        "def test_foo():\n    os.fchdir(fd)\n",
        "def test_foo():\n    chdir(tmp_path)\n",
        "def test_foo():\n    from os import chdir\n    chdir('/tmp/x')\n",
        "def test_foo():\n    Path.cwd().chdir()\n",
        "def test_foo():\n    from pathlib import Path\n    Path('/tmp/x').chdir()\n",
        "def test_foo():\n    def inner():\n        os.chdir(tmp_path)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _cwd_mutation_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo(monkeypatch):\n    os.chdir(tmp_path)\n",
        "def test_foo():\n    monkeypatch.chdir(tmp_path)\n",
        "def test_foo():\n    cwd = os.getcwd()\n",
        "def test_foo():\n    cwd = Path.cwd()\n",
        "def test_foo():\n    os.makedirs('/tmp/x')\n",
        "def test_foo():\n    os.mkdir('/tmp/x')\n",
        "def test_foo():\n    Path('/tmp/x').mkdir()\n",
        "def test_foo():\n    p = Path('/tmp/x')\n    p.chdir = fake\n",
        "def test_foo():\n    os.path.join('a', 'b')\n",
        "def test_foo():\n    chdir_service.run()\n",
        "def test_foo(monkeypatch):\n    def inner(monkeypatch):\n        os.chdir(tmp_path)\n",
        "def test_foo():\n    fd = os.open('/tmp/x', os.O_RDONLY)\n    os.close(fd)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _cwd_mutation_violations(tree), f"lens should NOT flag:\n{source}"


def _container_literal_truthiness(expr: ast.AST) -> str | None:
    """Return ``"always falsy"``/``"always truthy"`` for a container expression
    whose truthiness is fixed at source time, or ``None`` when it depends on
    runtime values.

    A list/dict/set/tuple *literal* is truthy exactly when it is non-empty, so
    empty literals (``[]``/``{}``/``()``) are always falsy and non-empty
    literals are always truthy no matter what their elements evaluate to. A
    zero-argument builtin container call (``list()``/``dict()``/``set()``/
    ``tuple()``/``bytes()``/``bytearray()``/``frozenset()`` — ``_EMPTY_BUILTIN_CALLS``)
    always returns an empty, falsy container. ``*``-unpacked sequences
    (``[*a]``/``(*a,)``/``{*a}``) and ``**``-spread dicts (``{**mapping}``)
    return ``None`` because their arity — and therefore their truthiness — is
    runtime-dependent; a dict with at least one literal key is always truthy
    even when a ``**`` spread joins it. Comprehensions (``ast.ListComp`` etc.)
    are never passed in here because they are their own node types and depend on
    the iterable at runtime."""
    if isinstance(expr, ast.Dict):
        if not expr.keys:
            return "always falsy"
        if any(key is not None for key in expr.keys):
            return "always truthy"
        return None
    if isinstance(expr, (ast.List, ast.Set, ast.Tuple)):
        if any(isinstance(elt, ast.Starred) for elt in expr.elts):
            return None
        return "always falsy" if not expr.elts else "always truthy"
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in _EMPTY_BUILTIN_CALLS
        and not expr.args
        and not expr.keywords
    ):
        return "always falsy"
    return None


def _dead_container_literal_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose *entire*
    test expression is a container literal or zero-argument empty-container
    builtin call with statically-fixed truthiness.

    ``assert []`` / ``assert not []``, ``assert [1]`` / ``assert not [1]``,
    ``assert list()`` / ``assert not set()``, ... — the outcome is decided by
    the literal's arity alone, so the assert either always passes (a silent
    false green that a mutation-testing run believes verifies behaviour) or
    always fails (unconditionally red) no matter what the code under test does.
    This is the direct-test-position twin of the empty-container *equality*
    lens: ``assert x == []`` is already flagged, but the container standing
    alone as the assertion (``assert []`` after a value was replaced by a
    literal while debugging, ``assert [1]`` left over from a hard-coded
    fixture) has an ``ast.List``-shaped test that the literal-constant lens
    provably misses. Only the whole test expression (optionally ``not``-wrapped)
    is flagged: a container that is an *operand* of a comparison or ``in`` is
    the responsibility of the equality/membership lenses, and a compound
    boolean test (``assert [] and x``) that short-circuits on the literal is
    deliberately left to the compound-assertion lens to reason about."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        candidate = test.operand if negated else test
        verdict = _container_literal_truthiness(candidate)
        if verdict is None:
            continue
        falsy = verdict == "always falsy"
        outcome = "always FAILS" if falsy != negated else "always PASSES"
        if isinstance(candidate, ast.Call):
            kind = "a zero-argument builtin call that always returns an empty container"
        else:
            kind = "an empty container literal" if falsy else "a non-empty container literal"
        found.append(
            (
                node.lineno,
                f"{ast.unparse(candidate)} — {outcome}: {kind} has fixed truthiness, so the "
                "outcome is decided at source time, never by the code under test",
            )
        )
    return found


def test_no_dead_container_literal_asserts():
    """An ``assert`` whose entire test expression is a container literal (or a
    zero-argument empty-container builtin call) is dead code: an empty
    container is always falsy and a non-empty one is always truthy, so the
    assert always passes (reporting green no matter how broken the code under
    test is) or always fails (breaking the suite unconditionally) — the outcome
    never depends on the behaviour under test. These are almost always leftover
    debugging where a value under test was replaced by a hard-coded fixture
    literal. Comprehensions, ``*args``/``**kwargs``-unpacked literals, and
    containers used as comparison/membership operands are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _dead_container_literal_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a container with statically-fixed truthiness.\n"
        "An empty container literal/builtin call is always falsy and a non-empty one is always\n"
        "truthy, so the assert either always passes (dead green) or always fails (unconditionally\n"
        "red), never depending on the code under test. Assert against the value the expression\n"
        "contains, or bind it to a name that reflects what is actually being verified.\n" + "\n".join(violations)
    )


def test_dead_container_literal_lens_flags_fixed_truthiness():
    """Synthetic positive/negative control for the dead-container-literal lens:
    it must flag every container literal and zero-argument empty-container
    builtin call standing alone as the assertion (direct or ``not``-wrapped,
    empty or non-empty) and ignore comparisons, comprehensions, ``*``/``**``-
    unpacked literals, and anything whose truthiness depends on runtime
    values."""
    positive_sources = [
        "def test_foo():\n    assert []\n",
        "def test_foo():\n    assert {}\n",
        "def test_foo():\n    assert ()\n",
        "def test_foo():\n    assert not []\n",
        "def test_foo():\n    assert not {}\n",
        "def test_foo():\n    assert [1, 2]\n",
        "def test_foo():\n    assert {'k': 'v'}\n",
        "def test_foo():\n    assert ('x',)\n",
        "def test_foo():\n    assert {1, 2}\n",
        "def test_foo():\n    assert not [1]\n",
        "def test_foo():\n    assert list()\n",
        "def test_foo():\n    assert dict()\n",
        "def test_foo():\n    assert set()\n",
        "def test_foo():\n    assert tuple()\n",
        "def test_foo():\n    assert bytes()\n",
        "def test_foo():\n    assert bytearray()\n",
        "def test_foo():\n    assert frozenset()\n",
        "def test_foo():\n    assert not set()\n",
        "def test_foo():\n    assert {'x': 1, **mapping}\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _dead_container_literal_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert items\n",
        "def test_foo():\n    assert not items\n",
        "def test_foo():\n    assert result == []\n",
        "def test_foo():\n    assert x != {}\n",
        "def test_foo():\n    assert x == list()\n",
        "def test_foo():\n    assert x in [1, 2]\n",
        "def test_foo():\n    assert x not in set()\n",
        "def test_foo():\n    assert [a for a in items]\n",
        "def test_foo():\n    assert {k: v for k, v in pairs}\n",
        "def test_foo():\n    assert {x for x in items}\n",
        "def test_foo():\n    assert {*values}\n",
        "def test_foo():\n    assert [*items]\n",
        "def test_foo():\n    assert (*items,)\n",
        "def test_foo():\n    assert {**mapping}\n",
        "def test_foo():\n    assert not [*items]\n",
        "def test_foo():\n    assert list(items)\n",
        "def test_foo():\n    assert not set(items)\n",
        "def test_foo():\n    assert len(items)\n",
        "def test_foo():\n    assert () == x\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _dead_container_literal_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _blocking_sleep_in_async_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``time.sleep(...)`` call
    inside an ``async def`` body.

    An ``async`` test or fixture that calls the *blocking* ``time.sleep``
    freezes the entire event loop for the duration: no other coroutine on that
    loop — concurrent tasks, teardowns, the loop itself — can make progress
    while the thread sleeps, so the call is a real wall-clock stall rather than
    the cooperative yield ``await asyncio.sleep(...)`` provides. The default
    pytest-asyncio loop is a fresh event loop per test, so a blocking sleep even
    starves nothing visible — the hazard appears when the sleep carries a
    real duration (each second is a second of CI) or when the async body shares
    a loop with concurrent work. Only the ``import time`` attribute spelling
    (``time.sleep(...)``) is matched: the ``from time import sleep`` bare-name
    twin cannot be distinguished statically from a local ``sleep`` helper. A
    ``time.sleep`` inside a *nested* plain ``def`` within the async body is
    still flagged: the nested helper is awaited on the same loop, so its
    blocking sleep freezes that loop just like an inline call."""
    found: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
            ):
                found.append(
                    (
                        node.lineno,
                        f"{ast.unparse(node)} in async def {fn.name} — time.sleep() blocks the event "
                        "loop; use 'await asyncio.sleep(...)' so the loop can interleave other work",
                    )
                )
    return found


def test_no_blocking_sleep_in_async_body():
    """``time.sleep(...)`` inside an ``async def`` blocks the whole event loop
    for the full duration instead of yielding control the way
    ``await asyncio.sleep(...)`` does. Every second of a literal sleep is a
    real second of wall-clock CI, and a concurrent coroutine on the same loop
    (a teardown, a background task) is starved for the whole wait even when the
    duration is tiny. This is the blocking-spelling twin of the
    computed-wall-clock-sleep lens. The ``from time import sleep`` bare-name
    spelling and calls inside plain ``def`` functions are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _blocking_sleep_in_async_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} blocking time.sleep() call(s) inside async test bodies.\n"
        "time.sleep() blocks the entire event loop, starving every other coroutine on it\n"
        "for the full duration; use 'await asyncio.sleep(...)' which yields control back\n"
        "to the loop instead of hogging it.\n" + "\n".join(violations)
    )


def test_blocking_sleep_lens_flags_loop_blockers():
    """Synthetic positive/negative control for the blocking-sleep lens: it must
    flag every ``time.sleep(...)`` call anywhere inside an ``async def`` and
    ignore the same call inside plain ``def`` functions, ``asyncio.sleep``
    (the prescribed fix), and unrelated ``.sleep`` attribute receivers."""
    positive_sources = [
        "async def test_foo():\n    time.sleep(0.1)\n",
        "async def test_foo():\n    await load()\n    time.sleep(1)\n",
        "async def test_foo():\n    def inner():\n        time.sleep(0.05)\n    await inner()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _blocking_sleep_in_async_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    time.sleep(0.1)\n",
        "async def test_foo():\n    await asyncio.sleep(0.1)\n",
        "async def test_foo():\n    await asyncio.sleep(delay)\n",
        "async def test_foo():\n    await some_sleep()\n",
        "async def test_foo():\n    Mock.sleep(1)\n",
        "async def test_foo():\n    fake.sleep(0)\n",
        "def test_foo():\n    async def inner():\n        await asyncio.sleep(1)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _blocking_sleep_in_async_violations(tree), f"lens should NOT flag:\n{source}"


_FRESH_VALUE_NAMES = frozenset({"uuid4", "uuid1", "uuid3", "uuid5", "token_hex", "token_bytes", "token_urlsafe"})
#: Bare names (``from uuid import uuid4``, ``from secrets import token_hex``)
#: that always evaluate to a freshly-generated, unique-per-call value. The
#: ``uuid3``/``uuid5`` name-based siblings are included: their inputs are
#: deterministic given the same (namespace, name) pair, but a *fresh* call in
#: an assert can still never equal a value produced independently by the code
#: under test with a different name/namespace.


def _is_fresh_value_call(node: ast.AST) -> bool:
    """Return True when ``node`` is a call expression that produces a *fresh*
    non-deterministic value on every evaluation: a UUID or secrets token, a
    wall-clock read, or a naive ``datetime.now()``.

    The recognised spellings are the bare UUID/token names (``uuid4()``,
    ``token_hex()`` — the ``from ... import`` twin), the attribute path on the
    ``uuid``/``secrets`` modules (``uuid.uuid4()``, ``secrets.token_hex()``),
    the ``time.*`` wall-clock reads in either spelling (``time.time()`` /
    ``time.monotonic()`` / ``time.perf_counter()`` / ``time.process_time()``
    and their ``_ns`` twins), and ``datetime.now()`` / ``datetime.utcnow()``
    (``from datetime import datetime`` or the ``import datetime`` twin).
    Subsystem-qualified methods (``clock.now()``, ``ticker.time()``) are left
    alone — only the unambiguous standard-module spellings are matched, so the
    lens has no false positives from legitimate injected clocks or mocks."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _FRESH_VALUE_NAMES
    if not isinstance(func, ast.Attribute):
        return False
    attr = func.attr
    if attr in _FRESH_VALUE_NAMES:
        return True
    if attr in {"now", "utcnow"}:
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id == "datetime":
            return True
        if (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "datetime"
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id == "datetime"
        ):
            return True
    return (
        attr
        in {
            "time",
            "time_ns",
            "monotonic",
            "monotonic_ns",
            "perf_counter",
            "perf_counter_ns",
            "process_time",
            "process_time_ns",
            "clock",
        }
        and isinstance(func.value, ast.Name)
        and func.value.id == "time"
    )


#: Name-based UUIDs that are *deterministic*: ``uuid3``/``uuid5`` hash their
#: inputs, so they return the SAME value on every call. They must never drive an
#: "always FAILS/PASSES" membership/equality verdict (such an assertion is
#: satisfiable), even though they are still freshly-constructed objects for the
#: direct truthy-assert purpose.
_DETERMINISTIC_UUID_NAMES = frozenset({"uuid3", "uuid5"})


def _is_non_deterministic_fresh_call(node: ast.AST) -> bool:
    """Like :func:`_is_fresh_value_call` but excludes deterministic name-based
    UUIDs (``uuid3``/``uuid5``), which return the SAME value on every call.

    A membership/equality verdict built on a deterministic UUID is satisfiable
    (not always-failing), so it must not be flagged as a dead always-FAILS/always-
    PASSES assertion. The direct truthy-assert lens keeps treating ``uuid3``/
    ``uuid5`` as fresh (a freshly built UUID object is still a silent false
    green), but the container-nested and membership lenses use this stricter
    check instead."""
    if not _is_fresh_value_call(node):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    return name not in _DETERMINISTIC_UUID_NAMES


def _walk_operand(node: ast.AST):
    """Yield ``node`` and its descendants, but never descend into ``Call``,
    ``Subscript``, or comprehension (``ListComp``/``SetComp``/``DictComp``/
    ``GeneratorExp``) boundaries.

    This keeps a comparison-operand search scoped to the operand's OWN container
    literals: a fresh value hidden behind a ``call(...)`` wrapper, a subscript,
    or a comprehension is left alone, exactly as the container lens documents
    its own contract — so ``assert load({'id': uuid.uuid4()}) == expected`` is
    NOT flagged (the fresh value is an argument to ``load``, not the compared
    container itself)."""
    yield node
    if isinstance(node, (ast.Call, ast.Subscript, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return
    for child in ast.iter_child_nodes(node):
        yield from _walk_operand(child)


def _fresh_value_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression — or a single equality-comparison operand — is a freshly
    generated non-deterministic value.

    Such an assertion is dead code with a fixed outcome, exactly like the
    Mock-constructor family: every call returns a *fresh* value unique to that
    evaluation, so ``assert uuid.uuid4()`` (a UUID is always truthy) is a
    silent false green, ``assert not uuid.uuid4()`` can never pass,
    ``assert result == uuid.uuid4()`` ALWAYS FAILS (the freshly minted value
    can never equal the one the code under test produced and stored), and the
    ``!=`` twin ALWAYS PASSES. These are almost always a broken attempt to
    compare the code's output against a value generated by the test *itself*;
    the fix is to capture the generated value in a variable first (passing it
    into the code under test or the mock), then assert against that same bound
    name. Ordering comparisons (``assert t < time.time()``) are deliberate
    bounds checks and are not flagged, and equality against a name bound to a
    call earlier in the test never appears as a constructor call in the assert
    at all."""
    found: list[tuple[int, str]] = []

    def _report(lineno: int, detail: str) -> None:
        found.append(
            (
                lineno,
                f"assert {detail} — a fresh non-deterministic value is regenerated on every "
                "evaluation, so the outcome is fixed at source time",
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if _is_fresh_value_call(test):
            _report(node.lineno, ast.unparse(test))
            continue
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _is_fresh_value_call(test.operand):
            _report(node.lineno, ast.unparse(test))
            continue
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
            for operand in (test.left, test.comparators[0]):
                if _is_fresh_value_call(operand):
                    _report(node.lineno, ast.unparse(test))
                    break
    return found


def test_no_fresh_value_in_asserts():
    """An ``assert`` that generates a fresh non-deterministic value directly in
    its test expression is dead code with a fixed outcome: every UUID, secrets
    token, ``time.time()`` read, and ``datetime.now()`` call returns a value
    unique to that evaluation, so the assertion can never depend on the
    behaviour under test. ``assert uuid.uuid4()`` (a UUID is always truthy) is
    a silent false green, ``assert not uuid.uuid4()`` can never pass,
    ``assert result == uuid.uuid4()`` always fails (the newly-minted value can
    never equal the one the code under test produced), and the ``!=`` twins
    always pass. These are the non-deterministic twin of the Mock-constructor
    lenses and are almost always a broken attempt to compare code output
    against a value the test itself generated instead of passing that captured
    value into the code under test. Capture the generated value in a variable
    first and assert against the same bound name."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _fresh_value_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a freshly generated non-deterministic value.\n"
        "A UUID/token/time call returns a value unique to that single evaluation, so the outcome of\n"
        "the assertion is fixed at source time, never by the code under test. Capture the generated\n"
        "value in a variable first, feed it into the code under test, and assert against that name.\n"
        + "\n".join(violations)
    )


def test_fresh_value_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the fresh-value lens: it must
    flag an ``assert`` that generates a non-deterministic value directly in the
    test expression (bare, ``not``-wrapped, or as an equality-comparison
    operand, in any recognised spelling) and ignore ordering bounds checks,
    subsystem-qualified time methods, comparisons against already-bound names,
    and asserts over ordinary values."""
    positive_sources = [
        "def test_foo():\n    assert uuid.uuid4()\n",
        "def test_foo():\n    assert uuid4()\n",
        "def test_foo():\n    assert uuid.uuid1()\n",
        "def test_foo():\n    assert uuid.uuid5(uuid.NAMESPACE_DNS, 'x')\n",
        "def test_foo():\n    assert not uuid.uuid4()\n",
        "def test_foo():\n    assert secrets.token_hex()\n",
        "def test_foo():\n    assert token_urlsafe()\n",
        "def test_foo():\n    assert secrets.token_bytes(16)\n",
        "def test_foo():\n    assert result == uuid.uuid4()\n",
        "def test_foo():\n    assert result != uuid.uuid4()\n",
        "def test_foo():\n    assert record['created'] == time.time()\n",
        "def test_foo():\n    assert elapsed == time.monotonic()\n",
        "def test_foo():\n    assert now == datetime.now()\n",
        "def test_foo():\n    assert now == datetime.datetime.now()\n",
        "def test_foo():\n    assert ts == datetime.utcnow()\n",
        "def test_foo():\n    assert x == time.perf_counter()\n",
        "def test_foo():\n    assert uuid.uuid4() == other\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _fresh_value_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    uid = uuid.uuid4()\n    assert uid\n",
        "def test_foo():\n    uid = uuid.uuid4()\n    assert result == uid\n",
        "def test_foo():\n    assert result == generated_id\n",
        "def test_foo():\n    assert t < time.time()\n",
        "def test_foo():\n    assert t >= time.monotonic()\n",
        "def test_foo():\n    assert clock.now() > t\n",
        "def test_foo():\n    assert ticker.time() < 5\n",
        "def test_foo():\n    assert service.uuid4() is None\n",
        "def test_foo():\n    assert x == 1\n",
        "def test_foo():\n    assert x == uuid.UUID(int=1)\n",
        "def test_foo():\n    assert len(items) == 0\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _fresh_value_assert_violations(tree), f"lens should NOT flag:\n{source}"


_SKIP_XFAIL_NAMES = {"skip", "xfail"}


def _skip_xfail_call(call: ast.Call) -> str | None:
    """Return ``"skip"``/``"xfail"`` when ``call`` is a skip/xfail invocation in
    either spelling — ``pytest.skip(...)``/``pytest.xfail(...)`` (attribute) or
    the bare imported ``skip(...)``/``xfail(...)`` name. ``skipif`` is
    deliberately excluded: it is inherently conditional (it takes a condition
    argument), whereas ``skip``/``xfail`` deselect unconditionally when called
    unconditionally."""
    func = call.func
    name = _callable_name(func)
    return name if name in _SKIP_XFAIL_NAMES else None


def _unconditional_body_skip_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every unconditional skip/xfail in a
    test body.

    ``pytest.skip(reason)``/``pytest.xfail(reason)`` (or the bare imported
    ``skip(...)``/``xfail(...)``) placed as a *direct statement of the test body*
    permanently deselects the test from every run: the call always executes, so
    whatever follows never runs and the test never verifies anything. It is
    indistinguishable in source from a runtime gate, yet it is not gated — there
    is no surrounding ``if``/loop to make the deselection conditional, and no
    marker on the function to make it reviewable. A reader — and a
    mutation-testing run — believes the test participates when it silently does
    not: the coverage loss is identical to deleting the test, but reported green.

    The marker twins are owned elsewhere — ``test_no_skip_without_reason``
    catches ``@skip``/``@xfail``/``@skipif`` and body ``skip()`` calls that carry
    *no* reason, and ``test_no_constant_condition_skips`` catches statically
    foldable ``@skipif(True, ...)`` conditions. A body skip that *does* carry a
    reason slips past both, which makes this the sneaky statement form: it reads
    like real logic but is unconditional. A skip nested under an explicit
    ``if``/loop ``try``/``with`` block is a genuine runtime gate and is left
    alone — only the direct, unconditional top-level statement is flagged.
    """
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_decorator_name(d) == "fixture" for d in fn.decorator_list):
            continue
        if not (fn.name.startswith("test_") or any(_is_mark_decorator(d) for d in fn.decorator_list)):
            continue
        for stmt in fn.body:
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
                continue
            name = _skip_xfail_call(stmt.value)
            if name is None:
                continue
            if stmt.value.args:
                reason = ast.unparse(stmt.value.args[0])
                found.append(
                    (
                        stmt.lineno,
                        f"{name}() called unconditionally on the test body ('{reason}') — "
                        f"the test ALWAYS {name}s, so the rest of the body never runs",
                    )
                )
            else:
                found.append(
                    (
                        stmt.lineno,
                        f"{name}() called unconditionally on the test body — "
                        f"the test ALWAYS {name}s, so the rest of the body never runs",
                    )
                )
    return found


def test_no_unconditional_body_skip():
    """A ``pytest.skip(reason)``/``pytest.xfail(reason)`` (or bare
    ``skip(...)``/``xfail(...)``) placed as a *direct statement* of the test
    body permanently deselects the test from every run — the call always
    executes, so whatever follows never runs and the test never verifies
    anything. It reads like a runtime gate but is not gated: there is no
    surrounding ``if``/loop, so the deselection is unconditional and
    unreviewable — the coverage loss is identical to deleting the test, but the
    suite still reports green. ``test_no_skip_without_reason`` and
    ``test_no_constant_condition_skips`` own the marker and reason-less forms;
    this lens owns the *statement* form that carries a reason and therefore
    slips past both. A skip nested under an explicit ``if``/loop (a real runtime
    gate) is left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unconditional_body_skip_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unconditional skip/xfail statement(s) in test bodies.\n"
        "A skip/xfail placed directly on the test body always executes, so the test is\n"
        "permanently deselected while still reporting green. Gate it behind an explicit\n"
        "condition, or drop the call so the test either runs or is removed.\n" + "\n".join(violations)
    )


def test_unconditional_body_skip_lens_flags_permanent_deselection():
    """Synthetic positive/negative control for the unconditional-body-skip lens:
    it must flag every direct top-level ``skip``/``xfail`` statement (in the
    attribute and bare-name spellings, sync and async, mid-body as well as
    single-statement), and ignore skips nested under an explicit ``if``/loop,
    ``skipif`` (inherently conditional), ``self.skipTest``, and unrelated
    calls."""
    positive_sources = [
        "def test_foo():\n    pytest.skip('not yet')\n",
        "def test_foo():\n    pytest.xfail('flaky')\n",
        "def test_foo():\n    skip('no')\n",
        "def test_foo():\n    from pytest import skip\n    skip('no')\n",
        "def test_foo():\n    x = do_work()\n    pytest.skip('abandoned')\n",
        "async def test_foo():\n    pytest.skip('no')\n",
        "def test_foo():\n    pytest.skip()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unconditional_body_skip_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    if not pkg:\n        pytest.skip('missing')\n",
        "def test_foo():\n    if sys.version_info < (3, 12):\n        pytest.xfail('old')\n",
        "def test_foo():\n    for x in items:\n        pytest.skip('x')\n",
        "def test_foo():\n    def inner():\n        pytest.skip('nested helper')\n    inner()\n",
        "def test_foo():\n    pytest.skipif(True, reason='always')\n",
        "def test_foo():\n    self.skipTest('x')\n",
        (
            "def test_foo():\n"
            "    try:\n"
            "        do_work()\n"
            "    except NotImplementedError:\n"
            "        pytest.xfail('not there')\n"
        ),
        "def test_foo():\n    skip_service.run()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unconditional_body_skip_violations(tree), f"lens should NOT flag:\n{source}"


def _unconditional_skip_marker_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every unconditional ``@skip`` marker.

    ``@pytest.mark.skip(...)`` (or the bare imported ``@skip`` decorator) on a
    test function/method, a class-level ``@pytest.mark.skip`` decorator (which
    permanently deselects every test defined in the class) — and the
    module-level ``pytestmark = pytest.mark.skip`` form that deselects every
    test in the module — permanently removes the item
    from every run: unlike ``skipif`` there is no condition argument to gate on,
    so the deselection is unconditional and the test reports green while its
    body never runs. The body-statement sibling goggles this class separately;
    this lens owns the *decorator* spelling, which neither
    ``test_no_skip_without_reason`` (only flags markers *without* ``reason=``)
    nor ``test_no_constant_condition_skips`` (only ``skipif``/``xfail`` with a
    foldable condition) can reach. ``@skipif`` (inherently conditional) and
    ``@xfail`` (a visible, reviewable known-failing pin whose XPASS shows up in
    the report) are deliberately left alone.
    """
    found = []

    def _marker_prefix(marker: ast.AST) -> str:
        """Return the dotted path *before* the terminal marker name, e.g.
        ``pytest.mark.`` for ``pytest.mark.skip`` and ``''`` for a bare imported
        ``skip`` (so the reported context reads ``@skip`` rather than the
        misleading ``@pytest.mark.skip``)."""
        node = marker.func if isinstance(marker, ast.Call) else marker
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        parts.reverse()
        # Drop the terminal name (the marker itself, e.g. ``skip``).
        if len(parts) >= 2:
            return ".".join(parts[:-1]) + "."
        return ""

    def _report(marker: ast.AST, prefix: str, lineno: int) -> None:
        # Reuse the module-level ``_decorator_name`` rather than re-deriving it.
        if _decorator_name(marker) != "skip":
            return
        reason = ""
        if isinstance(marker, ast.Call):
            if marker.args:
                reason = f" ('{ast.unparse(marker.args[0])}')"
            else:
                for kw in marker.keywords:
                    if kw.arg == "reason":
                        reason = f" ('{ast.unparse(kw.value)}')"
                        break
        found.append(
            (
                lineno,
                f"{prefix}skip marker with no condition{reason} — "
                "the test ALWAYS skips, so it never runs (replace with "
                "@pytest.mark.skipif(<real condition>, ...) or delete the marker)",
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(_decorator_name(d) == "fixture" for d in node.decorator_list):
                continue
            is_test = node.name.startswith("test_") or any(_is_mark_decorator(d) for d in node.decorator_list)
            if not is_test:
                continue
            for dec in node.decorator_list:
                _report(dec, f"@{_marker_prefix(dec)}", node.lineno)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            value = node.value
            # Unwrap list/tuple spellings (``pytestmark = [pytest.mark.skip(...)]``)
            # — otherwise a whole-module deselect slips past the lens.
            elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            for el in elements:
                _report(el, f"pytestmark = {_marker_prefix(el)}", node.lineno)
    return found


def test_no_unconditional_skip_markers():
    """An ``@pytest.mark.skip`` marker (or the bare imported ``@skip``
    decorator, or module-level ``pytestmark = pytest.mark.skip``) has no
    condition to gate on, so the decorated test is permanently removed from
    every run while still reporting green. It is the decorator twin of the
    unconditional body-skip statement — the ``skip-without-reason`` lens only
    flags markers that lack a ``reason=``, and ``constant-condition-skip`` only
    handles ``skipif``/``xfail`` with a foldable condition, so a ``skip`` marker
    carrying a reason slips past every sibling. The sanctioned spellings are
    ``@pytest.mark.skipif(<real condition>, ...)`` and the flaky-test
    quarantine registry; ``@xfail`` is deliberately left alone (it is the
    visible, reviewable known-failing pin)."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unconditional_skip_marker_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unconditional @skip marker(s).\n"
        "A plain @pytest.mark.skip has no condition to gate on, so the test is permanently\n"
        "deselected while still reporting green. Use @pytest.mark.skipif(<real condition>, ...)\n"
        "or delete the marker (the quarantine registry is the flaky-test alternative).\n" + "\n".join(violations)
    )


def test_unconditional_skip_marker_lens_flags_permanent_deselection():
    """Synthetic positive/negative control for the unconditional-skip-marker
    lens: it must flag the ``@pytest.mark.skip`` decorator (called and bare,
    with and without a reason, on sync and async tests, on methods, at the
    class level, and at module level via ``pytestmark =``) and ignore
    ``@skipif``, ``@xfail``, non-test helpers, fixtures, and unrelated marks."""
    positive_sources = [
        "@pytest.mark.skip\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skip()\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skip(reason='not implemented')\ndef test_foo():\n    assert x\n",
        "import pytest\n@skip\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skip(reason='flaky in CI')\nasync def test_foo():\n    await x\n",
        (
            "import pytest\n"
            "\n"
            "class TestFoo:\n"
            "    @pytest.mark.skip(reason='legacy')\n"
            "    def test_bar(self):\n"
            "        assert x\n"
        ),
        (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='legacy')\n"
            "class TestFoo:\n"
            "    def test_bar(self):\n"
            "        assert x\n"
        ),
        "pytestmark = pytest.mark.skip\ndef test_foo():\n    assert x\n",
        "pytestmark = pytest.mark.skip(reason='whole module dormant')\ndef test_foo():\n    assert x\n",
        "pytestmark = [pytest.mark.skip(reason='whole module dormant')]\ndef test_foo():\n    assert x\n",
        "pytestmark = (pytest.mark.skip(reason='whole module dormant'),)\ndef test_foo():\n    assert x\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unconditional_skip_marker_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "@pytest.mark.skipif(os.name == 'nt', reason='windows')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(True, reason='temp')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.xfail(reason='known bug')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.xfail(condition=True, reason='known bug')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.asyncio\nasync def test_foo():\n    await x\n",
        "@pytest.fixture\n@skip\ndef fixture_foo():\n    return x\n",
        "def test_foo():\n    if not pkg:\n        pytest.skip('missing')\n",
        "skip_service = Service()\ndef test_foo():\n    assert skip_service.run()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unconditional_skip_marker_violations(tree), f"lens should NOT flag:\n{source}"


def _empty_raises_context_body_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``with pytest.raises(...):`` /
    ``with pytest.warns(...):`` (or their ``async with`` twins, or a bare
    imported ``raises``/``warns``) whose body contains no executable
    statement — only ``pass``, ``...``, a docstring, or nothing at all.

    Such a context is an unfinished test: the expectation is declared but the
    code that is supposed to trigger it was never written, so the ``with``
    block exercises nothing.

    This is *not* a false green, and the distinction matters. Unlike the
    unentered-raises lens — where ``pytest.raises(X)`` stands as a bare
    statement, the context is never entered, and nothing is ever checked —
    pytest itself fails an entered-but-empty context loudly with
    ``Failed: DID NOT RAISE`` / ``Failed: DID NOT WARN`` the moment the test
    runs. The lens is therefore a dead-code/incompleteness check, not a
    false-green check.

    It still earns its place because the guaranteed failure only surfaces if
    the test actually executes. When it does not, the empty body is reported
    green and no ``DID NOT RAISE`` is ever seen:

    * behind ``@pytest.mark.skip``/``skipif`` — the body never runs (SKIPPED),
    * behind ``@pytest.mark.xfail`` — the failure is absorbed and reported as
      XFAIL, which reads as a pass,
    * inside a branch or helper that is never reached at runtime.

    Statically flagging the body also names the real defect ("the
    code-under-test is missing") instead of leaving a reader to decode a
    ``DID NOT RAISE`` at runtime. It complements the no-op-test-body lens,
    which only governs whole ``test_*`` functions rather than individual
    ``with`` blocks.
    """
    found: list[tuple[int, str]] = []

    def _body_is_empty(body: list[ast.stmt]) -> bool:
        statements = [
            s
            for s in body
            if not (
                isinstance(s, ast.Pass)
                or (
                    isinstance(s, ast.Expr)
                    and isinstance(s.value, ast.Constant)
                    and (isinstance(s.value.value, str) or s.value.value is Ellipsis)
                )
            )
        ]
        return not statements

    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)) or not node.items:
            continue
        with_item = node.items[0].context_expr
        lineno = node.lineno
        # Only a called context manager counts: a bare ``with pytest.raises:``
        # (no call) is not a raises context, so it must not be flagged.
        name = _callable_name(with_item) if isinstance(with_item, ast.Call) else None
        if name not in _RAISES_CONTEXT_FUNCS:
            continue
        if not _body_is_empty(node.body):
            continue
        found.append(
            (
                lineno,
                f"{name}(...) with an empty body — no code runs inside the with block, so the "
                "expected exception/warning is never exercised. pytest fails this with 'DID NOT "
                "RAISE'/'DID NOT WARN' whenever the test runs, and it is silently green when it "
                "does not (skip/xfail/unreached code); put the code-under-test inside the "
                "'with' body",
            )
        )
    return found


def test_no_empty_raises_context_bodies():
    """A ``pytest.raises``/``pytest.warns`` context (``with ...:`` or
    ``async with ...:``, attribute or bare-imported name) whose body contains
    no executable statement — only ``pass``, ``...``, a docstring, or nothing —
    is an unfinished test: the expectation is declared but the code meant to
    trigger it is missing.

    pytest fails such a context loudly (``DID NOT RAISE``/``DID NOT WARN``)
    whenever the test runs, so this is dead scaffolding rather than a false
    green. It is gated statically because that guaranteed failure never
    surfaces when the body does not execute — behind ``skip``/``skipif``, behind
    ``xfail`` (absorbed as XFAIL, which reads as a pass), or in unreached code.
    Put the actual code-under-test inside the ``with`` body."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_raises_context_body_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} pytest.raises/pytest.warns context(s) with an empty body.\n"
        "A with-block that never runs any statement exercises no exception/warning: pytest fails "
        "it with 'DID NOT RAISE'/'DID NOT WARN' when the test runs, and it is silently green when "
        "it does not (skip/xfail/unreached code). Move the code-under-test inside the 'with' "
        "body.\n" + "\n".join(violations)
    )


def test_empty_raises_context_body_lens_flags_unfinished_bodies():
    """Synthetic positive/negative control for the empty-raises-context-body
    lens: it must flag ``with``/``async with`` ``pytest.raises``/``pytest.warns``
    contexts (attribute and bare-imported name spellings) whose body is only
    ``pass``/``...``/a docstring/empty, and ignore contexts whose body contains
    a real executable statement."""
    positive_sources = [
        "def test_foo():\n    with pytest.raises(ValueError):\n        pass\n",
        "def test_foo():\n    async with pytest.raises(ValueError):\n        pass\n",
        "def test_foo():\n    with pytest.raises(ValueError, match='boom'):\n        ...\n",
        "def test_foo():\n    with pytest.warns(UserWarning):\n        pass\n",
        "import pytest\ndef test_foo():\n    with raises(ValueError):\n        pass\n",
        "import pytest\ndef test_foo():\n    with warns(UserWarning):\n        pass\n",
        ('def test_foo():\n    with pytest.raises(ValueError):\n        """docstring only"""\n'),
        "def test_foo():\n    with pytest.raises(ValueError):\n        # comment only\n        pass\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_raises_context_body_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    with pytest.raises(ValueError):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(ValueError) as exc_info:\n        foo()\n"
        "        assert exc_info.value.args[0] == 'boom'\n",
        "def test_foo():\n    async with pytest.raises(ValueError):\n        await foo()\n",
        "def test_foo():\n    with pytest.warns(UserWarning):\n        foo()\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        x = 1\n",
        "def test_foo():\n    pytest.raises(ValueError)\n",
        "def test_foo():\n    with my_custom_context():\n        pass\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_raises_context_body_violations(tree), f"lens should NOT flag:\n{source}"


_SELECTION_MARKERS = frozenset({"skip", "skipif", "xfail"})
"""Selection/outcome marker names that are silently ignored when applied to a
fixture. ``@pytest.mark.parametrize`` is deliberately NOT in the set: pytest
honours parametrize on a fixture (that is how parametrized fixtures work), and
``@pytest.mark.asyncio``/``@pytest.mark.anyio`` are behaviour markers, not
selection markers, so they are left to the async-decorator lenses."""


def _selection_marker_on_fixture_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``@pytest.fixture``-decorated
    function that also carries a selection marker — ``@pytest.mark.skip``,
    ``@pytest.mark.skipif``, ``@pytest.mark.xfail``, or the bare imported
    ``@skip``/``@skipif``/``@xfail`` spellings.

    pytest only honours selection markers on *collected test items*; a fixture
    is not collected as an item, so the marker is silently ignored: the tests
    that request the fixture run unconditionally no matter what condition the
    ``skipif`` was meant to gate, and nothing in the run reports the marker as
    dead. Both spellings are covered — the ``@pytest.mark.*`` chain (via
    ``_is_mark_decorator``) and the bare imported decorator — because a custom
    ``@skip``/``@xfail`` decorator stacked on a fixture is the same silent
    no-op either way. ``@pytest.mark.parametrize`` is deliberately left alone
    (legitimate on fixtures, see ``_SELECTION_MARKERS``), as is a module-level
    ``pytestmark = pytest.mark.skip`` deselecting the whole module.
    """
    found: list[tuple[int, str]] = []

    def _selection_name(dec: ast.AST) -> str | None:
        name = _decorator_name(dec)
        if name in _SELECTION_MARKERS:
            return name
        if _is_mark_decorator(dec):
            if isinstance(dec, ast.Call):
                dec = dec.func
            if isinstance(dec, ast.Attribute) and dec.attr in _SELECTION_MARKERS:
                return dec.attr
        return None

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_fixture = any(_decorator_name(dec) == "fixture" for dec in node.decorator_list)
        if not is_fixture:
            continue
        for dec in node.decorator_list:
            name = _selection_name(dec)
            if name is None:
                continue
            marker_repr = ast.unparse(dec)
            found.append(
                (
                    dec.lineno,
                    f"@pytest.mark.{name} on a @pytest.fixture function {node.name!r} — "
                    "pytest only honours selection markers on collected test items, so this "
                    "marker is silently ignored: the tests using the fixture run regardless of "
                    f"the {name} condition. Hoist the gate into the fixture body with "
                    f"pytest.skip(...) instead ({marker_repr})",
                )
            )
    return found


def test_no_selection_markers_on_fixtures():
    """A ``@pytest.mark.skip``/``skipif``/``xfail`` (or bare ``@skip``/``@skipif``/
    ``@xfail``) stacked on a ``@pytest.fixture`` function is silently ignored:
    pytest only honours selection markers on *collected test items*, and a
    fixture is never collected as one. A ``skipif`` that was meant to gate the
    tests built from the fixture therefore never triggers — a true condition
    still runs the tests, and nothing in the run reports the marker as dead —
    while a reader believes the conditional skip is in force. It is the fixture
    twin of the ``unconditional-skip-marker`` lens, which deliberately leaves
    fixtures alone, and the ``skip-without-reason``/``constant-condition-skip``
    siblings check *reason*/*condition*, not whether the marker is even on a
    test. Move the gate into the fixture body (``pytest.skip(...)`` behind the
    real ``if``), where it fires when the fixture is requested."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _selection_marker_on_fixture_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} selection marker(s) on a @pytest.fixture function.\n"
        "pytest only honours skip/skipif/xfail on collected test items — a fixture is never one,\n"
        "so the marker is silently ignored and a skipif that was meant to gate the tests built\n"
        "from the fixture never fires. Hoist the gate into the fixture body with\n"
        "pytest.skip(...) behind the real if, where it takes effect when the fixture is\n"
        "requested.\n" + "\n".join(violations)
    )


def _bound_method_truthiness_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` for every ``assert obj.attr`` /
    ``assert not obj.attr`` whose attribute is a *method* — evidenced by the
    same attribute being called (``obj.attr(...)``) elsewhere in the file —
    but is asserted without trailing parentheses.

    A bare attribute access on a bound method returns the method object
    itself, which is always truthy; the ``assert`` is therefore a silent
    false-green (``assert obj.method`` always passes) or a permanent failure
    (``assert not obj.method`` always fails, since a method object is never
    falsy). Either way the assertion says nothing about the behaviour under
    test. The call-site evidence keeps this lens precise: an attribute that
    is *never* called (a plain boolean/property attribute, e.g.
    ``assert response.ok``) is a legitimate truthiness check and is left
    alone; only a demonstrably-invocable method trips it."""
    called = {
        ast.dump(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            test = test.operand
        if not isinstance(test, ast.Attribute):
            continue
        if ast.dump(test) not in called:
            continue
        name = test.attr
        violations.append((node.lineno, f"assert {name} — bare bound-method reference is always truthy; call {name}()"))
    return violations


def test_no_bound_method_truthiness_asserts():
    """An ``assert obj.method`` / ``assert not obj.method`` against a bound
    method (verified to be a method by its use as a call elsewhere in the same
    file) asserts the method object itself, which is always truthy. The
    positive spelling is a silent false-green — the test passes regardless of
    whether ``method()`` would return a truthy value — and the ``not``-wrapped
    spelling always fails. Both are almost always a missing ``()`` from a
    forgotten call. The lens only fires when the attribute is demonstrably a
    method (it is invoked parenthesised elsewhere); a plain truthiness check on
    a boolean/property attribute (``assert response.ok``) is legitimate."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _bound_method_truthiness_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} truthiness assert(s) against a bare bound method.\n"
        "assert obj.method() — a bound method object is always truthy, so asserting the bare\n"
        "reference is a silent false-green (or, under 'not', always fails). Add the calling ()\n"
        "so the assertion actually exercises the method's return value." + "\n".join(violations)
    )


def test_selection_marker_on_fixture_lens_flags_silent_noops():
    """Synthetic positive/negative control for the selection-marker-on-fixture
    lens: it must flag ``@pytest.mark.skip``/``skipif``/``xfail`` (called and
    bare, ``@pytest.fixture`` and ``@pytest_asyncio.fixture``, module-level
    fixture too, bare ``@skipif``/``@xfail`` imported spellings) stacked on a
    fixture, and ignore the same markers on tests/classes, ``@pytest.mark.
    parametrize`` on a fixture (legitimate), behaviour markers such as
    ``@pytest.mark.asyncio`` on a fixture, and a module-level
    ``pytestmark = pytest.mark.skip``."""
    positive_sources = [
        "@pytest.fixture\n@pytest.mark.skip\ndef fx():\n    return 1\n",
        "@pytest.fixture\n@pytest.mark.skip(reason='why')\ndef fx():\n    return 1\n",
        "@pytest.fixture\n@pytest.mark.skipif(not pkg, reason='no pkg')\ndef fx():\n    return 1\n",
        (
            "@pytest.fixture(scope='session')\n"
            "@pytest.mark.skipif(sys.platform == 'win32', reason='win')\n"
            "def fx():\n"
            "    return 1\n"
        ),
        "@pytest_asyncio.fixture\n@pytest.mark.xfail(reason='known bug')\ndef afx():\n    return 1\n",
        "@pytest.fixture\n@skipif(not openssl_available, reason='no ssl')\ndef fx():\n    return 1\n",
        "@pytest.fixture\n@xfail(reason='flaky')\nasync def afx():\n    return 1\n",
        (
            "import pytest\n"
            "\n"
            "class TestFoo:\n"
            "    @pytest.fixture\n"
            "    @pytest.mark.skip(reason='legacy')\n"
            "    def fx(self):\n"
            "        return 1\n"
        ),
        (
            "import pytest\n"
            "\n"
            "fx_value = None\n"
            "\n"
            "@pytest.fixture\n"
            "@pytest.mark.skipif(True, reason='temp')\n"
            "def fx():\n"
            "    global fx_value\n"
            "    return fx_value\n"
        ),
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _selection_marker_on_fixture_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "@pytest.fixture\n@pytest.mark.parametrize('x', [1, 2])\ndef fx(x):\n    return x\n",
        "@pytest.fixture\n@pytest.mark.asyncio\nasync def afx():\n    return 1\n",
        "@pytest.fixture\ndef fx():\n    return 1\n",
        "@pytest.mark.skip(reason='legacy')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.skipif(not pkg, reason='no pkg')\ndef test_foo():\n    assert x\n",
        "@pytest.mark.xfail(reason='known bug')\ndef test_foo():\n    assert x\n",
        "pytestmark = pytest.mark.skip(reason='whole module dormant')\ndef test_foo():\n    assert x\n",
        (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='legacy')\n"
            "class TestFoo:\n"
            "    def test_bar(self):\n"
            "        assert x\n"
        ),
        (
            "import pytest\n"
            "\n"
            "def make_fx():\n"
            "    @pytest.mark.skipif(True, reason='x')\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner\n"
        ),
        "@pytest.fixture\n@something_else\ndef fx():\n    return 1\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _selection_marker_on_fixture_violations(tree), f"lens should NOT flag:\n{source}"


def test_bound_method_lens_flags_missing_call_parens():
    """The bound-method lens must flag a bare assert on an attribute that is
    called parenthesised elsewhere (proving it is a method), and must NOT flag
    a plain truthiness check on a boolean/property attribute, a comparison, or
    a method that is genuinely invoked in the assert."""
    positive_sources = [
        ("def test_foo():\n    result = service.lookup(1)\n    assert service.lookup\n"),
        ("def test_foo():\n    started = runner.start()\n    assert not runner.start\n"),
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _bound_method_truthiness_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert response.ok\n",
        "def test_foo():\n    assert not config.enabled\n",
        "def test_foo():\n    service.lookup(1)\n    assert service.lookup(1) is not None\n",
        "def test_foo():\n    assert service.lookup(1) == expected\n",
        "def test_foo():\n    assert obj.method()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _bound_method_truthiness_violations(tree), f"lens should NOT flag:\n{source}"


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LENS: impossible cross-container-type equality (dict-view / set / list)
# ---------------------------------------------------------------------------
_DICT_VIEW_METHODS = {"keys", "values", "items"}


_DIRECT_CONTAINERS = {"set": "set", "frozenset": "set", "list": "list", "tuple": "list"}
_CONTAINER_LITERALS = {
    "set": (ast.Set,),
    "list": (ast.List, ast.Tuple),
    "dict": (ast.Dict,),
}


def _is_dict_view_call(node: ast.AST) -> str | None:
    """Return the dict-view kind (``keys``/``values``/``items``) for a
    ``<obj>.keys()`` / ``.values()`` / ``.items()`` call, else None.

    Any receiver is accepted: if the object is a real dict the view comparison
    is a guaranteed type mismatch, and if it is a test double the comparison is
    a mock tautology either way."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in _DICT_VIEW_METHODS:
        return None
    if node.args or node.keywords:
        return None
    return node.func.attr


def _container_kind(node: ast.AST) -> str | None:
    """Categorise a comparison operand into a container family, or None if it is
    not one the cross-type lens reasons about.

    Returns ``"dictview:<keys|values|items>"`` for a dict view call, ``"set"``
    for a set/frozenset literal or ``set(...)``/``frozenset(...)`` conversion,
    and ``"list"`` for a list/tuple literal or ``list(...)``/``tuple(...)``
    conversion. Non-container expressions, calls with arguments beyond a single
    iterable, and ``dict(...)``/``frozenset(...)``-with-multiple-args return
    None so the lens never over-reaches."""
    view = _is_dict_view_call(node)
    if view is not None:
        return f"dictview:{view}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _DIRECT_CONTAINERS and len(node.args) == 1 and not node.keywords:
            return _DIRECT_CONTAINERS[node.func.id]
        return None
    for kind, types in _CONTAINER_LITERALS.items():
        if isinstance(node, types):
            return kind
    return None


def _cross_type_comparison_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for ``assert`` comparisons whose two
    operands are container families that can never compare equal.

    All of these are decided at source time, so the assertion either ALWAYS
    FAILS (``==``) or ALWAYS PASSES (``!=``) no matter what the code under test
    produces:

    - a dict view (``d.keys()`` / ``d.values()`` / ``d.items()``) on one side
      compared with a set/list/tuple/dict literal or a ``set(...)``/``list(...)``
      conversion — ``dict_keys``-and-friends only compare equal to a same-kind
      *view*, so they never equal a literal or another container type;
    - a dict view compared with a *different* dict-view kind (``d.keys()`` vs
      ``e.values()``) — ``dict_keys`` never equals ``dict_values``;
    - a ``set`` family (literal or conversion) compared with a ``list`` family
      (literal or conversion) in either position — a ``set`` never equals a
      ``list``/``tuple``.

    ``set(x) == {set literal}`` (both sides ``set``), ``sorted(x) == [list]``
    (both sides ``list``), and same-kind dict-view comparisons
    (``d.keys() == e.keys()``) are deliberately left alone — those are the valid
    idioms. Only ``==``/``!=`` are considered; ``in``/``not in`` and ordering
    comparisons are other lenses' business.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare) or len(sub.ops) != 1:
                continue
            if not isinstance(sub.ops[0], (ast.Eq, ast.NotEq)):
                continue
            op = sub.ops[0]
            left, right = sub.left, sub.comparators[0]

            left_kind = _container_kind(left)
            right_kind = _container_kind(right)
            if left_kind is None or right_kind is None:
                continue

            if left_kind == right_kind and left_kind not in ("set", "list"):
                continue

            detail = None
            if left_kind.startswith("dictview:") or right_kind.startswith("dictview:"):
                if left_kind == right_kind:
                    continue
                kind_names = {
                    "dictview:keys": "dict.keys()",
                    "dictview:values": "dict.values()",
                    "dictview:items": "dict.items()",
                    "set": "a set",
                    "list": "a list/tuple",
                }
                lname = kind_names.get(left_kind, left_kind)
                rname = kind_names.get(right_kind, right_kind)
                detail = (
                    f"{ast.unparse(sub)} — compares {lname} against {rname}; "
                    f"a dict view only compares equal to a view of the same kind, so this "
                    f"{'ALWAYS FAILS' if isinstance(op, ast.Eq) else 'ALWAYS PASSES'} "
                    f"regardless of the dict contents"
                )
            else:
                if left_kind == right_kind:
                    continue
                detail = (
                    f"{ast.unparse(sub)} — compares {left_kind} against {right_kind}; "
                    f"a {left_kind} never compares equal to a {right_kind}, so this "
                    f"{'ALWAYS FAILS' if isinstance(op, ast.Eq) else 'ALWAYS PASSES'} "
                    f"regardless of the operands (normalise both sides with the same container)"
                )

            found.append((sub.lineno, detail))
    return found


def test_no_cross_container_type_equality():
    """``assert d.keys() == {"a", "b"}`` / ``assert set(x) == [a, b]`` — equality
    between two operands that can never be the same type. A dict ``keys()``/
    ``values()``/``items()`` view only compares equal to a view *of the same
    kind*, so comparing it against a set/list/tuple/dict literal (or a
    ``set(...)``/``list(...)``/``tuple(...)`` conversion) is ALWAYS False under
    ``==`` and ALWAYS True under ``!=`` — a silent false-green every time the
    test author reaches for ``!=``, and an always-red that masks the real bug
    under ``==``. Likewise ``set(...) == [list literal]`` and
    ``list(...) == {set literal}`` can never hold because a ``set`` never equals
    a ``list``/``tuple``. The fix is to normalise both sides with the same
    container (``set(d.keys()) == {...}``); ``set(x) == {set literal}`` and
    ``sorted(x) == [list literal]`` are left alone because both operands are
    already the same type."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _cross_type_comparison_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} cross-container-type equality comparison(s).\n"
        "Comparing operands of different container types (a dict view vs a set/list literal,\n"
        "or a set(...) vs a list/tuple literal) is decided at source time: dict views only\n"
        "equal same-kind views, and a set never equals a list/tuple. Normalise both sides\n"
        "with the same container, e.g. set(d.keys()) == {...}.\n" + "\n".join(violations)
    )


def test_cross_container_type_lens_flags_impossible_equality():
    """Synthetic positive/negative control for the cross-container-type-equality
    lens: it must flag dict-view-vs-literal, set-vs-list/tuple, and
    list-vs-set comparisons, and ignore the valid same-type idioms
    (``set(x) == {set}``, ``sorted(x) == [list]``, and pair-of-same-kind-view
    comparisons)."""
    positive_sources = [
        "def test_foo():\n    assert d.keys() == {'a', 'b'}\n",
        "def test_foo():\n    assert d.keys() != ['a', 'b']\n",
        "def test_foo():\n    assert d.values() == ('x', 'y')\n",
        "def test_foo():\n    assert d.items() == {'a': 1}\n",
        "def test_foo():\n    assert {'a': 1}.keys() == list(d)\n",
        "def test_foo():\n    assert set(d) == ['a', 'b']\n",
        "def test_foo():\n    assert set(x) == ['a', 'b']\n",
        "def test_foo():\n    assert list(x) == {'a', 'b'}\n",
        "def test_foo():\n    assert tuple(x) == {1, 2}\n",
        "def test_foo():\n    assert set(x) != ('a', 'b')\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _cross_type_comparison_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert set(x) == {'a', 'b'}\n",
        "def test_foo():\n    assert set(x) == frozenset({'a', 'b'})\n",
        "def test_foo():\n    assert sorted(x) == ['a', 'b']\n",
        "def test_foo():\n    assert list(x) == ['a', 'b']\n",
        "def test_foo():\n    assert tuple(x) == (1, 2)\n",
        "def test_foo():\n    assert d.keys() == e.keys()\n",
        "def test_foo():\n    assert d.items() == e.items()\n",
        "def test_foo():\n    assert d.keys() == {'a': 1}.keys()\n",
        "def test_foo():\n    assert {'a', 'b'} == set(d)\n",
        "def test_foo():\n    assert set(x) == set(y)\n",
        "def test_foo():\n    assert 'a' in d.keys()\n",
        "def test_foo():\n    assert len(set(x)) == 3\n",
        "def test_foo():\n    assert set(x) <= {'a', 'b'}\n",
        "def _helper():\n    return set(x) == ['a', 'b']\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _cross_type_comparison_violations(tree), f"lens should NOT flag:\n{source}"


# LENS: empty-string membership / str-method tautologies
# ---------------------------------------------------------------------------
_EMPTY_STRING_METHODS = frozenset(
    {
        "startswith",
        "endswith",
        "removeprefix",
        "removesuffix",
        "find",
        "rfind",
        "index",
        "count",
        "split",
        "rsplit",
        "partition",
        "rpartition",
        "replace",
    }
)
"""``str`` methods whose *empty-string* first argument forces a fixed outcome."""

_EMPTY_STRING_FIXED_OUTCOMES: dict[str, str] = {
    "startswith": "always True — every string begins with the empty string",
    "endswith": "always True — every string ends with the empty string",
    "removeprefix": "no-op — returns the receiver unchanged",
    "removesuffix": "no-op — returns the receiver unchanged",
    "find": "fixed non-negative index — can never report -1 (missing)",
    "rfind": "fixed non-negative index — can never report -1 (missing)",
    "index": "always succeeds immediately — can never raise ValueError",
    "count": "always truthy — the empty string occurs len(receiver) + 1 times",
    "split": "always raises ValueError — the assertion can never complete",
    "rsplit": "always raises ValueError — the assertion can never complete",
    "partition": "always raises ValueError — the assertion can never complete",
    "rpartition": "always raises ValueError — the assertion can never complete",
    "replace": "always raises ValueError — the assertion can never complete",
}


def _empty_str_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and not node.value


def _empty_string_tautologies(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose test
    expression contains an empty-string membership test or ``str`` method call
    with a fixed, source-time outcome.

    ``'' in value`` is always True and ``'' not in value`` always False — the
    empty string is a substring of every string — while ``x.startswith('')``
    / ``x.endswith('')`` are always True, ``x.removeprefix('')`` /
    ``x.removesuffix('')`` are no-ops, ``x.find('')`` / ``x.rfind('')`` /
    ``x.index('')`` / ``x.count('')`` can never report a missing match, and
    ``x.split('')`` / ``x.rsplit('')`` / ``x.partition('')`` /
    ``x.rpartition('')`` / ``x.replace('', ...)`` always raise ``ValueError``.
    Every one of these reports a fixed verdict no matter how broken the
    behaviour under test is; they are usually a typo for ``' '`` (or for the
    bare method together with a real argument) and always a dead assertion."""
    found: list[tuple[int, str]] = []

    def _record(lineno: int, detail: str) -> None:
        pair = (lineno, detail)
        if pair not in found:
            found.append(pair)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in _EMPTY_STRING_METHODS
                and sub.args
                and _empty_str_constant(sub.args[0])
            ):
                _record(
                    node.lineno,
                    f"assert ... .{sub.func.attr}('') — {_EMPTY_STRING_FIXED_OUTCOMES[sub.func.attr]}",
                )
            if isinstance(sub, ast.Compare) and _empty_str_constant(sub.left):
                for op in sub.ops:
                    if not isinstance(op, (ast.In, ast.NotIn)):
                        continue
                    verdict = "always PASSES" if isinstance(op, ast.In) else "always FAILS"
                    _record(
                        node.lineno,
                        f"assert '' in value — {verdict} at source time "
                        "(the empty string is a substring of every string)",
                    )
    return found


def test_no_empty_string_membership_and_method_tautologies():
    """Assertions built around the empty string have fixed outcomes that can
    never depend on the behaviour under test: ``assert '' in value`` / ``assert
    value.startswith('')`` report green no matter what, ``assert '' not in
    value`` always fails (the empty string is a substring of every string), and
    the ``str`` methods that reject (or no-op on) an empty argument never do any
    work worth asserting against. These are nearly always a typo for ``' '`` or
    for a real separator, and the assertion quietly pins a truth that holds for
    every input — dead code that hides regressions."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_string_tautologies(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} empty-string tautolog(ies) in assertions.\n"
        "An empty-string membership test or str-method argument has a fixed outcome "
        "that can never depend on the code under test — it is a dead assertion "
        "(usually a ' ' typo).\n" + "\n".join(violations)
    )


def test_empty_string_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the empty-string tautology lens:
    it must flag every empty-string membership / str-method form in an assert
    and ignore non-empty separators, empty-string *receivers* (``''.join()`` is
    a legitimate idiom), and unrelated comparisons."""
    positive_sources = [
        "def test_foo():\n    assert '' in value\n",
        "def test_foo():\n    assert '' not in value\n",
        "def test_foo():\n    assert text.startswith('')\n",
        "def test_foo():\n    assert text.endswith('')\n",
        "def test_foo():\n    assert text.removeprefix('') == expected\n",
        "def test_foo():\n    assert text.removesuffix('') == expected\n",
        "def test_foo():\n    assert text.find('') == -1\n",
        "def test_foo():\n    assert text.rfind('') == -1\n",
        "def test_foo():\n    assert text.index('') == 0\n",
        "def test_foo():\n    assert text.count('')\n",
        "def test_foo():\n    assert text.split('') == []\n",
        "def test_foo():\n    assert text.rsplit('') == [text]\n",
        "def test_foo():\n    assert 'a' in text.partition('')\n",
        "def test_foo():\n    assert text.rpartition('') == ('', '', text)\n",
        "def test_foo():\n    assert text.replace('', '-') == text\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_string_tautologies(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert text.startswith('a')\n",
        "def test_foo():\n    assert text.endswith('!')\n",
        "def test_foo():\n    assert text.find('x') != -1\n",
        "def test_foo():\n    assert text.split(' ') == [text]\n",
        "def test_foo():\n    assert text.replace('a', 'b') == expected\n",
        "def test_foo():\n    assert ''.join(items) == expected\n",
        "def test_foo():\n    assert ' ' in text\n",
        "def test_foo():\n    assert text == ''\n",
        "def test_foo():\n    assert value is None\n",
        "def test_foo():\n    assert len(text) == 0\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_string_tautologies(tree), f"lens should NOT flag:\n{source}"


# ---------------------------------------------------------------------------
# LENS: NaN as an expected comparison value
# ---------------------------------------------------------------------------
def _is_nan_literal(node: ast.AST) -> bool:
    """Return True when ``node`` is a NaN-typed expression: a ``float`` or
    ``str`` constant spelling NaN, ``float('nan')`` (with optional sign), or
    ``math.nan``."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, float) and str(value).lower() == "nan":
            return True
        if isinstance(value, str) and value.lower() in {"nan", "+nan", "-nan"}:
            return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_nan_literal(node.operand)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
        return bool(node.args) and _is_nan_literal(node.args[0])
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "nan"
        and isinstance(node.value, ast.Name)
        and node.value.id == "math"
    )


def _nan_comparison_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every single comparison assertion
    that uses NaN (``float('nan')`` / ``math.nan``) directly as an expected
    operand.

    NaN is the one IEEE-754 value unequal to itself, and every NaN-typed
    expression evaluates to a distinct NaN object (``float('nan')`` /
    ``math.nan`` is minted on each reference in CPython), so ``==`` / ``is``
    against it can never hold (always FAILS) and ``!=`` / ``is not`` against it
    always PASSES. NaN fed *into* the code under test as an argument — e.g.
    ``assert safe_int(float('nan')) == 0`` — is a legitimate edge-case test and
    is deliberately not flagged; only a comparison whose expected operand is a
    NaN expression is a fixed-outcome dead assertion."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        op = test.ops[0]
        if not isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            continue
        for side in (test.left, *test.comparators):
            if not _is_nan_literal(side):
                continue
            verdict = "always FAILS" if isinstance(op, (ast.Eq, ast.Is)) else "always PASSES"
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — comparing against {ast.unparse(side)} "
                    f"(NaN, the one value unequal to itself): {verdict}",
                )
            )
            break
    return found


def test_no_nan_expected_comparison_value():
    """Assertions that compare a value against NaN are fixed-outcome dead code:
    NaN is the one IEEE-754 value unequal to itself, so ``assert x == float('nan')``
    / ``assert x is math.nan`` can never pass and the ``!=`` / ``is not`` twins
    can never fail — green or red regardless of the behaviour under test. The
    only legitimate NaN use in a test is pushing it *into* the code under test
    (``assert safe_int(float('nan')) == 0``), which is unaffected here."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _nan_comparison_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} comparison(s) against NaN.\n"
        "NaN is the one value unequal to itself, so ==/is can never pass and !=/is not can never fail.\n"
        "Assert against the real expected value instead.\n" + "\n".join(violations)
    )


def test_nan_lens_flags_dead_comparisons():
    """Synthetic positive/negative control for the NaN lens: it must flag every
    spelling of a NaN expected operand (`float('nan')`, `float('-nan')`,
    `math.nan`, on either side, with any of `==`/`!=`/`is`/`is not`) and ignore
    NaN fed into the code under test, `math.isnan()` guards, and ordinary
    numeric comparisons."""
    positive_sources = [
        "def test_foo():\n    assert result == float('nan')\n",
        "def test_foo():\n    assert result != float('nan')\n",
        'def test_foo():\n    assert result == float("nan")\n',
        "def test_foo():\n    assert result is math.nan\n",
        "def test_foo():\n    assert result is not math.nan\n",
        "def test_foo():\n    assert math.nan == result\n",
        "def test_foo():\n    assert result == -float('nan')\n",
        "def test_foo():\n    assert result != float('-nan')\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _nan_comparison_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert safe_int(float('nan')) == 0\n",
        "def test_foo():\n    assert math.isnan(float('nan'))\n",
        "def test_foo():\n    assert result == float('inf')\n",
        "def test_foo():\n    assert result == 1.5\n",
        "def test_foo():\n    assert value is None\n",
        "def test_foo():\n    assert result >= 0\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _nan_comparison_violations(tree), f"lens should NOT flag:\n{source}"


# ---------------------------------------------------------------------------
# LENS: redundant single-element isinstance type tuple
# ---------------------------------------------------------------------------
def _single_element_isinstance_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``isinstance(x, (T,))`` call
    whose types argument is a single-element tuple literal.

    A one-element type tuple is element-for-element identical to passing the
    type bare, so the tuple adds nothing beyond signalling that the author
    thought ``isinstance`` needed a tuple when it did not. It reads as a
    defensive-typing leftover and, written inside an assertion, quietly checks
    the same thing ``isinstance(x, T)`` would — most likely a copy-paste from a
    sibling call that really does pass several types."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "isinstance":
            continue
        if len(node.args) != 2:
            continue
        types = node.args[1]
        if not isinstance(types, ast.Tuple) or len(types.elts) != 1:
            continue
        inner = ast.unparse(types.elts[0])
        found.append(
            (
                node.lineno,
                f"isinstance({ast.unparse(node.args[0])}, ({inner},)) — a single-element type tuple "
                f"is just isinstance(x, {inner}); drop the redundant tuple",
            )
        )
    return found


def test_no_single_element_isinstance_type_tuple():
    """``isinstance(x, (T,))`` wraps a single type in a tuple that behaves
    identically to ``isinstance(x, T)`` — the tuple only confuses the reader
    into thinking more than one type is being checked. Inside an assertion it
    never changes the verdict, so the surrounding check is what the author
    thinks it is only by luck; spell it ``isinstance(x, T)``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _single_element_isinstance_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} isinstance call(s) with a single-element type tuple.\n"
        "A one-element type tuple is equivalent to isinstance(x, T) — write it without "
        "the redundant tuple.\n" + "\n".join(violations)
    )


def test_single_element_isinstance_lens_flags_redundant_tuples():
    """Synthetic positive/negative control for the single-element isinstance
    lens: it must flag a one-type tuple literal whether in an assert, in helper
    code, or on an attribute path, and ignore bare types, multi-type tuples,
    ``type()`` comparisons, and unrelated calls."""
    positive_sources = [
        "isinstance(value, (str,))\n",
        "def test_foo():\n    assert isinstance(result, (int,))\n",
        "def _helper(x):\n    return isinstance(x, (list,))\n",
        "isinstance(value, (module.Type,))\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _single_element_isinstance_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "isinstance(value, str)\n",
        "isinstance(value, (str, bytes))\n",
        "def test_foo():\n    assert isinstance(value, int)\n",
        "def test_foo():\n    assert type(value) is int\n",
        "def test_foo():\n    assert value.__class__ is int\n",
        "isinstance(value, typing.Union[str, bytes])\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _single_element_isinstance_violations(tree), f"lens should NOT flag:\n{source}"


# ---------------------------------------------------------------------------
# LENS: assert statements inside a pytest.raises(...) body
# ---------------------------------------------------------------------------
def _is_raises_context_only(node: ast.AST) -> ast.Call | None:
    """Return the ``pytest.raises(...)``/``raises(...)`` call backing a ``with``
    context item, or ``None`` when the context expression is not a raises call.

    Unlike ``_RAISES_CONTEXT_FUNCS`` (which also admits ``warns``), this lens
    scopes its match to ``raises`` only — a ``with pytest.warns(...)`` body runs
    to completion (warnings don't raise), so an assert inside it is reachable
    and meaningful. The ``pytest.`` attribute spelling and the bare imported
    ``raises`` name are both matched; an unrelated ``obj.raises(...)`` attribute
    is deliberately not (a custom helper's body semantics are unknown)."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr != "raises":
            return None
        if not (isinstance(func.value, ast.Name) and func.value.id == "pytest"):
            return None
    elif isinstance(func, ast.Name):
        if func.id != "raises":
            return None
    else:
        return None
    return node


def _expected_exception_operand(call: ast.Call) -> ast.AST | None:
    """Return the expression passed as ``pytest.raises``'s expected-exception
    argument (first positional or ``expected_exception=``), or ``None`` when
    there is none."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "expected_exception":
            return kw.value
    return None


def _raises_body_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` that is a direct
    statement of a ``with pytest.raises(...)`` body whose expected exception is
    not ``AssertionError``.

    An assert inside the ``pytest.raises`` region is either unreachable (when it
    follows the call that raises the expected exception, the assert never runs)
    or a failure-mode mismatch (when it precedes it, a broken condition raises
    ``AssertionError`` — almost never the exception `pytest.raises` expects — so
    the failure reads as a wrong-exception mismatch instead of naming the broken
    condition). Both are silent signal loss: a reader — and a mutation-testing
    run — believes the assert verifies behaviour it provably never checks.
    Blocks expecting ``AssertionError`` are left alone: there the assert *is*
    the intended trigger. Descent into nested function/class definitions inside
    the body is skipped — those are definitions, not statements that execute in
    the raises region."""
    found: list[tuple[int, str]] = []

    def _collect_body_asserts(body: list[ast.stmt], exc_repr: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assert):
                verdict = (
                    "unreachable when the expected exception is raised (anything after the "
                    "raising call never runs), and raises a mismatched AssertionError (not the "
                    "expected exception) when it fails"
                )
                found.append(
                    (
                        stmt.lineno,
                        f"assert {ast.unparse(stmt.test)} is a direct statement of the "
                        f"pytest.raises({exc_repr}) body — the assertion is {verdict}; move it "
                        "after the with-block (assert on exc_info.value) or make it the intended "
                        "trigger with pytest.raises(AssertionError)",
                    )
                )
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            elif isinstance(stmt, ast.If):
                _collect_body_asserts(stmt.body, exc_repr)
                _collect_body_asserts(stmt.orelse, exc_repr)
            elif isinstance(stmt, ast.Try):
                _collect_body_asserts(stmt.body, exc_repr)
                _collect_body_asserts(stmt.orelse, exc_repr)
                for handler in stmt.handlers:
                    _collect_body_asserts(handler.body, exc_repr)
            elif isinstance(stmt, (ast.For, ast.While)):
                _collect_body_asserts(stmt.body, exc_repr)
                _collect_body_asserts(stmt.orelse, exc_repr)
            elif isinstance(stmt, ast.With):
                inner_is_raises = any(_is_raises_context_only(item.context_expr) is not None for item in stmt.items)
                if not inner_is_raises:
                    _collect_body_asserts(stmt.body, exc_repr)
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    _collect_body_asserts(case.body, exc_repr)

    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        raises_call = None
        for item in node.items:
            call = _is_raises_context_only(item.context_expr)
            if call is not None:
                raises_call = call
                break
        if raises_call is None:
            continue
        expected = _expected_exception_operand(raises_call)
        if isinstance(expected, ast.Name) and expected.id == "AssertionError":
            continue
        exc_repr = ast.unparse(expected) if expected is not None else "..."
        _collect_body_asserts(node.body, exc_repr)
    return found


def test_no_assert_inside_raises_body():
    """An ``assert`` that is a direct statement of a ``with pytest.raises(...)``
    body whose expected exception is not ``AssertionError`` is either dead or
    misleading: after the raising call the assert never executes, and before it
    a failure raises ``AssertionError`` instead of the expected error, so the
    test fails with a wrong-exception mismatch that names neither the broken
    condition nor the assertion. Blocks expecting ``AssertionError`` are the
    intentional validator-trip idiom and are left alone, as are
    ``pytest.warns`` bodies (their body runs to completion)."""

    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _raises_body_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assert statement(s) inside a pytest.raises(...) body.\n"
        "An assert in the expected-exception region is unreachable (after the raising call) or\n"
        "raises a mismatched AssertionError (before it); move the assertion after the with-block\n"
        "or make it the intended trigger with pytest.raises(AssertionError).\n" + "\n".join(violations)
    )


def test_raises_body_assert_lens_flags_hazardous_asserts():
    """Synthetic positive/negative control for the raises-body-assert lens: it
    must flag an assert that is a direct statement of a ``pytest.raises`` body
    (positional or ``expected_exception=`` exception, with or without ``as
    exc_info``, before or after the raising call, bare or ``pytest.``-qualified,
    and nested under an ``if``), and ignore asserts outside the block, in
    ``pytest.warns`` bodies, in ``AssertionError``-expecting blocks, in nested
    helper definitions, and under unrelated context managers."""
    positive_sources = [
        "def test_foo():\n    with pytest.raises(ValueError):\n        step()\n        assert ready\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        assert events.DoesNotExist\n",
        "def test_foo():\n    with pytest.raises(ValueError) as exc_info:\n        assert ready\n",
        "def test_foo():\n    with pytest.raises(expected_exception=ValueError):\n        assert ready\n",
        "from pytest import raises\n\ndef test_foo():\n    with raises(ValueError):\n        assert ready\n",
        "def test_foo():\n    with pytest.raises(AttributeError, match='no attribute'):\n"
        "        assert events.DoesNotExist\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        if flag:\n            assert ready\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        for x in items:\n            assert x\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _raises_body_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    with pytest.raises(AssertionError, match='x invalid'):\n        assert validate(x)\n",
        "def test_foo():\n    with pytest.raises(AssertionError):\n        assert validate(x)\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        step()\n    assert ready\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        def helper():\n"
        "            assert ready\n        step()\n",
        "def test_foo():\n    with pytest.warns(UserWarning):\n        assert flag\n",
        "def test_foo():\n    with some_manager():\n        assert ready\n",
        "def test_foo():\n    with pytest.raises(ValueError):\n        step()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _raises_body_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _assert_in_finally_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` placed in a
    ``finally:`` clause, where it runs only during unwinding.

    A failing assert in ``finally`` runs while an exception (or another
    failure) is unwinding, so it *replaces* that in-flight error with its own
    ``AssertionError`` and the original traceback is lost; a passing assert
    in ``finally`` adds nothing a check on the normal path would not express.
    This is the unwind-twin of the assert-inside-``except`` lens. Both
    ``try``/``finally`` and ``except*``/``finally`` (``ast.TryStar``) forms
    are covered."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        for stmt in node.finalbody:
            if isinstance(stmt, ast.Assert):
                found.append(
                    (
                        stmt.lineno,
                        f"assert {ast.unparse(stmt.test)} in a finally: clause masks any in-flight exception",
                    )
                )
    return found


def test_no_assert_in_finally():
    """An assertion in a ``finally:`` clause runs only during unwinding, so it
    either masks the exception that triggered the unwind (an ``AssertionError``
    replaces the original traceback a reader needs to debug the real
    regression) or verifies nothing that a check on the normal path could not
    express more clearly. Move the check to the normal path or into the
    ``except`` handler that owns the failure."""

    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _assert_in_finally_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assert(s) inside a finally: clause.\n"
        "An assert in finally runs during unwinding and masks the original failure.\n"
        "Move the check to the normal path or into the except handler that owns it.\n" + "\n".join(violations)
    )


def test_assert_in_finally_lens_flags_unwind_time_verification():
    """Synthetic positive/negative control for the assert-in-finally lens: it
    must flag an ``assert`` in a ``finally:`` clause (plain ``try`` and
    ``try/except`` twins) and ignore asserts on the normal path, asserts in
    ``except`` handlers (owned by the sibling lens), and non-assert ``finally``
    statements."""
    positive_sources = [
        "def test_foo():\n    try:\n        foo()\n    finally:\n        assert cleanup_done\n",
        (
            "def test_foo():\n"
            "    try:\n"
            "        foo()\n"
            "    except Exception:\n"
            "        raise\n"
            "    finally:\n"
            "        assert x == 1\n"
        ),
        "def test_foo():\n    try:\n        foo()\n    finally:\n        assert result is not None\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _assert_in_finally_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    try:\n        foo()\n    finally:\n        cleanup()\n",
        "def test_foo():\n    assert x == 1\n",
        "def test_foo():\n    try:\n        assert x\n    except Exception:\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except Exception as e:\n        assert e.args\n",
        (
            "def test_foo():\n"
            "    try:\n"
            "        foo()\n"
            "    except Exception as e:\n"
            "        assert e.args\n"
            "    finally:\n"
            "        cleanup()\n"
        ),
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _assert_in_finally_violations(tree), f"lens should NOT flag:\n{source}"


def _exception_names(node: ast.AST) -> list[str]:
    """Collect the class names named in an ``except`` type expression — a bare
    name (``except BaseException:``) or the elements of a handled tuple
    (``except (ValueError, BaseException):``). Attribute-qualified spellings
    (``except infra.Error:``) and nested tuples deliberately return nothing."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple):
        return [e.id for e in node.elts if isinstance(e, ast.Name)]
    return []


def _named_base_exception_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``except`` handler that
    names ``BaseException`` — directly or as an element of a handled tuple.

    ``BaseException`` is the base of ``KeyboardInterrupt``/``SystemExit``/
    ``GeneratorExit`` as well as ``Exception``, so a test handler that names it
    swallows the control-flow signals a test can never want to catch, silently
    converting an interrupt during a hang into a false green. This is the
    name-spelled mask the bare-``except:`` lens exists to forbid; a bare
    ``except:`` (no type) is owned by that sibling lens and is left alone
    here."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if "BaseException" in _exception_names(node.type):
            found.append(
                (node.lineno, "except BaseException swallows KeyboardInterrupt/SystemExit alongside Exception")
            )
    return found


def test_no_named_base_exception_handler():
    """A handler that names ``BaseException`` explicitly is the bare-``except:``
    swallow wearing a mask: it catches ``KeyboardInterrupt``/``SystemExit``/
    ``GeneratorExit`` as well as ``Exception``, so a test can accidentally
    absorb an interrupt that should have aborted the run and report false
    green. Name ``Exception`` (or the concrete failures the code is documented
    to raise) instead."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _named_base_exception_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} except handler(s) naming BaseException.\n"
        "BaseException catches KeyboardInterrupt/SystemExit too; name Exception\n"
        "or the specific failure the code is documented to raise.\n" + "\n".join(violations)
    )


def test_named_base_exception_lens_flags_control_flow_swallows():
    """Synthetic positive/negative control for the named-BaseException lens: it
    must flag ``except BaseException`` directly, with ``as``, and inside a
    handled tuple, and ignore bare ``except:`` (owned by the sibling lens),
    ``except Exception``, tuples of concrete exceptions, and
    attribute-qualified handlers."""
    positive_sources = [
        "def test_foo():\n    try:\n        foo()\n    except BaseException:\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except (ValueError, BaseException):\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except BaseException as exc:\n        handle(exc)\n",
        "def test_foo():\n    try:\n        foo()\n    except (BaseException,):\n        pass\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _named_base_exception_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    try:\n        foo()\n    except:\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except Exception:\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except Exception as exc:\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except (TypeError, ValueError):\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except ExceptionGroup:\n        pass\n",
        "def test_foo():\n    try:\n        foo()\n    except infra.Error:\n        pass\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _named_base_exception_violations(tree), f"lens should NOT flag:\n{source}"


def _loop_has_exit(node: ast.While) -> bool:
    """True when ``node``'s body reaches a statement that terminates it: a
    ``break`` targeting ``node`` itself, or a ``return``/``raise`` that
    unwinds the enclosing function and therefore the loop.

    A ``break`` inside a nested ``for``/``while`` targets the *inner* loop and
    never exits ``node``, so it does not count; a ``return``/``raise`` inside a
    nested loop (or via a ``try``/``except``) still unwinds the function and
    *does* count. ``try``/``except``/``else``/``finally`` create no loop scope
    of their own. Nested function/class/lambda definitions are skipped
    entirely — their ``return``s belong to a different scope and cannot exit
    ``node``."""

    def walk(n: ast.AST, in_nested_loop: bool) -> bool:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return False
        if isinstance(n, ast.Break):
            return not in_nested_loop
        if isinstance(n, (ast.Return, ast.Raise)):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)) and n is not node:
            in_nested_loop = True
        return any(walk(child, in_nested_loop) for child in ast.iter_child_nodes(n))

    return walk(node, False)


def _infinite_exit_less_loop_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``while`` loop whose
    condition is a statically-foldable constant-true literal and whose body has
    no statement that can terminate it — no ``break`` targeting the loop and no
    reachable ``return``/``raise`` unwinding the enclosing function.

    A ``while True:``/``while 1:`` loop's condition never becomes false at the
    language level, so it terminates only via ``break``/``return``/``raise``.
    One with none of those reachable is an infinite loop that hangs the test —
    and every test after it in the same process — with an opaque failure, the
    exact hazard the unbounded-subprocess and unbounded-thread-join lenses
    already guard. Dynamic conditions (names, calls, comparisons) are left
    alone: those can change through the code under test."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.While):
            continue
        test = node.test
        if not isinstance(test, ast.Constant):
            continue
        if isinstance(test.value, complex) or not test.value:
            continue
        if _loop_has_exit(node):
            continue
        found.append(
            (node.lineno, f"while {ast.unparse(test)} has no break/return/raise that stops it — the loop is infinite")
        )
    return found


def test_no_infinite_constant_condition_loops():
    """A ``while`` loop whose condition is a statically-foldable constant-true
    literal (``while True:``, ``while 1:``, ...) terminates only via ``break``;
    without one, it is an infinite loop that hangs the test and every test
    after it in the same process, with an opaque failure. Guard on a real
    condition or break explicitly when the work completes."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _infinite_exit_less_loop_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} exit-less constant-condition while loop(s).\n"
        "A while True loop with no break can never terminate; add a break on the\n"
        "completion condition or drive the loop with a real (non-constant) guard.\n" + "\n".join(violations)
    )


def test_infinite_loop_lens_flags_hang_risks():
    """Synthetic positive/negative control for the infinite-loop lens: it must
    flag ``while True:``/``while 1:`` bodies with no way out — no ``break``
    targeting the loop (a nested-loop ``break`` does not count) and no reachable
    ``return``/``raise`` — and ignore loops with a direct ``break`` (even one
    reached through an ``if`` or a ``try`` body), loops that unwind via
    ``return``/``raise``, loops with a dynamic condition, and ``for`` loops."""
    positive_sources = [
        "def test_foo():\n    while True:\n        step()\n",
        "def test_foo():\n    while 1:\n        x = foo()\n        x.bar()\n",
        "def test_foo():\n    while True:\n        for i in items:\n            break\n",
        (
            "def test_foo():\n"
            "    while True:\n"
            "        try:\n"
            "            work()\n"
            "        except Exception:\n"
            "            pass\n"
        ),
        "def test_foo():\n    while True:\n        step(1)\n        step(2)\n    return\n",
        "async def test_foo():\n    while True:\n        await tick()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _infinite_exit_less_loop_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    while True:\n        if done:\n            break\n",
        "def test_foo():\n    while True:\n        if not done:\n            continue\n        break\n",
        (
            "def test_foo():\n"
            "    while True:\n"
            "        try:\n"
            "            work()\n"
            "            break\n"
            "        except Exception:\n"
            "            retry()\n"
        ),
        "def test_foo():\n    while True:\n        if end:\n            return obj\n",
        ("def test_foo():\n    while True:\n        if text[index] == 'X':\n            raise AssertionError('bad')\n"),
        "def test_foo():\n    while condition:\n        step()\n",
        "def test_foo():\n    while not done:\n        step()\n",
        "def test_foo():\n    for x in items:\n        if x:\n            break\n",
        "def test_foo():\n    while True:\n        break\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _infinite_exit_less_loop_violations(tree), f"lens should NOT flag:\n{source}"


# ---------------------------------------------------------------------------
# LENS: fresh random-value draw in an assert (the random twin of fresh-value)
# ---------------------------------------------------------------------------
#: rather than drawing a value, so it is owned by the
#: ``random.reseed-without-monkeypatch`` lens.
_RANDOM_DRAW_FUNCS: frozenset[str] = frozenset(
    {
        "randint",
        "randrange",
        "randbytes",
        "getrandbits",
        "random",
        "uniform",
        "triangular",
        "gauss",
        "betavariate",
        "expovariate",
        "gammavariate",
        "normalvariate",
        "lognormvariate",
        "vonmisesvariate",
        "paretovariate",
        "weibullvariate",
    }
)

#: ``random.<fn>`` drawing functions matched ONLY in the module-qualified
#: spelling (see :data:`_RANDOM_DRAW_FUNCS` — excluded from the bare-name form).
_RANDOM_DRAW_QUALIFIED_ONLY = frozenset({"choice", "choices", "sample", "shuffle"})

_ALL_RANDOM_DRAW_FUNCS: frozenset[str] = _RANDOM_DRAW_FUNCS | _RANDOM_DRAW_QUALIFIED_ONLY


def _is_random_draw_call(node: ast.AST) -> bool:
    """Return True when ``node`` is a call that *draws a fresh random value*.

    Two spellings are recognised: the module-qualified ``random.<fn>(...)``
    call (all drawing functions) and the bare ``from random import <fn>`` twin
    for the subset of drawing names that are never plausible local helpers.
    ``random.seed`` is left alone (global-state mutation, owned by the reseed
    lens), and subsystem-qualified receivers (``rng.randint(...)`` where
    ``rng`` is an injected ``random.Random``/``secrets.SystemRandom``) are left
    alone too — a dedicated instance is the deliberate deterministic form, not
    a fresh-draw hazard.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _ALL_RANDOM_DRAW_FUNCS and isinstance(func.value, ast.Name) and func.value.id == "random"
    if isinstance(func, ast.Name):
        return func.id in _RANDOM_DRAW_FUNCS
    return False


def _random_draw_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose test
    expression — or a single equality-comparison operand — draws a fresh random
    value directly.

    A draw call is the restart-of-the-generator twin of the fresh-value lens:
    ``random.randint(...)``/``random.choice(...)``/``random.sample(...)`` (and
    siblings) return a *new* value on every evaluation, so the assertion's
    outcome is decided at source time, never by the code under test —
    ``assert random.random()`` (a float is always truthy) is a silent false
    green, ``assert not random.choice(lst)`` can never pass,
    ``assert result == random.randint(0, 9)`` ALWAYS FAILS unless the code
    under test happened to draw the same value, and the ``!=`` twin ALWAYS
    PASSES. The expected-value case is the flaky one: a test that should
    compare code output against a *captured* draw instead re-draws at assert
    time, so the result depends on the generator's next value. The fix is the
    same as the fresh-value lens — capture the drawn value in a variable
    first, feed it into the code under test (or the mock), then assert against
    that same bound name. Ordering comparisons (``assert t < random.random()``)
    are deliberate probabilistic bounds and are not flagged, and a draw passed
    *into* a function under test (property-style ``assert median(random.sample(
    items, 5)) in items``) is a legitimate random-input usage, not an expected
    value — only the draw sitting in the assertion's checked position is
    flagged.
    """
    found: list[tuple[int, str]] = []

    def _report(lineno: int, detail: str) -> None:
        found.append(
            (
                lineno,
                f"assert {detail} — a fresh random value is drawn on every evaluation, "
                "so the outcome is decided at source time (capture the draw in a variable first)",
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if _is_random_draw_call(test):
            _report(node.lineno, ast.unparse(test))
            continue
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _is_random_draw_call(test.operand):
            _report(node.lineno, ast.unparse(test))
            continue
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
            for operand in (test.left, test.comparators[0]):
                if _is_random_draw_call(operand):
                    _report(node.lineno, ast.unparse(test))
                    break
    return found


def test_no_random_draw_in_asserts():
    """An ``assert`` that draws a fresh random value directly in its test
    expression is dead code with an outcome fixed at source time (the
    randomness twin of the fresh-value lens): ``assert random.random()`` is a
    silent false green, ``assert not random.choice(lst)`` can never pass,
    and ``assert result == random.randint(0, 9)`` compares code output against
    a value the test itself draws at assert time — it ALWAYS FAILS unless the
    code under test happens to draw the same value, while the ``!=`` twin
    ALWAYS PASSES. The expected-value case is the flaky one: the assertion
    depends on the generator's next value instead of a captured input. Capture
    the drawn value in a variable first, feed it into the code under test (or
    the mock), then assert against that same bound name. Ordering comparisons
    and draws passed into a function under test (property-style checks) are
    deliberately left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _random_draw_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a freshly drawn random value.\n"
        "A random draw (randint/random/choice/sample/...) returns a fresh value on every\n"
        "evaluation, so the outcome is fixed at source time, never by the code under test.\n"
        "Capture the drawn value in a variable first, feed it into the code under test, and\n"
        "assert against that same bound name.\n" + "\n".join(violations)
    )


def test_random_draw_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the random-draw lens: it must
    flag an ``assert`` that draws a random value directly in the test
    expression (bare, ``not``-wrapped, or as an equality-comparison operand, in
    the module-qualified and bare-name spellings) and ignore ordering bounds
    checks, draws fed *into* a call argument (property-style random-test
    inputs), injected ``rng`` instances, ``random.seed`` reseeds, and asserts
    over ordinary values."""
    positive_sources = [
        "import random\ndef test_foo():\n    assert random.random()\n",
        "import random\ndef test_foo():\n    assert not random.choice(lst)\n",
        "import random\ndef test_foo():\n    assert random.randint(0, 9)\n",
        "import random\ndef test_foo():\n    assert result == random.randint(0, 9)\n",
        "import random\ndef test_foo():\n    assert result != random.choice(lst)\n",
        "import random\ndef test_foo():\n    assert random.uniform(0, 1) == other\n",
        "import random\ndef test_foo():\n    assert random.sample(items, 2) != expected\n",
        "from random import randint\ndef test_foo():\n    assert chosen == randint(0, 9)\n",
        "from random import random\ndef test_foo():\n    assert random()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _random_draw_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "import random\ndef test_foo():\n    assert t < random.random()\n",
        "import random\ndef test_foo():\n    assert random.random() < 0.5\n",
        "import random\ndef test_foo():\n    assert len(random.sample(items, 2)) == 2\n",
        "import random\ndef test_foo():\n    roll = random.randint(1, 6)\n    assert result == roll\n",
        "import random\ndef test_foo():\n    rng = random.Random(42)\n    assert rng.randint(0, 9) == 3\n",
        "import random\ndef test_foo():\n    random.seed(7)\n    assert x == 1\n",
        "def test_foo():\n    assert shuffle(deck) == deck\n",
        "def test_foo():\n    assert median(random.sample(items, 5)) in items\n",
        "def test_foo():\n    assert x == 1\n",
        "def test_foo():\n    assert items is not None\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _random_draw_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _stable_dump(node: ast.AST) -> str | None:
    """Return a syntax fingerprint for *pure* expressions — constants, names,
    attribute paths, subscripts, comparisons, and containers of pure elements
    — or ``None`` for expressions the lenses must not judge statically: calls
    (may carry side effects or non-determinism), lambdas, comprehensions, and
    ``await``. Sharing this helper keeps the self-membership / duplicate-membership
    / redundant-boolean-operand / point-range lenses consistent about what an
    "identical expression" means: the same AST shape, limited to operands that
    are safe to assume re-evaluate to the same value."""
    if isinstance(
        node,
        (
            ast.Call,
            ast.Lambda,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
            ast.Await,
        ),
    ):
        return None
    return ast.dump(node, include_attributes=False)


def _self_referential_membership_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose membership
    container literal embeds the (pure) operand itself — ``assert x in [x]``,
    ``assert key not in (key,)``, ``assert item in {item}``, ``assert tag in
    {tag: 'v'}``. A list/tuple/set literal element (or a dict literal *key*,
    which is what ``in`` consults) that is the same expression as the left
    operand matches it under ordinary ``==`` semantics, so ``in`` ALWAYS
    PASSES and ``not in`` ALWAYS FAILS regardless of what the operand
    evaluates to: a silent false green (or unconditionally-red) assertion that
    never exercises a distinct value. This is the membership twin of the
    self-comparison lens, which only owns two-operand comparisons whose
    operands are the same expression — an ``in`` against a container literal
    is a different AST shape it provably misses."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        op = test.ops[0]
        if not isinstance(op, (ast.In, ast.NotIn)):
            continue
        container = test.comparators[0]
        if isinstance(container, (ast.List, ast.Tuple, ast.Set)):
            candidates = container.elts
        elif isinstance(container, ast.Dict):
            candidates = container.keys
        else:
            continue
        left_dump = _stable_dump(test.left)
        if left_dump is None:
            continue
        for el in candidates:
            if el is None:
                continue  # ``{**other}`` unpacking — dynamic keys
            el_dump = _stable_dump(el)
            if el_dump is None or el_dump != left_dump:
                continue
            verdict = "always PASSES" if isinstance(op, ast.In) else "always FAILS"
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — the container literal embeds the operand "
                    f"itself, so the membership verdict is fixed at source time ({verdict}); "
                    "test a genuinely distinct value (or drop the assertion)",
                )
            )
            break
    return found


def test_no_self_referential_membership():
    """A membership check whose container literal contains the same operand it
    is testing — ``assert x in [x]``, ``assert k not in (k,)``,
    ``assert item in {item}``, ``assert tag in {tag: 'v'}`` — is decided at
    source time in ordinary Python semantics: the embedded element equals
    itself, so ``in`` always passes and ``not in`` always fails, never
    exercising the behaviour under test. These are almost always a leftover
    from inlining a value back into a membership literal or from a copy-paste
    that duplicated the left operand into the container. Only the pure
    expressions the self-comparison lens already trusts are judged; a call or
    comprehension operand (``assert f() in [f()]``) is left alone because the
    call may carry side effects or non-determinism."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _self_referential_membership_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} self-referential membership assertion(s).\n"
        "A membership container literal that embeds its own operand has a fixed outcome\n"
        "that can never depend on the value checked. Test a genuinely distinct value\n"
        "(or drop the assertion).\n" + "\n".join(violations)
    )


def test_self_referential_membership_lens_flags_fixed_outcomes():
    """Synthetic positive/negative control for the self-referential-membership
    lens: it must flag every spelling where the (pure) operand re-appears as an
    element of a list/tuple/set literal or a key of a dict literal, and ignore
    distinct elements, empty containers, dynamic containers, ``**``-unpacked
    dicts, call operands, and membership tests against a non-literal."""
    positive_sources = [
        "def test_foo():\n    assert x in [x]\n",
        "def test_foo():\n    assert key not in (key,)\n",
        "def test_foo():\n    assert item in {item}\n",
        "def test_foo():\n    assert tag in {tag: 'v'}\n",
        "def test_foo():\n    assert x in [x, y]\n",
        "def test_foo():\n    assert obj.attr in (obj.attr,)\n",
        "def test_foo():\n    assert row['k'] not in [row['k']]\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _self_referential_membership_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x in [y]\n",
        "def test_foo():\n    assert x not in [y, z]\n",
        "def test_foo():\n    assert 'k' in {'a': 1}\n",
        "def test_foo():\n    assert x in {**d}\n",
        "def test_foo():\n    assert x in [compute()]\n",
        "def test_foo():\n    assert x in y\n",
        "def test_foo():\n    assert f() in [f()]\n",
        "def test_foo():\n    assert x in []\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _self_referential_membership_violations(tree), f"lens should NOT flag:\n{source}"


def _duplicate_membership_literal_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose list/tuple/set
    membership container holds the same (pure) expression more than once —
    ``assert x in (A, A)``, ``assert x not in [1, 1]``. Membership against the
    container behaves identically to the container with that occurrence removed
    (a duplicate element can never be the *first* match where the single one
    was not), so the duplicated element advertises an alternative that does not
    exist — the copy-paste trap the duplicate-parametrize lens guards against
    at the parametrize level, here in a literal membership container. Calls and
    comprehensions are excluded: an identical *call* element may legitimately
    produce distinct values on each evaluation."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], (ast.In, ast.NotIn)):
            continue
        container = test.comparators[0]
        if not isinstance(container, (ast.List, ast.Tuple, ast.Set)):
            continue
        seen: dict[str, ast.AST] = {}
        for el in container.elts:
            el_dump = _stable_dump(el)
            if el_dump is None:
                continue  # dynamic element cannot be judged or compared
            if el_dump in seen:
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(test)} — the container literal repeats "
                        f"{ast.unparse(el)}; membership behaves identically to the single "
                        "occurrence (drop the duplicate case)",
                    )
                )
                break
            seen[el_dump] = el
    return found


def test_no_duplicate_membership_literal_elements():
    """A list/tuple/set literal used as a membership container that repeats the
    same element — ``assert x in (A, A)``, ``assert x not in [1, 1]`` — behaves
    exactly like the container with the duplicate removed, so the duplication is
    pure dead weight: a reader (and a mutation-testing run) believes the check
    considers N alternatives when only N-1 exist. Almost always copy-paste from
    growing the case list. Byte-identical pure expressions are flagged; calls
    and comprehensions are left alone because repeated evaluation may differ."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _duplicate_membership_literal_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} membership container(s) with a duplicate element.\n"
        "Repeating an element inside a membership literal adds no alternative to the check;\n"
        "drop the duplicate case.\n" + "\n".join(violations)
    )


def test_duplicate_membership_literal_lens_flags_repeated_cases():
    """Synthetic positive/negative control for the duplicate-membership-literal
    lens: it must flag every repeat of a pure element inside a list/tuple/set
    membership container (values, names, mixed shapes), and ignore distinct
    elements, empty containers, and repeated *call* elements."""
    positive_sources = [
        "def test_foo():\n    assert x in (A, A)\n",
        "def test_foo():\n    assert x not in [1, 1]\n",
        "def test_foo():\n    assert x in (1, 2, 1)\n",
        "def test_foo():\n    assert x in {'a', 'a'}\n",
        "def test_foo():\n    assert x in (FLAG, FLAG)\n",
        "def test_foo():\n    assert x in [1, 1, 2]\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _duplicate_membership_literal_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x in (1, 2, 3)\n",
        "def test_foo():\n    assert x in ()\n",
        "def test_foo():\n    assert x not in {1, 2}\n",
        "def test_foo():\n    assert x in [f(), f()]\n",
        "def test_foo():\n    assert x in (1, len(y), 1 + 1)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _duplicate_membership_literal_violations(tree), f"lens should NOT flag:\n{source}"


def _redundant_boolean_operand_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose ``and``/``or``
    expression repeats the same (pure) operand — ``assert x and x``,
    ``assert x or x``, ``assert a == b or a == b``. ``x or x`` collapses to
    ``x`` by idempotence and ``x and x`` collapses to ``x`` by absorption, so
    the repeated operand never changes the verdict: the compound is dead weight
    that reports the same outcome a single ``assert x`` would. Calls and
    comprehensions are excluded, and complementary pairs (``x and not x``) are
    owned by the complementary-boolean lens, which this deliberately avoids."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.BoolOp):
            continue
        op_name = "and" if isinstance(test.op, ast.And) else "or"
        seen: dict[str, ast.AST] = {}
        for value in test.values:
            value_dump = _stable_dump(value)
            if value_dump is None:
                continue  # call/comprehension operand cannot be judged statically
            if value_dump in seen:
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(test)} — operand {ast.unparse(value)} repeats earlier "
                        f"in the same {op_name.upper()} conjunction and the compound collapses to "
                        f"a single {op_name} of that operand (drop the duplicate)",
                    )
                )
                break
            seen[value_dump] = value
    return found


def test_no_redundant_boolean_operands():
    """A boolean assertion that repeats the same (pure) operand twice —
    ``assert x and x``, ``assert x or x``, ``assert a == b or a == b`` —
    is absorbed by ``and``/``or`` idempotence into a single ``assert x``, so
    the repeated spelling is dead weight that can never change the verdict.
    It is almost always a copy-paste leftover from composing conditions.
    Identical pure operands are flagged; calls/comprehensions (non-determinism,
    side effects) and the complementary ``x and not x`` shape (owned by the
    complementary-boolean lens) are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _redundant_boolean_operand_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} boolean assertion(s) with a repeated operand.\n"
        "An and/or expression that repeats the same operand collapses to the single operand;\n"
        "drop the duplicate.\n" + "\n".join(violations)
    )


def test_redundant_boolean_operand_lens_flags_absorbent_pairs():
    """Synthetic positive/negative control for the redundant-boolean-operand
    lens: it must flag every ``and``/``or`` that repeats a pure operand (names,
    attributes, comparisons, negations) and ignore distinct operands,
    complementary pairs, and repeated calls."""
    positive_sources = [
        "def test_foo():\n    assert x and x\n",
        "def test_foo():\n    assert x or x\n",
        "def test_foo():\n    assert a == b or a == b\n",
        "def test_foo():\n    assert obj.a and obj.a\n",
        "def test_foo():\n    assert (a == b) and (a == b)\n",
        "def test_foo():\n    assert not x or not x\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _redundant_boolean_operand_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert x and y\n",
        "def test_foo():\n    assert x or y\n",
        "def test_foo():\n    assert f() or f()\n",
        "def test_foo():\n    assert x and not x\n",
        "def test_foo():\n    assert a == b or a != b\n",
        "def test_foo():\n    assert x\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _redundant_boolean_operand_violations(tree), f"lens should NOT flag:\n{source}"


def _point_collapsed_range_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose ordering chain
    (two or more ``<``/``<=``/``>``/``>=`` comparisons) has the same (pure)
    expression as its first and last operands — ``assert lo <= x <= lo``,
    ``assert depth >= n >= depth``. A chain whose endpoints are the same value
    forces every interior operand to equal that endpoint (``lo <= x <= lo``
    only holds when ``x == lo``), so what reads as a range/bounds check is
    degenerate: it cannot span an interval, and the assertion is either a
    needle-eye equality (``lo <= x <= lo``) or unconditionally impossible
    when an interior bound is strict (``lo < x < lo``). Almost always a typo
    for two distinct bounds."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) < 2:
            continue
        if not all(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in test.ops):
            continue
        left_dump = _stable_dump(test.left)
        right_dump = _stable_dump(test.comparators[-1])
        if left_dump is None or left_dump != right_dump:
            continue
        found.append(
            (
                node.lineno,
                f"assert {ast.unparse(test)} — an ordering chain whose first and last operands "
                "are the same expression collapses to a point (every interior value is forced "
                "equal to it) and never spans a range; use two distinct bounds",
            )
        )
    return found


def test_no_point_collapsed_range_chains():
    """An ordering chain whose endpoints are the same expression —
    ``assert lo <= x <= lo`` — collapses to a point: every interior operand is
    forced equal to that endpoint and the "range" never spans an interval, so
    the assertion either can only pass on an exact equality or (with a strict
    interior bound, ``assert lo < x < lo``) can never pass at all — both dead
    at source time. Almost always a typo for two distinct bounds. Only chains
    of ordering operators are judged (the equality-chain lens owns ``==``
    chains), and calls/comprehensions are excluded."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _point_collapsed_range_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} point-collapsed range chain(s).\n"
        "An ordering chain whose first and last operands are the same expression never spans\n"
        "an interval; use two distinct bounds (or assert the equality directly).\n" + "\n".join(violations)
    )


def test_point_collapsed_range_lens_flags_degenerate_bounds():
    """Synthetic positive/negative control for the point-collapsed-range lens:
    it must flag every ordering chain whose endpoints are identical and ignore
    distinct endpoints, single comparisons, ``==``/mixed-operator chains, and
    repeated call operands."""
    positive_sources = [
        "def test_foo():\n    assert lo <= x <= lo\n",
        "def test_foo():\n    assert depth >= row.n >= depth\n",
        "def test_foo():\n    assert a < x < a\n",
        "def test_foo():\n    assert a <= b >= a\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _point_collapsed_range_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert lo <= x <= hi\n",
        "def test_foo():\n    assert a < x < b\n",
        "def test_foo():\n    assert 0 <= x <= 1\n",
        "def test_foo():\n    assert x <= hi\n",
        "def test_foo():\n    assert a <= b == a\n",
        "def test_foo():\n    assert f(a) <= x <= f(a)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _point_collapsed_range_violations(tree), f"lens should NOT flag:\n{source}"


def _duplicate_dict_key_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``dict`` literal that repeats
    the same (pure) key more than once — ``{'a': 1, 'a': 2}``,
    ``{key: 1, key: 2}``, ``{'x': 1, 'y': 2, 'x': 3}``. Python evaluates the
    duplicate keys in source order and silently keeps only the LAST value, so
    the first occurrence is dead data: an expected-value dict, a mock
    ``side_effect`` table, a request payload, or a config overlay carries an
    entry that never applies while a reader believes both are used. Almost
    always copy-paste from editing one case into an existing dict. Byte-
    identical pure keys (constants, names, attribute paths, subscripts —
    the ``_stable_dump`` set) are flagged; call/comprehension keys (may carry
    side effects or non-determinism) and ``**other`` unpacking (dynamic keys)
    are left alone."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: dict[str, int] = {}
        for key in node.keys:
            if key is None:
                continue  # ``{**other}`` unpacking supplies dynamic keys
            key_dump = _stable_dump(key)
            if key_dump is None:
                continue  # dynamic key cannot be judged or compared
            if key_dump in seen:
                found.append(
                    (
                        node.lineno,
                        f"dict literal repeats key {ast.unparse(key)} (first occurrence at line "
                        f"{seen[key_dump]}); Python silently keeps only the last value, so the "
                        "earlier entry is dead data (drop the duplicate key)",
                    )
                )
                break
            seen[key_dump] = node.lineno
    return found


def test_no_duplicate_dict_literal_keys():
    """A ``dict`` literal that repeats the same key — ``{'a': 1, 'a': 2}``,
    ``{key: 1, key: 2}`` — silently drops the first value: Python evaluates
    the keys in order and keeps only the last occurrence, so an expected-value
    dict, a mock ``side_effect`` table, or a request payload carries an entry
    that never applies. When the duplicate sits in the expected value, the dead
    first entry desynchronizes the assertion from its source. This is the
    dict-data twin of the duplicate-membership-element lens (list/tuple/set
    membership containers only); a dict literal is a different AST shape it
    cannot see. Identical pure keys are flagged; calls/comprehensions and
    ``**``-unpacking are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _duplicate_dict_key_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} dict literal(s) with a duplicate key.\n"
        "Python keeps only the last of two identical keys, so the first occurrence is dead\n"
        "data that a reader and a mutation-testing run believe is used; drop the duplicate.\n" + "\n".join(violations)
    )


def test_duplicate_dict_key_lens_flags_dead_first_values():
    """Synthetic positive/negative control for the duplicate-dict-key lens: it
    must flag every dict literal that repeats a pure key (constants, names,
    attribute paths, nested pure values) and ignore distinct keys, dynamic
    ``**``-unpacking, repeated *call* keys, and dict comprehensions."""
    positive_sources = [
        "def test_foo():\n    payload = {'a': 1, 'a': 2}\n",
        "def test_foo():\n    assert get() == {'k': 'v1', 'k': 'v2'}\n",
        "def test_foo():\n    mock.side_effect = {'k': 1, 'k': 2}\n",
        "def test_foo():\n    d = {KEY: 1, KEY: 2}\n",
        "def test_foo():\n    d = {'x': 1, 'y': 2, 'x': 3}\n",
        "def test_foo():\n    cfg = {item.kind: 1, item.kind: 2}\n",
        "def test_foo():\n    d = {'a': {'n': 1}, 'a': {'n': 2}}\n",
        "def test_foo():\n    d = {**base, 'a': 1, 'a': 2}\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _duplicate_dict_key_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    d = {'a': 1, 'b': 2}\n",
        "def test_foo():\n    d = {**other, 'a': 1}\n",
        "def test_foo():\n    d = {f(x): 1, f(x): 2}\n",
        "def test_foo():\n    d = {'a': 1}\n",
        "def test_foo():\n    d = {k: None for k in keys}\n",
        "def test_foo():\n    d = {'a' + 'b': 1, 'ab': 2}\n",
        "def test_foo():\n    return {'x': 1} or {'x': 2}\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _duplicate_dict_key_violations(tree), f"lens should NOT flag:\n{source}"


def _rng_construction_has_seed(call: ast.Call) -> bool:
    """True when an RNG constructor call carries an explicit seed.

    Any positional argument counts as a seed; a ``seed=None`` keyword (or a
    leading literal ``None``) is the same as omitting the seed entirely — the
    OS-entropy path — and does not count."""
    for arg in call.args:
        if isinstance(arg, ast.Constant) and arg.value is None:
            continue
        return True
    for keyword in call.keywords:
        if keyword.arg == "seed":
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                continue
            return True
    return False


def _unseeded_rng_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every random-number generator
    constructed *without a seed* in a test module — ``random.Random()``,
    ``random.Random(seed=None)``, the ``from random import Random`` bare-name
    twin, ``numpy.random.RandomState()``, and ``numpy.random.default_rng()``
    (with ``np`` the alias spelling). An RNG constructed with no explicit seed
    draws its state from OS entropy, so every run produces a DIFFERENT dataset:
    a failing test cannot be re-run with the same inputs, and a mutation-testing
    run observes inputs no real run drew — the failure is unreproducible by
    construction. This is the construction twin of the fresh-random-draw lens
    (a draw standing in an assertion) and the global-reseed lens (the shared
    ``random`` singleton); wherever this suite blesses determinism it blesses
    ``random.Random(N)``, so an unseeded construction defeats that contract.
    The bare ``Random(...)`` spelling is judged only when the module imports the
    name from ``random`` — otherwise it is some other Random the lens cannot
    know. Calls carrying any positional argument or a non-``None`` ``seed=`` are
    seeded by definition and are left alone."""
    from_random_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "random"
        for alias in node.names
    }
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        kind = None
        if isinstance(func, ast.Attribute):
            attr = func.attr
            base = func.value
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                outer, mid = base.value.id, base.attr
                if outer in ("numpy", "np") and mid == "random" and attr in ("RandomState", "default_rng"):
                    kind = attr
            elif isinstance(base, ast.Name) and base.id == "random" and attr == "Random":
                kind = "Random"
        elif isinstance(func, ast.Name) and func.id == "Random" and "Random" in from_random_names:
            kind = "Random"
        if kind is None:
            continue
        if _rng_construction_has_seed(node):
            continue
        found.append(
            (
                node.lineno,
                f"{ast.unparse(node)} constructs a random-number generator without a seed — every "
                "run draws different data, so a failure cannot be reproduced with the same inputs; "
                "pass an explicit seed (e.g. random.Random(0))",
            )
        )
    return found


def test_no_unseeded_rng_construction():
    """A random-number generator constructed without a seed —
    ``random.Random()``, ``random.Random(seed=None)``,
    ``numpy.random.RandomState()``, ``numpy.random.default_rng()`` — draws its
    state from OS entropy, so every run of the test produces a different
    dataset: a failing run cannot be re-run with the same inputs (unreproducible
    by construction), and a mutation-testing run observes inputs no real run
    drew. This is the construction twin of the fresh-random-draw lens (a draw
    standing in an assertion) and the global-reseed lens (the shared ``random``
    singleton); the deterministic form this suite blesses is
    ``random.Random(N)``, so passing an explicit seed restores reproducibility.
    Any positional argument or a non-``None`` ``seed=`` counts as seeded; the
    bare ``Random(...)`` spelling is judged only under a ``from random import
    Random``."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _unseeded_rng_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} unseeded random-number generator construction(s).\n"
        "An RNG constructed without a seed draws fresh entropy every run, so failures are\n"
        "unreproducible and a mutation-testing run observes inputs no real run drew; pass an\n"
        "explicit seed (e.g. random.Random(0)) to pin the dataset.\n" + "\n".join(violations)
    )


def test_unseeded_rng_lens_flags_nondeterministic_constructions():
    """Synthetic positive/negative control for the unseeded-RNG lens: it must
    flag every seedless construction (``random.Random``, the bare ``Random()``
    twin, explicit ``seed=None``, numpy ``RandomState``/``default_rng``, at
    module or function scope) and ignore seeded constructions, calls on an
    already-constructed generator, bare ``Random`` without the from-import,
    and unrelated random reads."""
    positive_sources = [
        "def test_foo():\n    rng = random.Random()\n",
        "def test_foo():\n    import random\n    random.Random()\n",
        "def test_foo():\n    rng = random.Random(seed=None)\n",
        "from random import Random\ndef test_foo():\n    rng = Random()\n",
        "def test_foo():\n    rng = np.random.RandomState()\n",
        "def test_foo():\n    rs = numpy.random.default_rng()\n",
        "import random\nrng = random.Random()\n",
        "def test_foo():\n    from random import Random\n    rng = Random()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _unseeded_rng_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    rng = random.Random(42)\n",
        "def test_foo():\n    rng = random.Random(seed=7)\n",
        "def test_foo():\n    rng = random.Random([1, 2, 3])\n",
        "def test_foo():\n    rng = default_rng(0)\n",
        "def test_foo():\n    rng = np.random.default_rng(seed=1)\n",
        "def test_foo():\n    rng = np.random.RandomState(42)\n",
        "def test_foo():\n    x = random.random()\n",
        "def test_foo():\n    assert rng.random()\n",
        "def test_foo():\n    Random()\n",
        "def test_foo():\n    rng = other.Random()\n",
        "def test_foo():\n    random.seed(42)\n",
        "def test_foo():\n    rng = seeded_factory()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _unseeded_rng_violations(tree), f"lens should NOT flag:\n{source}"


#: Wall-clock reads (``time.<name>()`` attribute spellings) whose subtraction
#: from another value yields an *elapsed* measure. Reads on the left of a
#: subtraction measure elapsed-since-anchor; reads on the right measure
#: remaining-until-deadline. ``time.get_clock_info`` and friends that do not
#: return a clock value are out of scope, as are the ``clock()``/``ticks()``
#: names that do not exist on Python 3.12.
_WALL_CLOCK_READS = frozenset(
    {
        "monotonic",
        "perf_counter",
        "process_time",
        "thread_time",
        "time",
        "monotonic_ns",
        "perf_counter_ns",
        "process_time_ns",
        "thread_time_ns",
    }
)


def _wall_clock_read(node: ast.AST) -> bool:
    """Return True for a fresh ``time.<clock>()`` read (the attribute spelling).

    ``time.monotonic()``/``time.time()``/``time.perf_counter()`` and their
    ``_ns``/``thread_time`` twins. A local helper bound to a different name
    cannot be distinguished statically and is deliberately not matched,
    mirroring the ``wait_for``/``wait`` exclusion in the asyncio lens."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _WALL_CLOCK_READS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
        and not node.args
        and not node.keywords
    )


def _wall_clock_elapsed_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assert whose compare carries a
    wall-clock *elapsed* measure as a top-level operand — a subtraction that
    reads ``time.<clock>()`` on either side: ``assert time.monotonic() - started
    < 1.0``, ``assert deadline - time.time() > 0.1``. The verdict depends on how
    long the suite actually took between two clock reads, so the assertion is a
    wall-clock timing contract that flakes under CI load. Only the direct
    operand shape is judged: a measure buried under a wrapping call
    (``abs(time.monotonic() - start)``) belongs to the same hazard but is out of
    scope, and a bare ordering read (``started < time.monotonic()``) is a
    monotonicity check that cannot flake and is left alone."""
    found: list[tuple[int, str]] = []

    def _is_elapsed(operand: ast.AST) -> bool:
        if not isinstance(operand, ast.BinOp) or not isinstance(operand.op, ast.Sub):
            return False
        return _wall_clock_read(operand.left) or _wall_clock_read(operand.right)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        for operand in (test.left, *test.comparators):
            if not _is_elapsed(operand):
                continue
            clock_side = "left" if _wall_clock_read(operand.left) else "right"
            flavor = "elapsed-since-anchor measure" if clock_side == "left" else "remaining-time measure"
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — a wall-clock {flavor} compared "
                    "against a bound; the verdict depends on how long the suite really took "
                    "between clock reads, so the check flakes under load. Inject the time "
                    "source the code under test reads (a monotonic ``now`` callable) and "
                    "advance it deterministically instead",
                )
            )
            break
    return found


def test_no_wall_clock_elapsed_assertions():
    """An assert that compares a wall-clock *elapsed* measure —
    ``assert time.monotonic() - started < 1.0``, ``assert deadline -
    time.time() > 0.1`` — bakes real elapsed time into the verdict: the test
    passes or fails on how long the suite actually took between two clock reads,
    so it flakes on a loaded runner or a preempted process, and an artificially
    fast run can pass without exercising the slow path it was written to bound.
    This is the assertion twin of the computed-wall-clock-sleep lens. Inject the
    time source the code under test reads and advance it deterministically, or
    compare pinned timestamps instead."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _wall_clock_elapsed_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} wall-clock elapsed assertion(s).\n"
        "An assert on an elapsed duration bakes real wall-clock time into the verdict and\n"
        "flakes under CI load; inject the clock the code under test reads instead.\n" + "\n".join(violations)
    )


def test_wall_clock_elapsed_lens_flags_flaky_durations():
    """Synthetic positive/negative control for the wall-clock-elapsed lens: it
    must flag every compare whose top-level operand is a subtraction reading
    ``time.<clock>()`` on either side (all clock spellings, both elapsed and
    remaining-time shapes), and ignore bare ordering reads, non-``time`` clock
    providers, subtractions without a wall-clock read, and elapsed measures not
    sitting at the top of a compare operand."""
    positive_sources = [
        "def test_foo():\n    assert time.monotonic() - started < 1.0\n",
        "def test_foo():\n    assert d.last_activity() >= time.monotonic() - 1.0\n",
        "def test_foo():\n    assert time.perf_counter() - t0 <= 0.5\n",
        "def test_foo():\n    assert deadline - time.time() > 0.1\n",
        "def test_foo():\n    assert time.process_time_ns() - begin == 0\n",
        "def test_foo():\n    assert time.monotonic() - start >= 2 * timeout\n",
        "def test_foo():\n    assert time.thread_time() - cpu_start < 5\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _wall_clock_elapsed_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert started < time.monotonic()\n",
        "def test_foo():\n    assert start + 5 < time.monotonic()\n",
        "def test_foo():\n    assert clock.now() - started < 1.0\n",
        "def test_foo():\n    assert time.monotonic() < deadline\n",
        "def test_foo():\n    assert elapsed() - started < 1.0\n",
        "def test_foo():\n    assert time.monotonic() + 1.0 < deadline\n",
        "def test_foo():\n    assert x == 5\n",
        "def test_foo():\n    assert (time.monotonic() - started < 1.0) or pending\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _wall_clock_elapsed_violations(tree), f"lens should NOT flag:\n{source}"


def _fresh_value_call_assertion_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every fresh non-deterministic value
    passed as an *expected* argument to a mock call-assertion
    (``assert_called_with``, ``assert_called_once_with``, ``assert_any_call``,
    and their awaited twins).

    Every UUID/secrets-token/wall-clock/``datetime.now()`` call mints a *new*
    value on each evaluation, so the recorded call — whatever the code under
    test actually passed — can never equal the freshly-regenerated expectation:
    the assertion is dead code that always FAILS (and ``assert_any_call`` can
    never match any recorded call either). This is the expected-argument twin
    of the assert-position fresh-value lens in ``_fresh_value_assert_violations``,
    just as ``_fresh_mock_in_call_assertions`` is the expected-argument twin of
    the Mock-constructor lens. Only direct positional/keyword argument
    positions are checked, mirroring the fresh-Mock twin: a fresh value nested
    inside a container or ``call(...)`` wrapper is a less direct shape and is
    deliberately left alone."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _MOCK_CALL_VERIFY_METHODS:
            continue
        for arg in node.args:
            if _is_fresh_value_call(arg):
                found.append(
                    (
                        arg.lineno,
                        f"{ast.unparse(arg)} passed as an expected-call argument to "
                        f"{node.func.attr}() — a fresh non-deterministic value is regenerated on "
                        "every evaluation, so the recorded call can never equal it and the "
                        "assertion always FAILS; capture the value in a variable first, feed it "
                        "into the code under test, and assert against that bound name",
                    )
                )
        for kw in node.keywords:
            if kw.arg and _is_fresh_value_call(kw.value):
                found.append(
                    (
                        kw.value.lineno,
                        f"{kw.arg}={ast.unparse(kw.value)} passed as an expected-call argument to "
                        f"{node.func.attr}() — a fresh non-deterministic value is regenerated on "
                        "every evaluation, so the recorded call can never equal it and the "
                        "assertion always FAILS; capture the value in a variable first, feed it "
                        "into the code under test, and assert against that bound name",
                    )
                )
    return found


def test_no_fresh_value_in_call_assertions():
    """``<mock>.assert_called_with(id=uuid.uuid4())`` (and ``assert_called_once_with``,
    ``assert_any_call``, plus the awaited twins) declares a *fresh* non
    deterministic value as the expected call argument. Every UUID, secrets
    token, ``time.*`` read, and ``datetime.now()`` call returns a value unique
    to that single evaluation, so the recorded call (whatever the code under
    test actually passed) can never equal the re-generated expectation: the
    assertion always FAILS, and an ``assert_any_call`` can never match any
    recorded call either. This is the expected-argument twin of the assert-
    position fresh-value lens, and is almost always a broken attempt to assert
    against a value generated by the test itself at assert time. Capture the
    generated value in a variable first, feed it into the code under test (or
    into the mock), then assert against that same bound name."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _fresh_value_call_assertion_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} fresh non-deterministic value(s) in call-assertion expected "
        "arguments.\n"
        "A fresh UUID/token/wall-clock/datetime.now() value is regenerated on every evaluation, "
        "so the recorded call can never equal it and the assertion ALWAYS FAILS. Capture the\n"
        "value in a variable first, feed it into the code under test (or into the mock), and\n"
        "assert against that bound name.\n" + "\n".join(violations)
    )


def test_fresh_value_call_assertion_lens_flags_impossible_expectations():
    """Synthetic positive/negative control for the fresh-value-in-call-
    assertion lens: it must flag a fresh non-deterministic call in any
    expected-argument position (positional, keyword, sync or awaited method,
    every recognised spelling) and ignore bound names holding a previously
    captured value, non-assertion mock calls, fresh values nested inside
    container/call wrappers, and fresh values anywhere outside the verify
    methods."""
    positive_sources = [
        "def test_foo():\n    mock.assert_called_with(request_id=uuid.uuid4())\n",
        "def test_foo():\n    mock.assert_called_once_with(uuid4())\n",
        "def test_foo():\n    mock.assert_any_call(token_hex(), 'header')\n",
        "def test_foo():\n    mock_async.assert_awaited_with(event_time=datetime.now(UTC))\n",
        "def test_foo():\n    mock_async.assert_awaited_once_with(datetime.utcnow())\n",
        "def test_foo():\n    mock_async.assert_awaited_any_call(secrets.token_urlsafe())\n",
        "def test_foo():\n    mock.assert_called_with(time.monotonic())\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _fresh_value_call_assertion_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    expected = uuid.uuid4()\n    mock.assert_called_with(request_id=expected)\n",
        "def test_foo():\n    mock.assert_called_with(request_id=request.id)\n",
        "def test_foo():\n    mock.assert_called()\n",
        "def test_foo():\n    mock.assert_called_once()\n",
        "def test_foo():\n    mock.assert_not_called()\n",
        "def test_foo():\n    mock.assert_called_once_with(request_id=ANY)\n",
        "def test_foo():\n    mock.assert_called_with({'id': uuid.uuid4()})\n",
        "def test_foo():\n    mock.assert_called_with(call(uuid.uuid4()))\n",
        "def test_foo():\n    result_id = uuid.uuid4()\n    mock.assert_called_with(result_id)\n",
        "def test_foo():\n    assert mock.call_args == uuid.UUID(result_id)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _fresh_value_call_assertion_violations(tree), f"lens should NOT flag:\n{source}"


#: Zero-argument builtin calls that always produce an empty container for which
#: ``in``/``not in`` membership is statically dead. ``bytes()`` is deliberately
#: excluded: bytes ``in`` uses *substring* semantics, so ``b'' in bytes()`` is
#: TRUE rather than False, and the ``in b""`` literal spelling is already owned
#: by the empty-bytes tautology lens.
_EMPTY_MEMBERSHIP_BUILTINS = frozenset({"list", "dict", "set", "tuple", "frozenset", "bytearray"})


def _empty_builtin_call_membership_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``in``/``not in`` comparison
    whose container operand is an empty container produced by a zero-argument
    builtin call (``set()``/``list()``/``dict()``/``tuple()``/``frozenset()``/
    ``bytearray()``).

    An empty container can never contain anything, so ``in`` ALWAYS FAILS and
    ``not in`` ALWAYS PASSES regardless of what the other operand evaluates to.
    This is the membership twin of the ``== set()``/``== list()`` equality lens
    (``_empty_builtin_call_comparisons``) and the call-based twin of the
    empty-container *literal* membership lens (``_empty_container_membership_
    tautologies``). A literal other-side is owned by the literal-comparison
    lens, and a bare name is left alone exactly as in the sibling lenses."""
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
            if not (
                isinstance(container, ast.Call)
                and isinstance(container.func, ast.Name)
                and container.func.id in _EMPTY_MEMBERSHIP_BUILTINS
                and not container.args
                and not container.keywords
            ):
                continue
            if isinstance(operand, ast.Constant):
                continue
            op_name = "in" if isinstance(test.ops[0], ast.In) else "not in"
            verdict = "always FAILS" if isinstance(test.ops[0], ast.In) else "always PASSES"
            found.append(
                (
                    node.lineno,
                    f"asserts value {op_name} {container.func.id}() — {verdict} "
                    "(the zero-argument builtin always yields an empty container, which never "
                    "contains anything)",
                )
            )
            break
    return found


def test_no_empty_builtin_call_membership():
    """``assert x in set()`` / ``assert x not in list()`` compare membership
    against an empty container produced by a zero-argument builtin call — the
    call-based twin of the ``in []``/``not in {}`` literal lens. Every such
    builtin returns an empty container, and an empty container can never
    contain anything, so ``in`` always FAILS and ``not in`` always PASSES, no
    matter what ``x`` evaluates to: the assertion is dead code either way. This
    is the membership twin of the ``== set()``/``== list()`` equality lens.
    ``bytes()`` is excluded because bytes ``in`` uses substring semantics
    (``b'' in bytes()`` is TRUE, not dead), and a bare-name other operand is
    left alone exactly as in the sibling lenses."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _empty_builtin_call_membership_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} empty-builtin-call membership assertion(s).\n"
        "A zero-argument set()/list()/dict()/tuple()/frozenset()/bytearray() is always empty, so\n"
        "its membership check is dead: 'in' always FAILS and 'not in' always PASSES. Assert the\n"
        "membership (or emptiness) you actually mean on the non-empty container, or drop the\n"
        "check entirely.\n" + "\n".join(violations)
    )


def test_empty_builtin_call_membership_lens_flags_impossible_membership():
    """Synthetic positive/negative control for the empty-builtin-call membership
    lens: it must flag ``in``/``not in`` against a zero-argument
    ``set()``/``list()``/``dict()``/``tuple()``/``frozenset()``/``bytearray()``
    (either operand order) and ignore ``bytes()`` (substring semantics),
    non-empty builtin calls, bound-named containers, literal containers owned
    by sibling lenses, and literal-vs-literal membership."""
    positive_sources = [
        "def test_foo():\n    assert value in set()\n",
        "def test_foo():\n    assert value not in list()\n",
        "def test_foo():\n    assert value in dict()\n",
        "def test_foo():\n    assert value not in tuple()\n",
        "def test_foo():\n    assert frozenset() in values\n",
        "def test_foo():\n    assert value in bytearray()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _empty_builtin_call_membership_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert needle in b''\n",
        "def test_foo():\n    assert value in bytes()\n",
        "def test_foo():\n    assert value not in bytes()\n",
        "def test_foo():\n    assert value in set([1, 2])\n",
        "def test_foo():\n    assert value in {'a': 1}\n",
        "def test_foo():\n    assert value in []\n",
        "def test_foo():\n    seen = set()\n    assert value in seen\n",
        "def test_foo():\n    assert 1 in []\n",
        "def test_foo():\n    assert item in load_set()\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _empty_builtin_call_membership_violations(tree), f"lens should NOT flag:\n{source}"


def _fresh_value_container_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` comparison whose
    operand is a container literal that nests a freshly minted non-deterministic
    value — a UUID/secrets token/wall-clock read/``datetime.now()`` call.

    Every evaluation re-mints the nested call, so the freshly-constructed
    container can never equal the one the code under test produced and stored
    earlier: ``assert result == {'id': uuid.uuid4()}`` ALWAYS FAILS and
    ``assert result != [token_hex()]`` ALWAYS PASSES no matter what ``result``
    evaluates to. This is the container-nested twin of the assert-position
    fresh-value lens (``_fresh_value_assert_violations``), which owns only the
    direct bare/``not``/single-comparison-operand positions — a fresh call
    buried inside a list/dict/tuple/set literal is a different ``ast`` shape it
    provably misses, exactly the gap the container-nested Mock lens closes for
    ``Mock()`` constructors. Only *direct* container members are considered (a
    fresh call hidden behind a ``call(...)`` wrapper, a subscript, or a nested
    function call is a less direct shape and is left alone, mirroring its Mock
    sibling). For ``in``/``not in`` the verdict is fixed only when *every*
    membership candidate (element for list/tuple/set, key for dict) of a
    non-empty literal container is a fresh call — then no value the code under
    test produced can ever match. Mixed containers, ``**``-spread dicts,
    ``*``-starred sequences, and fresh values in a dict *value* slot (``in``
    never consults values) all leave the verdict runtime-dependent and are
    deliberately left alone."""
    found: list[tuple[int, str]] = []

    def _container_direct_fresh_value(container: ast.AST) -> ast.Call | None:
        """Return the first fresh-value call among a container literal's direct
        members (elements, or both keys and values for a dict), else None."""
        if isinstance(container, ast.Dict):
            candidates = [k for k in container.keys if k is not None] + list(container.values)
        else:
            candidates = list(container.elts)
        for candidate in candidates:
            if _is_non_deterministic_fresh_call(candidate):
                return candidate
        return None

    def _contains_direct_fresh_value(expr: ast.AST) -> ast.Call | None:
        """Return the first fresh-value call nested directly inside any container
        literal findable within ``expr`` — WITHOUT descending through Call/
        Subscript/comprehension boundaries, so a fresh value buried inside a
        ``call(...)`` wrapper (e.g. ``load({'id': uuid.uuid4()})``) is left
        alone, as documented."""
        for container in _walk_operand(expr):
            if not isinstance(container, (ast.List, ast.Dict, ast.Tuple, ast.Set)):
                continue
            fresh = _container_direct_fresh_value(container)
            if fresh is not None:
                return fresh
        return None

    def _membership_only_fresh_candidates(expr: ast.AST) -> bool:
        """True when ``expr`` is a non-empty list/tuple/set/dict literal whose
        *every* membership candidate (element, or dict key) is a non-
        deterministic fresh-value call — so no value the code under test produced
        can ever match. Deterministic name-based UUIDs (``uuid3``/``uuid5``) are
        excluded: they return the same value every call, so the verdict is
        satisfiable and must not be flagged as always-failing."""
        if isinstance(expr, ast.Dict):
            if not expr.keys or any(key is None for key in expr.keys):
                return False
            return all(_is_non_deterministic_fresh_call(key) for key in expr.keys)
        if not isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            return False
        if not expr.elts or any(isinstance(elt, ast.Starred) for elt in expr.elts):
            return False
        return all(_is_non_deterministic_fresh_call(elt) for elt in expr.elts)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        op = test.ops[0]
        if isinstance(op, (ast.Eq, ast.NotEq)):
            fresh = None
            for side in (test.left, test.comparators[0]):
                fresh = _contains_direct_fresh_value(side)
                if fresh is not None:
                    break
            if fresh is None:
                continue
            op_name = "==" if isinstance(op, ast.Eq) else "!="
            verdict = "always FAILS" if isinstance(op, ast.Eq) else "always PASSES"
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — the container literal re-mints "
                    f"{ast.unparse(fresh)} on every evaluation, so the freshly built container "
                    f"can never equal the one the code under test produced and {op_name} "
                    f"{verdict}; capture the fresh value in a variable first and compare "
                    "against that bound name",
                )
            )
        elif isinstance(op, (ast.In, ast.NotIn)):
            container = None
            for candidate in (test.left, test.comparators[0]):
                if _membership_only_fresh_candidates(candidate):
                    container = candidate
                    break
            if container is None:
                continue
            op_name = "in" if isinstance(op, ast.In) else "not in"
            verdict = "always FAILS" if isinstance(op, ast.In) else "always PASSES"
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — every membership candidate of the container "
                    f"literal is a fresh non-deterministic value, so no value the code under "
                    f"test produced can match and {op_name} {verdict}; assert against a "
                    "captured bound name instead",
                )
            )
    return found


def test_no_fresh_value_in_container_asserts():
    """``assert result == {'id': uuid.uuid4()}`` (and the ``!=``/``in``/``not in``
    twins, either operand order) nests a fresh non-deterministic value inside a
    container literal that is a comparison operand. Every evaluation re-mints the
    UUID/token/wall-clock/``datetime.now()`` call, so the freshly built container
    can never equal the one the code under test produced and stored:
    ``==`` ALWAYS FAILS, ``!=`` ALWAYS PASSES, and membership against a container
    whose *every* candidate is a fresh call can never match, so ``in`` ALWAYS
    FAILS and ``not in`` ALWAYS PASSES. This is the container-nested twin of the
    assert-position fresh-value lens (which owns only direct positions, exactly
    as the container-nested Mock lens complements the Mock-constructor lens).
    Capture the generated value in a variable first, feed it into the code under
    test, and compare against that same bound name."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _fresh_value_container_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} container-nested fresh value assertion(s).\n"
        "A fresh UUID/token/wall-clock/datetime.now() call nested inside a container literal is\n"
        "re-minted on every evaluation, so the freshly built container can never equal the one the\n"
        "code under test produced: == always FAILS, != always PASSES, and all-fresh membership\n"
        "always FAILS/PASSES. Capture the value in a variable first, feed it into the code under\n"
        "test, and assert against that bound name.\n" + "\n".join(violations)
    )


def test_fresh_value_container_lens_flags_nested_fresh_values():
    """Synthetic positive/negative control for the container-nested fresh-value
    lens: it must flag an ``assert`` comparison that nests a fresh non-
    deterministic call inside a list/dict/tuple/set literal in either equality
    operand order, plus full-fresh membership containers, and ignore the direct
    positions owned by the fresh-value lens (bare/``not``/single-comparison-
    operand), bound names, mixed/``*``/``**``-spread membership containers,
    fresh values in a dict *value* slot under ``in``, Mock constructors, and
    call-assertion expected arguments."""
    positive_sources = [
        "def test_foo():\n    assert result == {'id': uuid.uuid4()}\n",
        "def test_foo():\n    assert result != [token_hex()]\n",
        "def test_foo():\n    assert result == [datetime.now(UTC)]\n",
        "def test_foo():\n    assert {'ts': time.time()} == result\n",
        "def test_foo():\n    assert result == [uuid.uuid4(), 'fallback']\n",
        "def test_foo():\n    assert result in (time.monotonic(),)\n",
        "def test_foo():\n    assert result not in {secrets.token_urlsafe()}\n",
        "def test_foo():\n    assert result not in [token_bytes(16), uuid.uuid1()]\n",
        "def test_foo():\n    assert [uuid.uuid4()] in result\n",
        "def test_foo():\n    assert result == {'payload': {'id': uuid.uuid4()}}\n",
        "def test_foo():\n    assert result == {**base, 'id': uuid.uuid4()}\n",
        "def test_foo():\n    assert result != [uuid.uuid4()]\n",
        "def test_foo():\n    assert result == [time.perf_counter(), time.process_time()]\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _fresh_value_container_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert result == uuid.uuid4()\n",
        "def test_foo():\n    assert uuid.uuid4()\n",
        "def test_foo():\n    assert not uuid.uuid4()\n",
        "def test_foo():\n    assert result == {'id': expected_id}\n",
        "def test_foo():\n    assert result == {'id': 'abc'}\n",
        "def test_foo():\n    assert uuid.uuid4() in keys\n",
        "def test_foo():\n    assert result in [uuid.uuid4(), 'fallback']\n",
        "def test_foo():\n    assert result in {'k': uuid.uuid4()}\n",
        "def test_foo():\n    assert result in []\n",
        "def test_foo():\n    assert result == [Mock()]\n",
        "def test_foo():\n    assert result == [1, 2]\n",
        "def test_foo():\n    assert result in [*items, uuid.uuid4()]\n",
        "def test_foo():\n    assert result in {**mapping, uuid.uuid4(): 1}\n",
        "def test_foo():\n    assert x < time.monotonic()\n",
        "def test_foo():\n    assert result == call(uuid.uuid4())\n",
        "def test_foo():\n    mock.assert_called_with({'id': uuid.uuid4()})\n",
        "def test_foo():\n    assert result == {'a': 1, 'b': 2}\n",
        "def test_foo():\n    assert load({'id': uuid.uuid4()}) == expected\n",
        "def test_foo():\n    assert get({'k': uuid.uuid4()}) != result\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _fresh_value_container_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _random_draw_call_assertion_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every fresh *random-value draw*
    passed as an *expected* argument to a mock call-assertion
    (``assert_called_with``, ``assert_called_once_with``, ``assert_any_call``,
    and their awaited twins).

    Every ``random.<fn>`` draw returns a *new* value on each evaluation, so the
    recorded call — whatever the code under test actually passed — can never
    equal the re-drawn expectation: the assertion is dead code that always FAILS
    (and ``assert_any_call`` can never match any recorded call either). This is
    the expected-argument twin of the assert-position random-draw lens in
    ``_random_draw_assert_violations``, just as ``_fresh_value_call_assertion_
    violations`` is the expected-argument twin of the fresh-value lens. Only
    *direct* positional/keyword argument positions are checked, mirroring the
    fresh-value twin: a draw nested inside a container or ``call(...)`` wrapper
    is a less direct shape and is deliberately left alone."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _MOCK_CALL_VERIFY_METHODS:
            continue
        for arg in node.args:
            if _is_random_draw_call(arg):
                found.append(
                    (
                        arg.lineno,
                        f"{ast.unparse(arg)} passed as an expected-call argument to "
                        f"{node.func.attr}() — a fresh random value is drawn on every evaluation, "
                        "so the recorded call can never equal it and the assertion always FAILS; "
                        "capture the drawn value in a variable first, feed it into the code under "
                        "test, and assert against that bound name",
                    )
                )
        for kw in node.keywords:
            if kw.arg and _is_random_draw_call(kw.value):
                found.append(
                    (
                        kw.value.lineno,
                        f"{kw.arg}={ast.unparse(kw.value)} passed as an expected-call argument to "
                        f"{node.func.attr}() — a fresh random value is drawn on every evaluation, "
                        "so the recorded call can never equal it and the assertion always FAILS; "
                        "capture the drawn value in a variable first, feed it into the code under "
                        "test, and assert against that bound name",
                    )
                )
    return found


def test_no_random_draw_in_call_assertions():
    """``<mock>.assert_called_with(random.randint(0, 9))`` (and ``assert_called_once_with``,
    ``assert_any_call``, plus the awaited twins) declares a fresh *random-value
    draw* as the expected call argument — the expected-argument twin of the
    random-draw lens, in the same relationship the fresh-value-in-call-assertion
    lens holds to the fresh-value lens. Every ``random.<fn>`` draw returns a new
    value on each evaluation, so the recorded call (whatever the code under test
    actually passed) can never equal the re-drawn expectation: the assertion
    always FAILS, and an ``assert_any_call`` can never match any recorded call
    either. These are the flaky expected-value variant of comparing code output
    against a value the test itself draws at verify time. Capture the drawn value
    in a variable first, feed it into the code under test (or the mock), then
    pass that same bound name."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _random_draw_call_assertion_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} random draw(s) in call-assertion expected arguments.\n"
        "A random draw (randint/random/choice/sample/...) returns a fresh value on every\n"
        "evaluation, so the recorded call can never equal it and the assertion ALWAYS FAILS.\n"
        "Capture the drawn value in a variable first, feed it into the code under test (or the\n"
        "mock), and assert against that bound name.\n" + "\n".join(violations)
    )


def test_random_draw_call_assertion_lens_flags_flaky_expectations():
    """Synthetic positive/negative control for the random-draw-in-call-assertion
    lens: it must flag a random draw in any expected-argument position
    (positional, keyword, sync or awaited method, module-qualified or bare-name
    spelling) and ignore bound names holding a previously captured draw, injected
    ``rng`` instances, non-assertion mock calls, draws nested inside container/
    call wrappers, and draws anywhere outside the verify methods."""
    positive_sources = [
        "def test_foo():\n    mock.assert_called_with(random.randint(0, 9))\n",
        "def test_foo():\n    mock.assert_called_once_with(random.randint(1, 6))\n",
        "def test_foo():\n    mock.assert_any_call(random.random())\n",
        "def test_foo():\n    mock_async.assert_awaited_with(payload=random.uniform(0, 1))\n",
        "def test_foo():\n    mock_async.assert_awaited_once_with(random.sample(items, 2))\n",
        "def test_foo():\n    mock_async.assert_awaited_any_call(random.choice(names))\n",
        "def test_foo():\n    mocker.thing.assert_called_once_with(random.getrandbits(32))\n",
        "def test_foo():\n    mock.assert_called_with(limit=random.randrange(10))\n",
        "from random import randint\ndef test_foo():\n    mock.assert_called_with(randint(0, 9))\n",
        "from random import random\ndef test_foo():\n    mock.assert_called_with(random())\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _random_draw_call_assertion_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    roll = random.randint(1, 6)\n    mock.assert_called_with(roll)\n",
        "def test_foo():\n    mock.assert_called_with(drawn_value)\n",
        "def test_foo():\n    mock.assert_called_with(ANY)\n",
        "def test_foo():\n    rng = random.Random(42)\n    mock.assert_called_with(rng.randint(0, 9))\n",
        "def test_foo():\n    mock.assert_called_with(request.get('retries'))\n",
        "def test_foo():\n    mock.assert_called()\n",
        "def test_foo():\n    mock.assert_not_called()\n",
        "def test_foo():\n    mock.assert_called_with({'payload': random.randint(0, 9)})\n",
        "def test_foo():\n    mock.assert_called_with(call(random.randint(0, 9)))\n",
        "def test_foo():\n    assert result == random.randint(0, 9)\n",
        "def test_foo():\n    random.seed(7)\n",
        "def test_foo():\n    mock.assert_called_with(random_seed)\n",
        "def test_foo():\n    mock.assert_called_with([1, 2, 3])\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _random_draw_call_assertion_violations(tree), f"lens should NOT flag:\n{source}"


#: Iterator-producing builtin names. Unlike list/dict/set/tuple (which have a
#: real truthiness/length contract) and ``range`` (which implements ``__len__``
#: so ``bool(range(0))`` is False), the object these calls return is a lazy
#: iterator with neither ``__bool__`` nor ``__len__``, so it is ALWAYS truthy no
#: matter how many items it will eventually produce (including zero).
_ITERATOR_PRODUCING_BUILTINS = frozenset({"map", "filter", "zip", "iter", "reversed", "enumerate"})


def _iterator_object(node: ast.AST) -> str | None:
    """Return a short label for an expression that is statically an iterator
    object, or ``None``.

    Two shapes are recognised: a generator expression (``ast.GeneratorExp`` —
    the body never runs until the first ``next()``, so even a generator that
    yields nothing is a real object) and an iterator-producing builtin call in
    the ``map``/``filter``/``zip``/``iter``/``reversed``/``enumerate`` set. Only
    the bare builtin-name spelling is matched: an attribute spelling such as
    ``df.map`` or ``conn.iter`` is a method with an unknown return type and is
    deliberately left alone, mirroring the bare-name discipline of the
    random-draw lens."""
    if isinstance(node, ast.GeneratorExp):
        return "generator expression"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ITERATOR_PRODUCING_BUILTINS:
        return f"{node.func.id}() iterator"
    return None


def _fresh_container_literal(node: ast.AST) -> bool:
    """True for a list/tuple/set/dict literal with no ``*``/``**`` unpacking.

    A starred element (``[*x]`` / ``{**d}``) makes the literal dynamic by
    nature, mirroring how the container-literal lenses exclude unpacked
    shapes."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not any(isinstance(elt, ast.Starred) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return not any(key is None for key in node.keys)
    return False


def _iterator_object_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``assert`` whose operand is
    a freshly-built iterator object.

    Two shapes are owned:

    - *truthiness*: the assert's test (possibly ``not``-wrapped) is a generator
      expression or an iterator-producing builtin call. The object is ALWAYS
      truthy, so ``assert <iterator>`` always PASSES (a silent false green when
      the code under test produced an empty iterator) and ``assert not
      <iterator>`` always FAILS.
    - *equality*: an ``==``/``!=`` comparison where one side is a fresh iterator
      object and the other side is a freshly-allocated container literal
      (``map(...) == [...]`` always FAILS; ``!=`` always PASSES), or where both
      sides are fresh iterator objects (always FAILS for ``==``, always PASSES
      for ``!=`` — iterators compare by identity, so two freshly-constructed
      ones can never be equal).

    A comparison against a *name* is left alone: the name may hold a mock whose
    ``__eq__`` always returns a truthy sentinel, so the outcome is not provable
    statically (the mock-equality shapes are owned by the mock lenses), and two
    *syntactically identical* iterator calls are left alone — that is the
    self-comparison lens's determinism-check territory."""
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            label = _iterator_object(test.operand)
            if label is None:
                continue
            found.append(
                (
                    node.lineno,
                    f"assert not {ast.unparse(test.operand)} — a {label} is ALWAYS truthy "
                    "(iterator objects have no __bool__/__len__), so the assert ALWAYS FAILS; "
                    "consume the iterator (list(...)/next(...)) or reduce it (any(...)/all(...)) "
                    "and assert on its contents",
                )
            )
            continue
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
            left = _iterator_object(test.left)
            right = _iterator_object(test.comparators[0])
            if not (left or right):
                continue
            if left and right:
                if ast.unparse(test.left) == ast.unparse(test.comparators[0]):
                    continue
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(test)} — two freshly-constructed iterator objects "
                        "compare by identity, so they can never be equal: == ALWAYS FAILS / != "
                        "ALWAYS PASSES regardless of the data; materialize or reduce the iterators "
                        "(list(...)/any(...)) before comparing",
                    )
                )
                continue
            if left and _fresh_container_literal(test.comparators[0]):
                other = ast.unparse(test.comparators[0])
            elif right and _fresh_container_literal(test.left):
                other = ast.unparse(test.left)
            else:
                continue
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — a {left or right} can never compare equal to the "
                    f"freshly-allocated {other}: == ALWAYS FAILS / != ALWAYS PASSES no matter what "
                    "the code under test produced; materialize the iterator "
                    "(list(...)/tuple(...)/sorted(...)) before comparing",
                )
            )
            continue
        label = _iterator_object(test)
        if label is None:
            continue
        found.append(
            (
                node.lineno,
                f"assert {ast.unparse(test)} — a {label} is ALWAYS truthy (iterator objects have no "
                "__bool__/__len__), so the assert always PASSES even when the code under test "
                "produced an empty result (silent false green); materialize the iterator "
                "(list(...)/next(...)) or reduce it (any(...)/all(...)) before asserting",
            )
        )
    return found


def test_no_iterator_object_asserts():
    """An ``assert`` whose operand is a freshly-built iterator object — a
    generator expression (``assert (x for x in results)``) or an
    iterator-producing builtin call (``map``/``filter``/``zip``/``iter``/
    ``reversed``/``enumerate``) — is dead code: the object is ALWAYS truthy no
    matter what it will produce, so ``assert <iterator>`` always PASSES (a
    silent false green when the code under test produced an empty result) and
    ``assert not <iterator>`` always FAILS, while ``assert <iterator> ==
    [...]`` always FAILS (a fresh iterator can never equal a freshly-allocated
    container literal) and ``assert <iterator> != [...]`` always PASSES. These
    are almost always a forgotten materialization — ``list(map(...))``,
    ``any(x for x in y)`` — and are the lazy-iterator twin of the
    container-literal-truthiness lens.
    """
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _iterator_object_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assertion(s) against a freshly-built iterator object.\n"
        "An iterator object is ALWAYS truthy (no __bool__/__len__), and can never compare equal to\n"
        "a freshly-allocated container literal, so the verdict is fixed at source time.\n"
        "Materialize or reduce the iterator (list(...)/any(...)/all(...)/next(...)) first.\n" + "\n".join(violations)
    )


def test_iterator_object_lens_flags_dead_asserts():
    """Synthetic positive/negative control for the iterator-object lens: it must
    flag a generator expression or iterator-producing builtin call standing as
    the assertion (bare, ``not``-wrapped, or compared against a container
    literal / another fresh iterator) and ignore materialized calls
    (``list(...)``/``any(...)``/``sorted(...)``), container comprehensions
    (which have real truthiness), ``range``, attribute spellings, and compares
    against a name."""
    positive_sources = [
        "def test_foo():\n    assert (x for x in items)\n",
        "def test_foo():\n    assert (item.name for item in rows)\n",
        "def test_foo():\n    assert not (x for x in items)\n",
        "def test_foo():\n    assert map(str, items)\n",
        "def test_foo():\n    assert filter(None, items)\n",
        "def test_foo():\n    assert zip(a, b)\n",
        "def test_foo():\n    assert not iter(items[0])\n",
        "def test_foo():\n    assert reversed(items)\n",
        "def test_foo():\n    assert enumerate(sorted_keys)\n",
        "def test_foo():\n    assert map(str, items) == ['a', 'b']\n",
        "def test_foo():\n    assert ['a', 'b'] != filter(None, items)\n",
        "def test_foo():\n    assert {'k': 'v'} == filter(None, items)\n",
        "def test_foo():\n    assert map(f, a) == filter(g, b)\n",
        "def test_foo():\n    assert (x for x in a) != (y for y in b)\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _iterator_object_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert list(map(str, items))\n",
        "def test_foo():\n    assert any(x for x in items)\n",
        "def test_foo():\n    assert all(flag for flag in flags)\n",
        "def test_foo():\n    assert [x for x in items]\n",
        "def test_foo():\n    assert {x for x in items}\n",
        "def test_foo():\n    assert {k: v for k, v in pairs}\n",
        "def test_foo():\n    assert sorted(map(str, items)) == ['a']\n",
        "def test_foo():\n    assert range(5)\n",
        "def test_foo():\n    assert not range(0)\n",
        "def test_foo():\n    assert len(iter(x)) == 0\n",
        "def test_foo():\n    assert next(iter(items)) == 7\n",
        "def test_foo():\n    assert result == stored_map\n",
        "def test_foo():\n    assert map(str, items) != stored\n",
        "def test_foo():\n    assert reporter.map(str, items)\n",
        "def test_foo():\n    assert mapper(str, items)\n",
        "def test_foo():\n    assert map(str, x) == map(str, x)\n",
        "def test_foo():\n    assert x == map_result\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _iterator_object_assert_violations(tree), f"lens should NOT flag:\n{source}"


def _type_equality_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``==``/``!=`` comparison
    whose operand is a single-argument ``type(...)`` call.

    ``type(x) == X`` checks the *exact* runtime class with equality semantics,
    so an instance of a subclass of ``X`` silently fails the check even though
    ``isinstance(x, X)`` is what such a test almost always wants, and a mocked
    or replaced class defeats the check outright — equality on a class object
    is the unidiomatic-typecheck spelling of ``type(x) is X``. ``is``/``is
    not`` on a class object is identity-safe (the blessed exact-type spelling,
    mirroring how the None lens blesses ``is None`` over ``== None``). Only the
    builtin ``type`` name with a single argument is matched — the
    three-argument ``type(name, bases, ns)`` form *creates* a class and is left
    alone. An identical ``type(x) == type(x)`` comparison is a tautology and is
    flagged separately."""
    found: list[tuple[int, str]] = []

    def _is_type_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "type"
            and len(node.args) == 1
            and not node.keywords
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op = node.ops[0]
        if not isinstance(op, (ast.Eq, ast.NotEq)):
            continue
        op_name = "==" if isinstance(op, ast.Eq) else "!="
        for side in (node.left, *node.comparators):
            if not _is_type_call(side):
                continue
            other = node.comparators[0] if side is node.left else node.left
            if _is_type_call(other):
                if ast.dump(side, include_attributes=False) == ast.dump(other, include_attributes=False):
                    found.append(
                        (
                            node.lineno,
                            f"assert {ast.unparse(node)} — two identical type() calls always compare "
                            "equal (a tautology that passes no matter how broken the code under test is); "
                            "assert against a real expected type",
                        )
                    )
                else:
                    found.append(
                        (
                            node.lineno,
                            f"assert {ast.unparse(node)} — compares the exact type of two values with "
                            f"{op_name}; a subclass instance fails silently and a mocked class defeats "
                            "the check, prefer 'type(a) is type(b)' (exact-type identity) or "
                            "isinstance(a, type(b)) (subclass-aware)",
                        )
                    )
            else:
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(node)} — compares {op_name} the exact type via type(); "
                        "an instance of a subclass of the expected class fails silently and a mocked "
                        "class defeats the check, prefer isinstance(x, X) (subclass-aware) or "
                        "'type(x) is X' (exact type)",
                    )
                )
            break
    return found


def test_no_type_equality_comparisons():
    """``type(x) == X`` / ``type(x) != X`` compare the *exact* runtime class
    with equality semantics, so an instance of a subclass of ``X`` silently
    fails the check and a mocked or replaced class defeats it outright — the
    unidiomatic typecheck spelling where ``isinstance(x, X)`` (subclass-aware)
    or the identity-safe ``type(x) is X`` (exact type) was intended. ``is``/``is
    not`` are the blessed spellings and left alone, as is the three-argument
    ``type(name, bases, ns)`` class-creation form."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _type_equality_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} type() equality comparison(s).\n"
        "type(x) == X compares the exact runtime class with equality semantics, so a subclass\n"
        "instance fails silently and a mocked class defeats the check. Use isinstance(x, X) for\n"
        "subclass-aware checks, or the identity-safe 'type(x) is X' for an exact-type check.\n" + "\n".join(violations)
    )


def test_type_equality_lens_flags_fragile_exact_type_checks():
    """Synthetic positive/negative control for the type-equality lens: it must
    flag ``==``/``!=`` on a one-argument ``type(...)`` call (either operand
    order, ``type(a) == type(b)`` included, in assert or helper code) and ignore
    the ``is``/``is not`` exact-type spellings, ``isinstance``, ``__class__``
    identity, the three-argument class-creation form, subscripts/attributes, and
    comparisons against a captured type name."""
    positive_sources = [
        "def test_foo():\n    assert type(err) == ValueError\n",
        "def test_foo():\n    assert type(result) != SomeModel\n",
        "def test_foo():\n    assert ValueError == type(err)\n",
        "def test_foo():\n    assert SomeModel != type(result)\n",
        "def test_foo():\n    assert type(a) == type(b)\n",
        "def test_foo():\n    assert type(model) != models.BaseModel\n",
        "def test_foo():\n    assert type(x) == type(x)\n",
        "def _is_ok(v):\n    return type(v) == int\n",
        "def test_foo():\n    assert type(result) == SomeModule.Result\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _type_equality_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert type(err) is ValueError\n",
        "def test_foo():\n    assert type(result) is not SomeModel\n",
        "def test_foo():\n    assert isinstance(err, ValueError)\n",
        "def test_foo():\n    assert not isinstance(x, (int, float))\n",
        "def test_foo():\n    assert x.__class__ is int\n",
        "def test_foo():\n    assert x.__class__ == int\n",
        "def test_foo():\n    C = type('C', (object,), {})\n",
        "def test_foo():\n    t = type(obj)\n    assert t == SomeClass\n",
        "def test_foo():\n    assert obj.type == SomeClass\n",
        "def test_foo():\n    assert SomeClass == other_type\n",
        "def test_foo():\n    assert type(x) in (int, float)\n",
        "def test_foo():\n    assert result_type == SomeClass\n",
        "def test_foo():\n    return type(value)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _type_equality_violations(tree), f"lens should NOT flag:\n{source}"


def _noop_typecheck_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every ``isinstance``/``issubclass``
    call whose types argument makes the check a fixed outcome.

    ``isinstance(x, object)`` is ALWAYS True — every object in the language is
    an instance of ``object`` — and ``isinstance(x, ())`` is ALWAYS False — an
    empty types tuple matches nothing. ``issubclass`` is the mirror image on the
    class side: every class (``object`` itself included) is a subclass of
    ``object``, and ``issubclass(X, ())`` always fails. A types *tuple* that
    contains a bare ``object`` element (``(int, object)``) is the disjunction of
    its elements, so the ``object`` element alone forces the whole check to
    ALWAYS be True. Written inside an assertion, a fixed-outcome check is dead
    code: the verdict is decided at source time, so the assert passes (or
    fails) no matter how broken the behaviour under test is. Only the bare
    ``object`` *name* is matched — an attribute spelling such as
    ``builtins.object`` is not, mirroring how the type-equality lens only
    matches the builtin ``type`` name."""
    found: list[tuple[int, str]] = []

    def _is_object_name(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "object"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("isinstance", "issubclass"):
            continue
        if len(node.args) != 2 or node.keywords:
            continue
        types = node.args[1]
        verdict: str | None = None
        why: str | None = None
        if _is_object_name(types):
            verdict = "ALWAYS True"
            why = (
                "every object is an instance of object"
                if node.func.id == "isinstance"
                else "every class is a subclass of object"
            )
        elif isinstance(types, ast.Tuple) and not types.elts:
            verdict = "ALWAYS False"
            why = (
                "an empty types tuple matches nothing"
                if node.func.id == "isinstance"
                else "an empty types tuple has no subclass"
            )
        elif isinstance(types, ast.Tuple) and any(_is_object_name(elt) for elt in types.elts):
            verdict = "ALWAYS True"
            why = "a types tuple is a disjunction and object matches every object"
        if verdict is None:
            continue
        found.append(
            (
                node.lineno,
                f"{node.func.id}({ast.unparse(node.args[0])}, {ast.unparse(types)}) — {verdict} "
                f"({why}); the outcome is fixed at source time, so the assert is dead code — "
                "assert against a specific type or drop the check",
            )
        )
    return found


def test_no_noop_typecheck_tautologies():
    """``isinstance``/``issubclass`` against a types argument that fixes the
    verdict at source time — the bare ``object`` class (ALWAYS True: every
    object is an instance of ``object``, and every class a subclass of it), an
    empty types tuple ``()`` (ALWAYS False: nothing to match), or a types tuple
    containing ``object`` alongside other types (the tuple is a disjunction, so
    ``object`` alone forces the check ALWAYS True). A check that can never
    change its outcome is dead assertion code — assert against a specific type
    or drop the check entirely."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _noop_typecheck_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} isinstance/issubclass check(s) with a fixed source-time verdict.\n"
        "isinstance(x, object) ALWAYS passes and isinstance(x, ()) ALWAYS fails, and a types tuple\n"
        "containing object always passes because object matches every object. Each is dead assertion\n"
        "code: assert against a specific type or drop the check.\n" + "\n".join(violations)
    )


def test_noop_typecheck_lens_flags_fixed_verdicts():
    """Synthetic positive/negative control for the no-op typecheck lens: it must
    flag the bare ``object`` class and the empty or object-containing types
    tuple — in ``isinstance`` and ``issubclass`` calls, in assert and in helper
    code — and ignore spelled-out ``object`` attribute forms, captured names,
    and every concrete type or multi-type tuple that leaves room for a real
    outcome."""
    positive_sources = [
        "def test_foo():\n    assert isinstance(result, object)\n",
        "def test_foo():\n    assert not isinstance(err, object)\n",
        "def test_foo():\n    assert issubclass(Child, object)\n",
        "def test_foo():\n    assert isinstance(value, ())\n",
        "def test_foo():\n    assert not isinstance(x, ())\n",
        "def test_foo():\n    assert issubclass(Sub, ())\n",
        "def test_foo():\n    assert isinstance(x, (int, object))\n",
        "def test_foo():\n    assert isinstance(model, (object, models.BaseModel))\n",
        "def test_foo():\n    assert isinstance(x, (object,))\n",
        "def test_foo():\n    assert issubclass(SomeClass, (object, Other))\n",
        "def _is_ok(v):\n    return isinstance(v, object)\n",
        "def _helper(v):\n    return issubclass(v, ())\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _noop_typecheck_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert isinstance(result, dict)\n",
        "def test_foo():\n    assert isinstance(value, (str, bytes))\n",
        "def test_foo():\n    assert not isinstance(x, (int, float))\n",
        "def test_foo():\n    assert issubclass(A, B)\n",
        "def test_foo():\n    assert issubclass(models.Model, DeclarativeBase)\n",
        "def test_foo():\n    assert isinstance(x, module.object)\n",
        "def test_foo():\n    assert isinstance(x, builtins.object)\n",
        "def test_foo():\n    cls = object\n    assert isinstance(x, cls)\n",
        "def test_foo():\n    assert isinstance(x, X.object)\n",
        "def test_foo():\n    assert issubclass(object, cls)\n",
        "def test_foo():\n    assert isinstance(x, SomeObject)\n",
        "def test_foo():\n    types = (int, str)\n    assert isinstance(x, types)\n",
        "def test_foo():\n    assert x.__class__ is object\n",
        "def test_foo():\n    assert isinstance(x, Any)\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _noop_typecheck_violations(tree), f"lens should NOT flag:\n{source}"


def _conditional_verdict_assert_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assertion whose *entire*
    verdict is a conditional expression whose branches are both boolean
    verdicts (``assert a == 1 if c else b == 2``).

    An ``assert VERDICT_A if COND else VERDICT_B`` pins two different
    expectations behind one assertion, selected at runtime by ``COND``. If it
    fails, pytest rewrites the whole ternary and reports the resulting boolean
    as one opaque expression — it cannot say which branch broke, and a reader
    cannot see which expectation each branch requires. A literal ``True``/
    ``False`` branch makes the hazard concrete: whenever ``COND`` selects that
    branch the assertion ALWAYS passes (or ALWAYS fails) no matter how broken
    the behaviour under test is — the IfExp twin of the constant-absorbed
    boolean hazard, which only recognises the ``BoolOp`` shape. The legitimate
    spellings are the conditional-*value* form (``expected = ... if ... else
    ...`` then ``assert x == expected``) or one ``assert`` per branch.

    Conditional *operands* are deliberately not matched: ``assert x in (err if
    err else "")`` computes a single value and then asserts one fact about it.
    A branch that is a bare variable/attribute (``assert pending if c else a
    == 1``, ``assert result.ok if c else result.valid``) is the legitimate
    conditional-truthiness idiom — it tests two independent boolean *values*,
    neither of which is a comparison the lens can prove diverges — and is left
    alone too."""
    found = []

    def _is_verdict(node: ast.AST) -> bool:
        """True when ``node`` evaluates to a boolean outcome rather than a value:
        a comparison, a ``not``-wrapped verdict, a boolean combination of
        verdicts, or a nested conditional whose branches are verdicts."""
        if isinstance(node, ast.Compare):
            return True
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return _is_verdict(node.operand)
        if isinstance(node, ast.BoolOp):
            return bool(node.values) and all(_is_verdict(v) for v in node.values)
        if isinstance(node, ast.IfExp):
            return all(_is_verdict(v) for v in (node.body, node.orelse))
        return False

    def _is_bool_constant(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, bool)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.IfExp):
            branches = (test.body, test.orelse)
            both_outcomes = all(_is_verdict(b) or _is_bool_constant(b) for b in branches)
            if both_outcomes:
                verdict_pin = (
                    "both branches are full boolean verdicts"
                    if not any(_is_bool_constant(b) for b in branches)
                    else "a branch is a literal True/False constant"
                )
                found.append(
                    (
                        node.lineno,
                        f"assert {ast.unparse(test)} — {verdict_pin}, so the assert's outcome is "
                        "chosen at runtime by the condition and the failure is reported as one "
                        "opaque boolean (pytest cannot say which branch broke), while a literal "
                        "True/False branch is a fixed outcome whenever it is selected. Compute the "
                        "expected value first (`expected = ... if ... else ...`) then assert "
                        "`x == expected`, or split into one assert per branch",
                    )
                )
    return found


def test_no_conditional_verdict_asserts():
    """An assertion whose entire verdict is a *conditional expression* —
    ``assert a == 1 if c else b == 2`` — pins two different expectations
    behind one assertion. The branch taken (and therefore which expectation
    must hold) is picked at runtime by the condition, so a failure is reported
    as a single opaque boolean that pytest cannot attribute to either branch,
    and — when one branch is a literal ``True``/``False`` — the assert is a
    fixed outcome whenever the condition selects that branch (the IfExp twin
    of the constant-absorbed boolean hazard). Conditional *operands* (``assert
    x in (err if err else "")``) and conditional truthiness between two
    boolean *values* (``assert result.ok if c else result.valid``) are the
    legitimate forms and are left alone."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _conditional_verdict_assert_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} conditional-verdict assert(s).\n"
        "An 'assert X if C else Y' where X and Y are both boolean verdicts pins two expectations\n"
        "in one assertion: pytest reports the whole ternary as an opaque boolean and cannot say\n"
        "which branch broke, and a literal True/False branch is a fixed outcome whenever the\n"
        "condition selects it. Compute the expected value first (expected = ... if ... else ...)\n"
        "then assert x == expected, or split into one assert per branch.\n" + "\n".join(violations)
    )


def test_conditional_verdict_lens_flags_opaque_branches():
    """Synthetic positive/negative control for the conditional-verdict lens: it
    must flag a conditional expression standing as the whole assertion verdict
    whose branches are verdicts (comparisons, boolean combinations, a
    ``not``-wrapped verdict, or a literal True/False constant) — in either
    branch order — and ignore the conditional-*operand* form, conditional
    truthiness between two boolean values, a conditional feeding an expected
    value, and any plain assertion."""
    positive_sources = [
        "def test_foo():\n    assert a == 1 if c else b == 2\n",
        "def test_foo():\n    assert x != 3 if flag else y < 1\n",
        "def test_foo():\n    assert (a == 1 and b > 0) if c else (a == 2 or b < 0)\n",
        "def test_foo():\n    assert not (x is None) if ready else y is not None\n",
        "def test_foo():\n    assert a == 1 if c else True\n",
        "def test_foo():\n    assert False if c else a == 1\n",
        "def test_foo():\n    assert b == 2 if ready else a == 1\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _conditional_verdict_assert_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert a == 1\n",
        "def test_foo():\n    assert x in (err if isinstance(err, str) else str(err))\n",
        "def test_foo():\n    assert result.ok if c else result.valid\n",
        "def test_foo():\n    assert pending or done if c else a == 1\n",
        "def test_foo():\n    expected = 501 if isinstance(exc, ProgrammingError) else 503\n"
        "    assert resp.status_code == expected\n",
        "def test_foo():\n    assert (x if c else y) == 1\n",
        "def test_foo():\n    assert len(x) if c else len(y)\n",
        "def test_foo():\n    assert a == 1 if c else b\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _conditional_verdict_assert_violations(tree), f"lens should NOT flag:\n{source}"


# ---------------------------------------------------------------------------
# LENS: membership against the redundant ``d.keys()`` dict view
# ---------------------------------------------------------------------------
def _dict_keys_membership_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, detail)`` pairs for every assert whose verdict is a
    membership test against the redundant ``.keys()`` dict view (``assert x in
    d.keys()`` / ``assert x not in d.keys()``).

    ``k in d`` already performs key membership, so ``k in d.keys()`` yields the
    same verdict through an extra call that ruff SIM118 flags as redundant.
    Unlike the sibling ``.items()``/``.values()`` views — which genuinely change
    what is being tested and are the correct spellings when present — the
    ``.keys()`` view carries no information the bare mapping does not, and the
    spelling is one typo away from a value-view confusion (``k in d.values()``)
    that silently flips the assertion's meaning. Only the argument-free ``.keys()``
    call whose receiver is a callable/member expression is matched: a bare
    attribute without the call (``mapping.keys``), a ``.keys(x)`` call, or an
    equality/other comparison against the view is a different expression."""
    found: list[tuple[int, str]] = []

    def _is_keys_view(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and not node.args
            and not node.keywords
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "keys"
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        for op, comp in zip(test.ops, test.comparators, strict=True):
            if not isinstance(op, (ast.In, ast.NotIn)):
                continue
            if not _is_keys_view(comp):
                continue
            op_name = "in" if isinstance(op, ast.In) else "not in"
            found.append(
                (
                    node.lineno,
                    f"assert {ast.unparse(test)} — {op_name} against the redundant "
                    "'d.keys()' dict view: 'in <mapping>' already tests key membership "
                    "(ruff SIM118), and the extra call is one typo away from the "
                    "value-view confusion ('k in d.values()') that silently flips the "
                    "meaning. Assert against the mapping directly (e.g. assert key in "
                    "mapping)",
                )
            )
    return found


def test_no_dict_keys_membership():
    """Membership against the redundant ``.keys()`` dict view —
    ``assert x in d.keys()`` / ``assert x not in d.keys()`` — is a dead extra
    call: ``k in d`` already tests key membership, so ``.keys()`` adds nothing
    but the SIM118 redundancy while sitting one typo away from the value-view
    confusion (``k in d.values()``) that silently changes the assertion's
    meaning. Unlike ``.items()``/``.values()`` — the views that genuinely change
    what is being tested — ``.keys()`` carries no information the bare mapping
    does not."""
    violations = []
    for path in _iter_test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(TESTS)
        for lineno, detail in _dict_keys_membership_violations(tree):
            violations.append(f"  {rel}:{lineno}  {detail}")
    assert not violations, (
        f"Found {len(violations)} assert(s) against d.keys().\n"
        "'assert k in d.keys()' is redundant — 'in <mapping>' already tests key "
        "membership (ruff SIM118),\n"
        "and the extra call is one typo away from the value-view confusion "
        "('k in d.values()')\n"
        "that silently flips the meaning. Assert against the mapping directly "
        "(assert key in mapping).\n" + "\n".join(violations)
    )


def test_dict_keys_membership_lens_flags_redundant_views():
    """Synthetic positive/negative control for the ``.keys()`` membership lens:
    it must flag every ``in``/``not in`` assert whose right operand is the
    redundant, argument-free ``.keys()`` dict view — regardless of left-operand
    shape and receiver spelling — and leave the meaningful view memberships
    (``.items()`` / ``.values()``), the plain ``in <mapping>`` idiom, the bare
    ``mapping.keys`` attribute, and unrelated comparisons alone."""
    positive_sources = [
        "def test_foo():\n    assert key in mapping.keys()\n",
        "def test_foo():\n    assert key not in mapping.keys()\n",
        "def test_foo():\n    assert payload['id'] in result.keys()\n",
        "def test_foo():\n    assert (org_id, name) not in rows.keys()\n",
        "def test_foo():\n    assert find_key(config) in settings.keys()\n",
    ]
    for source in positive_sources:
        tree = ast.parse(source)
        assert _dict_keys_membership_violations(tree), f"lens should flag:\n{source}"

    negative_sources = [
        "def test_foo():\n    assert key in mapping\n",
        "def test_foo():\n    assert key not in mapping\n",
        "def test_foo():\n    assert (k, v) in mapping.items()\n",
        "def test_foo():\n    assert value in mapping.values()\n",
        "def test_foo():\n    assert value not in mapping.values()\n",
        "def test_foo():\n    assert key in mapping.keys\n",
        "def test_foo():\n    assert key in mapping.keys(x)\n",
        "def test_foo():\n    assert set(mapping.keys()) == expected\n",
        "def test_foo():\n    assert len(mapping.keys()) == len(keys)\n",
        "def test_foo():\n    assert key in ('a', 'b')\n",
    ]
    for source in negative_sources:
        tree = ast.parse(source)
        assert not _dict_keys_membership_violations(tree), f"lens should NOT flag:\n{source}"
