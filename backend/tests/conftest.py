"""Project-level conftest — shared test utilities only.

Do NOT put connector-specific fixtures here; they belong in
``tests/connectors/conftest.py``.
"""

import os

# Settings-critical env default (FAR-1751). pytest imports this conftest FIRST
# for every command-line anchor (ancestor chains load outermost-first), so a
# setdefault here wins over the ``setdefault`` calls in the unit/integration
# subconftests, which import later during collection. Order matters: unit
# subconftests set an INVALID placeholder key ("a" * 32 — it satisfies the
# Settings validator's length check but ``cryptography.Fernet`` rejects it), so
# a mixed ``tests/unit tests/integration`` run in ONE process previously cached
# a ``Settings`` carrying that invalid key before the integration conftest's
# valid ``setdefault`` could apply. Every consumer constructing ``Fernet`` from
# the lru-cached ``settings.fernet_key`` (e.g. the executor's
# ``ModuloPostgresSaver``) then raised ``Fernet key must be 32 url-safe
# base64-encoded bytes`` — a failure each suite never shows alone. Put the
# default at the earliest load point instead of special-casing a cached
# ``Settings``. ``setdefault`` still lets CI / the caller supply an explicit
# value (the workflow exports FERNET_KEY, which therefore always wins).
os.environ.setdefault("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

# pydantic.root_model residency guard — WHY this import exists:
#
# pytest-cov starts coverage during ``pytest_load_initial_conftests``, BEFORE
# this conftest is imported. When coverage is started with a single-module
# source spec (e.g. ``--cov=modulo.core.library_service.community``),
# coverage.py's ``InOrOut.set_matchers_depending_on_syspath()`` resolves the
# module via ``find_spec`` — which executes the target's parent packages —
# inside ``sys_modules_saved()``, and ``SysModuleSaver.restore()`` then
# deletes every module imported during that block from ``sys.modules``.
# That block's import chain (``library_service`` -> ``community`` ->
# ``library_sync`` -> ``settings`` -> ``pydantic_settings.sources.utils``)
# triggers pydantic's lazy ``pydantic.root_model`` load; the rollback removes
# the submodule from ``sys.modules`` but leaves the ``RootModel`` attribute
# bound on the already-imported ``pydantic`` package. Every later
# ``from pydantic import RootModel`` is then satisfied by that dangling
# attribute, the submodule is never re-imported, and ``mcp/types.py``'s
# import-time ``class JSONRPCMessage(RootModel[...])`` crashes in
# ``pydantic._internal._generics.create_generic_submodel`` with
# ``KeyError: 'pydantic.root_model'`` — erroring every test whose setup
# imports the app. This conftest is imported AFTER the rollback, so an
# explicit submodule import here re-establishes the ``sys.modules`` entry
# (explicit submodule imports bypass the dangling parent attribute) and every
# later RootModel generic specialization can resolve its module. A pre-import
# inside product code would NOT work: product modules first execute inside the
# rollback block, so their import is deleted again. Guarded by
# ``tests/unit/library_sync/test_coverage_source_import_guard.py``.
import sys
import warnings
from pathlib import Path

import pydantic.root_model  # noqa: F401
import pytest

import modulo.core.ssrf as _ssrf

#: The warning class the audited suites must treat as a HARD ERROR. Kept as the
#: pytest ``-W`` spelling so every CI command that runs a gated suite can carry
#: the byte-identical string - the architecture guard compares them.
UNRAISABLE_ESCALATION = "error::pytest.PytestUnraisableExceptionWarning"

#: Test trees AUDITED for leaked never-awaited coroutines / unclosed resources
#: and confirmed clean under the escalation. Only these are gated; every other
#: tree has NOT been audited to green, so a mixed ``pytest tests/`` run must not
#: silently apply the escalation to it.
#:
#: ``conformance`` (the runtime-provider suite, FAR-1053) was audited to green
#: on 2026-10-23 against a live single-node ``kind`` cluster (13/13 with
#: ``-W error::pytest.PytestUnraisableExceptionWarning``) and is gated. It is
#: still deselected by default via ``addopts``, so the gate only applies to an
#: explicit ``-m runtime_provider_conformance`` run and to the k8s-conformance
#: workflow.
#:
#: Still excluded:
#:
#: - ``load`` / ``determination`` - no test modules exist (``load`` holds only
#:   locust/support files; ``determination`` holds only fixture data).
#:
#: ``docker`` is audited and gated: it was brought to green under the
#: escalation (the FAR-2095 stop-vs-remove defect plus the leaked exec streams
#: and socket-proxy session), so it is no longer an exclusion.
#:
#: The hook lives HERE (the root conftest) rather than in a suite conftest
#: because pytest only loads a directory's conftest when that directory is on
#: the collection path: ``tests/unit/conftest.py`` is never imported by a
#: ``pytest tests/integration`` or ``pytest tests/bdd`` run, so a hook there
#: cannot gate those suites.
GATED_SUITE_ROOTS: tuple[str, ...] = (
    "unit",
    "integration",
    "bdd",
    "architecture",
    "connectors",
    "conformance",
    "docker",
)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_TESTS_ROOT = _BACKEND_ROOT / "tests"


def _gated_item(item: pytest.Item) -> bool:
    """True when *item* belongs to one of the audited suites in ``GATED_SUITE_ROOTS``.

    A leaked never-awaited coroutine only surfaces at garbage collection as
    ``PytestUnraisableExceptionWarning``. Left at its default action it floods
    the terminal with multi-line summaries, and a large enough summary can wedge
    pytest's terminal writer with ``BlockingIOError: [Errno 11] write could not
    complete without blocking`` - the 2026-10-21 merge-queue outage. Escalating
    the class to ``error`` surfaces the leak on the test that emits it.
    """
    path = getattr(item, "path", None)
    if path is None:  # pytest < 7 fallback
        path = item.fspath
    resolved = Path(str(path)).resolve()
    return any(resolved.is_relative_to(_TESTS_ROOT / suite) for suite in GATED_SUITE_ROOTS)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Make any leaked-resource warning fail the test that emits it.

    This is a ``pytest_runtest_setup`` hook rather than ``pytest_configure``
    deliberately. pytest's warnings plugin re-applies the ini/cmdline
    ``filterwarnings`` list around EVERY item (``catch_warnings_for_item``),
    AFTER ``pytest_configure`` has run - so a filter installed at configure
    time is overridden by the project-wide
    ``default::pytest.PytestUnraisableExceptionWarning`` entry and the class is
    merely printed. This hook runs inside that per-item context, so the
    ``error`` filter is the first match. pytest restores the filters at item
    teardown, keeping the scope to gated suites in a mixed run.

    Pinned by ``tests/architecture/test_unit_unraisable_warning_gate.py`` -
    that architecture test fails if this hook is removed, if a suite is wrongly
    added to or dropped from ``GATED_SUITE_ROOTS``, or if the matching ``-W``
    flag is dropped from any CI command that runs a gated suite.
    """
    if _gated_item(item):
        warnings.filterwarnings("error", category=pytest.PytestUnraisableExceptionWarning)


#: The module whose ``_verify_identity`` the FAR-1631 leak guard protects.
_AUTH_DEPS_MODULE = "modulo.auth.dependencies"

#: Snapshot of the real ``_verify_identity`` — the function object the auth
#: module body bound when it executed. Captured LAZILY (never at conftest
#: load): importing ``modulo.auth.dependencies`` costs ~2 s of session
#: startup, which is pure overhead for pure-logic sessions that never touch
#: auth. The capture points, in order of likelihood:
#:
#: 1. At module EXEC — an import hook (``_AuthDepsSnapshotFinder``, installed
#:    below) snapshots the binding the instant the module body finishes
#:    executing, before any importer receives the module object and can
#:    patch it. This is the normal path and is pristine by construction.
#: 2. At conftest load — when the module was already in ``sys.modules``
#:    (imported by a plugin before this conftest loaded). Still pristine:
#:    no test fixture has run yet.
#: 3. At the guard's FIRST observation (``_assert_real_verify_identity``)
#:    — only reachable when the module somehow predated the import hook AND
#:    was not caught by point 2; the root autouse guard runs before every
#:    other fixture, so a first observation at test-1 setup is still
#:    pre-patch.
#:
#: ``None`` until a capture happens. The guard below compares by IDENTITY
#: (``is``) rather than ``isinstance(..., Mock)`` so that ANY replacement —
#: a plain callable, a lambda, a partial — is caught, not just
#: ``unittest.mock`` instances.
_REAL_VERIFY_IDENTITY: object | None = None


def _snapshot_verify_identity(module: object) -> None:
    """Record the pristine ``_verify_identity`` binding from *module*.

    Called only at the three capture points above — each guarantees the
    module body has bound the real function and no fixture has patched it.
    On ``importlib.reload`` the hook fires again and re-snapshots the fresh
    binding, keeping the guard aligned with the live module.
    """
    global _REAL_VERIFY_IDENTITY
    _REAL_VERIFY_IDENTITY = module._verify_identity


class _AuthDepsSnapshotLoader:
    """Loader wrapper: snapshot ``_verify_identity`` right after ``exec_module``.

    ``import`` sets ``sys.modules[name]`` BEFORE executing the module body,
    but no importer can *use* the module until its import call returns — so
    the moment ``exec_module`` completes, the binding is the real function
    and no patcher has run yet. Duck-typed (no ``importlib.abc`` subclass) to
    keep conftest imports minimal; every other loader attribute delegates.
    """

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def create_module(self, spec: object) -> object | None:
        create = getattr(self._wrapped, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: object) -> None:
        self._wrapped.exec_module(module)  # type: ignore[attr-defined]
        _snapshot_verify_identity(module)

    def __getattr__(self, name: str) -> object:
        # Delegate get_source / get_code / is_package / etc. to the real loader
        # (coverage.py and other tooling may probe them).
        return getattr(self._wrapped, name)


class _AuthDepsSnapshotFinder:
    """``sys.meta_path`` finder that installs the snapshot loader.

    Registered at conftest load (zero cost — no module is imported). For
    every import attempt it only string-compares the name; on a match it
    delegates to the remaining finders and wraps the winning loader. Inserted
    at the FRONT of ``sys.meta_path`` so it sees the import before the default
    ``PathFinder`` (which would otherwise return an unwrapped loader first);
    non-matching names return ``None`` immediately, so pytest's assertion
    rewriter and every other finder run unchanged.
    """

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> object | None:
        if fullname != _AUTH_DEPS_MODULE:
            return None
        for finder in tuple(sys.meta_path):
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is not None:
                if getattr(spec, "loader", None) is not None:
                    spec.loader = _AuthDepsSnapshotLoader(spec.loader)
                return spec
        return None


# Install the hook (cheap; no import performed), then handle the pre-existing
# module case: anything already in ``sys.modules`` at conftest-load time was
# imported before the hook existed and will never re-import, so snapshot it
# now — still pre-collection, pre-fixture, therefore pristine.
sys.meta_path.insert(0, _AuthDepsSnapshotFinder())
_auth_deps_preexisting = sys.modules.get(_AUTH_DEPS_MODULE)
if _auth_deps_preexisting is not None:
    _snapshot_verify_identity(_auth_deps_preexisting)
del _auth_deps_preexisting


def _assert_real_verify_identity() -> None:
    """Fail fast when a leaked patch has replaced ``_verify_identity``.

    Regression guard for the FAR-1631 leak class: two fixtures patching the
    SAME symbol (``modulo.auth.dependencies._verify_identity``) with DIFFERENT
    mechanisms (``unittest.mock.patch`` vs ``monkeypatch.setattr``) unwind their
    restores out of order, so ``monkeypatch``'s undo (it is a shared fixture
    torn down last) overwrites the real function with a *stale* mock. That
    silently changes every later test from that point on.

    Removing the original duplicate fixture did not retire the class — this
    guard's first run caught a live instance in the unit suite:
    ``tests/unit/conftest.py``'s ``_patch_verify_identity`` used
    ``unittest.mock.patch`` while per-module fixtures (e.g.
    ``tests/unit/test_schema_folders.py``'s ``_prevent_db_auth_check``) used
    ``monkeypatch.setattr``. ``_patch_verify_identity`` now also uses
    ``monkeypatch.setattr`` so both restores share one LIFO-correct undo stack.

    LAZY (why this is not a conftest-load check): the real function is
    captured by the import hook the moment the auth module executes — never
    by importing it at conftest load, which cost every backend test session
    ~2 s even when no test touches auth. When the module is not in
    ``sys.modules`` at all (a pure-logic session), nothing can hold or leak a
    patched symbol and the check returns at zero cost. When it IS present,
    the snapshot was taken pristine (see ``_REAL_VERIFY_IDENTITY``), so the
    comparison below is exactly as strong as the old eager capture —
    including catching a fixture that leaked a patch before this guard's
    first observation of the symbol. Ordering makes a same-setup race
    impossible: this is a ROOT-level autouse fixture, and pytest runs
    root-conftest autouse fixtures before sub-directory autouse fixtures, so
    the guard always observes the state left by the previous test's teardown,
    never a concurrent patch.

    The comparison is by identity against the snapshot, so it catches a
    replacement of any shape, not only ``Mock`` objects. Exposed as a
    module-level function (not inlined in the fixture) so the guard's own
    behaviour is covered by ``tests/architecture/
    test_verify_identity_guard.py`` — a guard that cannot fail is not a guard.
    """
    module = sys.modules.get(_AUTH_DEPS_MODULE)
    if module is None:
        # Never imported in this session: no symbol exists to leak. This is
        # the pure-logic fast path the laziness exists for.
        return
    captured = _REAL_VERIFY_IDENTITY
    if captured is None:
        # Belt-and-braces fallback for a module that predated the import hook
        # without being caught at conftest load. Reachable only at the first
        # test's setup (any later setup would have found a snapshot), and the
        # root autouse guard runs before every fixture — so the binding is
        # still pristine: capture it now.
        _snapshot_verify_identity(module)
        return
    current = module._verify_identity
    if current is not captured:
        raise AssertionError(
            "modulo.auth.dependencies._verify_identity was replaced and not "
            "restored before test setup: a fixture leaked a patch (teardown "
            "restored a stale value). Expected the real function "
            f"{captured!r} but found {current!r}. Fix the leaking "
            "fixture's teardown so it restores the original callable."
        )


@pytest.fixture(autouse=True)
def _guard_real_verify_identity() -> None:
    """Run :func:`_assert_real_verify_identity` at every test SETUP.

    This root-level autouse guard runs before the legitimate patchers, which
    set up strictly after it, so it observes the state left by the *previous*
    test's teardown. A leaked replacement is caught here on the next test
    instead of silently changing its behaviour. When
    ``modulo.auth.dependencies`` was never imported in this session the check
    is a no-op (see :func:`_assert_real_verify_identity`).
    """
    _assert_real_verify_identity()


@pytest.fixture(autouse=True)
def _allow_test_hostnames(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise real DNS in the SSRF guard for the whole backend suite.

    This is the SINGLE definition of the shim — it lives at the root of
    ``tests/`` so it applies to every sub-suite (unit, connectors, model
    backends, BDD). Do not copy it into a per-directory conftest.

    The SSRF guard (``modulo.core.ssrf``) performs real DNS resolution, which is
    unavailable in CI sandboxes. Every backend test mocks the HTTP layer (respx
    or a patched ``AsyncClient``) against ``example.com`` / ``localhost`` hosts,
    so the guard fails closed on absent DNS and breaks the suite.

    Stubbing ``ssrf._resolve_all_sync`` / ``_resolve_all_async`` makes any
    hostname validate as a public address. This only affects the *validation*
    pre-check: production call sites use ``validate_outbound_url`` (no connection
    pinning), so no test will actually connect to the stubbed address. Literal
    private/loopback IPs remain blocked via a separate DNS-independent code
    path, so the control is not weakened.

    Two opt-outs exist so the shim can never hide a fail-closed regression:

    * ``tests/unit/core/test_ssrf.py`` exercises the REAL resolver (including
      DNS timeouts and resolution failures) and is skipped by filename.
    * Any test marked ``@pytest.mark.real_ssrf_dns`` gets the real resolver, so
      the connector / model-backend gate tests can prove the guard rejects a
      private or loopback ``base_url``.
    """
    # The SSRF unit suite validates the real resolver; never stub it there, or
    # the fail-closed timeout/failure assertions would be silently defeated.
    if "test_ssrf" in getattr(request.node.path, "name", ""):
        return
    # Explicit per-test opt-out for suites that assert the guard fails closed.
    if request.node.get_closest_marker("real_ssrf_dns") is not None:
        return

    monkeypatch.setattr(_ssrf, "_resolve_all_sync", lambda host: ["8.8.8.8"])

    async def _fake_resolve(_host: str) -> list[str]:  # pragma: no cover - test shim
        return ["8.8.8.8"]

    monkeypatch.setattr(_ssrf, "_resolve_all_async", _fake_resolve)


pytest_plugins = ["tests.quarantine_plugin"]


# ---------------------------------------------------------------------------
# POSIX-only suites: the SINGLE platform gate (FAR-2070)
# ---------------------------------------------------------------------------
#
# A few modules execute a SHIPPED shell artefact (``scripts/dispatch_with_retry.sh``,
# and a CI workflow step's real ``run:`` block) in a subprocess with stubbed
# ``curl`` / ``gh`` / ``sleep`` binaries placed first on ``PATH``. Those modules
# need a genuinely POSIX execution environment:
#
# * **Executable-bit semantics.** The stubs are shell scripts made runnable with
#   ``Path.chmod(0o755)`` and resolved through their ``#!/usr/bin/env bash``
#   shebang. A POSIX filesystem honours the mode bits; MSYS/Git-Bash on Windows
#   does not, so a stub that is not really executable resolves to a different
#   binary (or nothing) and the test stops exercising the script under test -
#   a pass for the wrong reason.
# * **A ``PATH`` the shell can read.** The modules build ``PATH`` as
#   ``f"{stub_dir}{os.pathsep}{existing}"`` from a Windows ``tmp_path`` such as
#   ``C:\Users\...\stubs``. Under MSYS bash the colon inside ``C:`` splits the
#   entry; under WSL bash - which is what ``bash`` on ``PATH`` actually resolves
#   to on this project's Windows hosts (``C:\Windows\System32\bash.exe``) - a
#   Windows path does not exist at all. Either way the stubs are never found.
# * **``jq``.** ``dispatch_with_retry.sh`` parses the webhook response with
#   ``jq``. Supplying a fake ``jq`` stub to make the suite "run" on Windows
#   would fake a real production dependency and weaken the assertions, so it is
#   deliberately not done.
#
# Mark such a module ``pytest.mark.posix_only`` (module-level
# ``pytestmark = pytest.mark.posix_only``). This hook - the ONE definition of
# the gate - skips it on Windows and does nothing on any other platform, so
# Linux CI and POSIX dev machines still execute it for real. Declaring the
# requirement once here, instead of repeating a
# ``pytest.mark.skipif(sys.platform == "win32", ...)`` in every affected
# module, keeps it discoverable (``pytest --markers`` lists ``posix_only``,
# registered in ``backend/pyproject.toml``) and keeps the modules free of
# ad-hoc test disables.
#
# The pre-commit "run tests for changed test files" hook
# (``scripts/run_changed_tests.py``) needs no special-casing: a skipped test is
# not a failure, so committing a change to a ``posix_only`` module on Windows
# passes the hook while Linux CI still runs the real assertions.
#
# Running these suites on Windows: use a POSIX host - Linux CI, the dev-1
# Linux box, or WSL with ``jq`` installed (invoke pytest from inside the WSL
# filesystem so ``tmp_path`` is a POSIX path). There is no Windows override,
# because none of the three requirements above can be satisfied there.

POSIX_ONLY_MARKER = "posix_only"

POSIX_ONLY_SKIP_REASON = (
    f"{POSIX_ONLY_MARKER}: requires a POSIX shell (executable-bit semantics and a POSIX PATH) and jq; "
    "skipped on Windows - runs for real on Linux CI"
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``posix_only`` suites on Windows - the single platform gate.

    Additive to the other ``pytest_collection_modifyitems`` implementations (the
    quarantine plugin registers its own); pytest calls every registered hook.
    See the block comment above for why the platform requirement is real and why
    it is declared exactly once, here, rather than per module.
    """
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(reason=POSIX_ONLY_SKIP_REASON)
    for item in items:
        if item.get_closest_marker(POSIX_ONLY_MARKER) is not None:
            item.add_marker(skip)
