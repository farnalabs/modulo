"""Global fixtures for all unit tests."""

import linecache
import os
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, AsyncMockMixin

import pytest

from tests.helpers.audit_session import install_audit_session_double
from tests.unit._e2b_sandbox_bridge import install_bridge

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")


@pytest.fixture(autouse=True)
def _restore_app_dependency_overrides() -> Generator[None, None, None]:
    """Restore FastAPI dependency overrides after every unit test.

    API tests often install overrides directly on the process-global app. A
    forgotten cleanup can leave an ``AsyncMock`` session factory active for a
    later test in the same xdist worker, so the later test observes another
    test's mock lifecycle and may leave its coroutines unawaited. This fixture
    is declared before the other autouse fixtures so its teardown runs last,
    after the test-specific fixture finalizers have finished.
    """
    app_module = sys.modules.get("farna.api.main")
    app = getattr(app_module, "app", None)
    baseline = dict(app.dependency_overrides) if app is not None else {}
    yield
    app_module = sys.modules.get("farna.api.main")
    app = getattr(app_module, "app", None)
    if app is not None:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(baseline)


@pytest.fixture(autouse=True)
def _isolated_audit_session(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Route the ``audited(...)`` fresh-session write to an in-memory double.

    See ``tests.helpers.audit_session`` for why the double exists and why
    integration tests are deliberately left to exercise the genuine append.

    Installs the double through a scoped ``monkeypatch.context()`` and yields
    from inside it, so the unpatch of ``_shared_session_factory`` runs as this
    fixture's teardown - after the test body - rather than during ordinary
    finalization while in-flight ``fail_closed=True`` destroy routes are still
    finalizing.

    GATED (same shape as ``_patch_verify_identity``'s auth-stack gate): the
    STRING target ``install_audit_session_double`` resolves by importing
    ``farna.core.audit_coverage`` (fastapi + sqlalchemy + db.models; ~1.8-3.8
    s measured 2026-10-13), and paying that for every unit test dragged
    pure-logic packages onto the app import stack they never touch. The
    install now runs only when the test can actually reach the audit path;
    see ``_audit_session_double_needed`` for the conservative-correctness
    argument (NOTE: unlike the auth gate, a false NEGATIVE here can be
    SILENT — a skipped fail-open audited route dials the real shared engine
    and logs-and-continues — so the predicate is pinned per-module by
    ``tests/architecture/test_unit_audit_double_gate.py``). The skip path
    still yields (a generator fixture must yield exactly once per invocation)
    but installs nothing; the ``monkeypatch.context()`` + yield teardown
    ordering above is unchanged on the install path.
    """
    if not _audit_session_double_needed(request):
        yield
        return
    with monkeypatch.context() as scoped:
        install_audit_session_double(scoped)
        yield


@pytest.fixture(autouse=True)
def _patch_verify_identity(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent _verify_identity from connecting to a real database.

    The _verify_identity function in auth/dependencies creates its own
    database engine and queries the real DB to check account/org existence.
    This bypasses all FastAPI dependency overrides, causing 401 errors when
    the local Postgres is running but the test UUIDs don't match real data.

    ``monkeypatch.setattr`` (not ``unittest.mock.patch``) is deliberate: several
    per-module fixtures (e.g. ``tests/unit/test_schema_folders.py``'s
    ``_prevent_db_auth_check``) patch this same symbol with ``monkeypatch``, and
    ``monkeypatch`` is a shared function-scoped fixture torn down LAST — after
    any ``unittest.mock.patch`` context exits. Sharing one undo stack keeps the
    two restores LIFO-correct, so the real function is restored rather than a
    stale AsyncMock leaking into the next test (the FAR-1631 leak class, now
    guarded by ``_guard_real_verify_identity`` in ``tests/conftest.py``).

    GATED (same shape as ``_far681_any_credential_default``'s FAR-1678 app
    import gate): the STRING target below resolves by importing
    ``farna.auth.dependencies`` (~5 s measured 2026-10-13), and paying that
    for every unit test dragged pure-logic packages onto the auth stack they
    never touch — PR #1730 made the root conftest's import lazy, but this
    fixture's string setattr re-introduced the eager import for every unit
    session. The patch now installs only when the test can actually reach
    ``_verify_identity``; see ``_verify_identity_patch_needed`` for the
    conservative-correctness argument and
    ``tests/architecture/test_unit_auth_patch_gate.py`` for the pin.
    """
    if not _verify_identity_patch_needed(request):
        return
    monkeypatch.setattr("farna.auth.dependencies._verify_identity", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _e2b_provider_bridge(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1050 R6: route the dispatch's provider seams at a mock-sandbox bridge.

    R6 deleted the legacy direct path, so ``_sandbox_agent_impl`` resolves its
    provider through ``_build_dispatch_provider`` and drives every site through
    the RuntimeProvider ABC. The pre-R6 unit suite drives the dispatch with
    ``patch("e2b.AsyncSandbox.create", ...)`` and asserts on the mock sandbox's
    SDK surface \u2014 this fixture installs an ABC-backed bridge over that mock so
    the suite exercises the provider path without a live sandbox or network.

    Seams patched: dispatch create/stream/kill, file I/O, log tail, isolation.
    Tests that install their own fake (``install_fake_dispatch``,
    ``fake_file_io``) or that patch a seam in the test body still win, because
    their ``monkeypatch`` calls run after this fixture. Tests that import a
    builder symbol directly (``from ... import _build_log_tail_provider``)
    keep the real function \u2014 the fixture only replaces the module attribute
    the dispatch looks up at call time.

    ``E2B_API_KEY`` is seeded so the seam helpers' key pre-checks pass; tests
    that assert a missing-credential refusal ``delenv`` it themselves.

    GATED by ``_e2b_bridge_needed`` (same conservative-predicate shape as
    ``_patch_verify_identity`` / ``_provisioned_system_engine`` / the
    ``_far681`` app gate): ``install_bridge`` imports
    ``farna.core.pipeline_engine.node_runner`` — a ~4.5 s chain measured
    2026-10-13 — and paying that on the first setup of every unit session
    dragged pure-logic packages onto the dispatch stack they never touch.
    The gate skips ONLY the install; the ``E2B_API_KEY`` seed stays
    unconditional so the environment every test observes is byte-identical
    to the ungated fixture (the seed is not the cost, and gating it would
    change env behaviour for no perf gain). See ``_e2b_bridge_needed`` for
    the conservative-correctness argument and
    ``tests/architecture/test_unit_e2b_bridge_gate.py`` for the pin.
    """
    monkeypatch.setenv("E2B_API_KEY", "bridge-test-key")
    if not _e2b_bridge_needed(request):
        return
    install_bridge(monkeypatch)


class _ProvisionedSystemSettings:
    """Settings stub presenting a provisioned system database URL.

    ``farna.api.dependencies`` resolves its settings via the module-level
    ``get_settings`` name, so tests can present a provisioned reading without
    touching the lru-cached real :class:`Settings`.
    """

    modulo_system_database_url = "postgresql+asyncpg://localhost/modulo-system-unit-test"
    # FAR-1524: get_or_create_system_engine passes this to create_async_engine
    # as pool_recycle; the stub must expose it just like the real Settings.
    db_pool_recycle_seconds = 1500


@pytest.fixture(autouse=True)
def _provisioned_system_engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Present a PROVISIONED system engine to the unit-test suite.

    Unit tests mock the system SESSION but run in an environment without
    ``MODULO_SYSTEM_DATABASE_URL``. The (now robust) fallback predicate
    initialises the engine factory itself, so an un-provisioned reading would
    503 every trigger delivery here. With a provisioned URL the flag reads
    False exactly as in production; the created engine is lazy and never
    connects (every system session is overridden per test).

    This fixture is registered in the UNIT conftest rather than
    ``tests/unit/api/conftest.py`` (it lived there, FAR-523): pytest keys an
    autouse fixture to the collector node that loaded its conftest, and the
    directory-level Package node for ``tests/unit/api/`` is NOT guaranteed
    to exist for an argv section that detours out of the directory. A
    pre-and-post ``tests/unit/test_analytics_builder.py`` argv order made
    test_slack_trigger_endpoint lose this fixture and read a degraded
    (fallback) system engine (FAR-1597). The unit-level conftest always
    loads — conftest.py of every directory containing an initial argv item
    is loaded before any test runs — so the provisioning is stable under any
    collection order.

    GATED by the same conservative predicate as ``_patch_verify_identity``
    (``_verify_identity_patch_needed``). The body below imports
    ``farna.api.dependencies``, which itself executes
    ``farna.auth.dependencies`` at module level (measured 2026-10-13) — so
    running it for every unit test is what pins pure-logic sessions onto the
    auth/app import stack even when the ``_verify_identity`` patch is gated.
    A test can only OBSERVE this stub by touching ``farna.api.dependencies``
    (the object being patched), and every such test either already has it in
    ``sys.modules`` (module-level import, resolved before fixture setup) or
    names ``farna.api`` / ``farna.auth`` in its source (lazy in-body
    import; the only other chains into ``api.dependencies`` verified in the
    test corpus — ``farna.auth.scim_auth`` — carry the ``farna.auth``
    token too). Skip cannot strand a consumer; it only spares tests that
    never load the module being patched.
    """
    if not _verify_identity_patch_needed(request):
        return
    from farna.api import dependencies as _deps

    monkeypatch.setattr(_deps, "get_settings", lambda: _ProvisionedSystemSettings())


#: The module whose import constructs the FastAPI application singleton
#: (``farna.api.main.app``). Loading it costs ~10 s of CPU-bound route and
#: pydantic-model construction (measured 2026-10-11, FAR-1678), so the unit
#: suite must only load it when a test can actually reach the app.
_APP_MODULE = "farna.api.main"

#: The module holding ``_verify_identity`` (and FastAPI's auth dependencies).
#: Importing it costs ~5 s (measured 2026-10-13), so the autouse
#: ``_patch_verify_identity`` / ``_provisioned_system_engine`` fixtures gate
#: on it instead of paying it for every unit test.
_AUTH_DEPS_MODULE = "farna.auth.dependencies"

#: ``sys.modules`` markers meaning "this session has already loaded the
#: auth/app stack". ``farna.api.dependencies`` is included because it
#: executes ``farna.auth.dependencies`` at module level, so its presence
#: means the expensive import is paid regardless — patching then costs only
#: the (cheap) setattr.
_AUTH_STACK_MARKERS: tuple[str, ...] = (_AUTH_DEPS_MODULE, "farna.api.dependencies")

#: Source tokens whose appearance in a test module marks it as able to reach
#: the auth/app stack INSIDE its body (a lazy ``from farna.api... import`` /
#: ``from farna.auth... import`` is a string in its source). ``farna.api``
#: is deliberately one hop wide: any ``farna.api.*`` import transitively
#: executes ``farna.auth.dependencies``, so catching only ``_AUTH_DEPS_MODULE``
#: would miss e.g. an in-body ``from farna.api.main import app``. A false
#: positive (token in a comment/docstring) costs only the already-cached
#: import — a performance no-op for a module that names the stack, never a
#: behaviour change. A false negative would leave a test hitting the REAL
#: ``_verify_identity`` (a live DB query -> 401), so the set errs wide.
_AUTH_STACK_MENTION_TOKENS: tuple[str, ...] = ("farna.api", "farna.auth")

#: Per-file cache for the lazy source scan below: (file path, token) ->
#: does the file contain the token? Filled on demand so the per-test gate
#: costs a dict lookup after the first test of a module.
_MENTION_CACHE: dict[tuple[str, str], bool] = {}


def _test_module_mentions(node: object, token: str) -> bool:
    """True when *node*'s test module's source contains *token*.

    A module-level ``from farna.api.main import app`` (or any other token
    import) is already visible in ``sys.modules`` at fixture-setup time
    (pytest imports test modules during collection), so the caller's
    ``sys.modules`` probe covers it. A handful of modules instead import the
    app / auth stack lazily INSIDE a test or fixture body (e.g.
    ``tests/unit/scim/test_scim_provisioning_programming_error.py``,
    ``tests/unit/trigger_engine/test_trigger_engine.py``); for those,
    ``sys.modules`` alone would wrongly skip the install and leave their
    routes without the fixture's default. The module source is therefore
    scanned once per file as the pre-import signal for that case. A mention
    in a comment or docstring only costs the (cached) import — a false
    positive is a performance no-op, never a behaviour change.

    Generalised from the original ``_test_module_mentions_app`` (FAR-1678)
    so the auth-patch gate and the app gate share one source scan.
    """
    obj = getattr(node, "obj", None)
    modname = getattr(obj, "__module__", None)
    if not modname:
        return False
    module = sys.modules.get(modname)
    path = getattr(module, "__file__", None)
    if not path:
        return False
    key = (path, token)
    cached = _MENTION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        mentions = False
    else:
        mentions = token in source
    _MENTION_CACHE[key] = mentions
    return mentions


#: The module that owns the four provider seams the e2b bridge patches
#: (``_build_dispatch_provider`` / ``_build_file_io_provider`` /
#: ``_build_log_tail_provider`` / ``_build_isolation_provider``). Importing it
#: pulls a ~4.5 s chain (measured 2026-10-13), so the autouse
#: ``_e2b_provider_bridge`` fixture only installs when a test can actually
#: reach the dispatch. ``executor`` and ``graph_cache`` import ``node_runner``
#: at module level, so ANY ``farna.core.pipeline_engine`` load — package
#: ``__init__`` included — puts this marker in ``sys.modules``.
_E2B_BRIDGE_MARKER = "farna.core.pipeline_engine.node_runner"

#: Source tokens whose appearance in a test module marks it as able to reach
#: the dispatch INSIDE its body: an in-body ``from
#: farna.core.pipeline_engine... import`` / ``from ...node_runner import`` /
#: a ``monkeypatch.setattr("farna.core.pipeline_engine.node_runner....")``
#: string target is literally in the module's source, and the fixture decides
#: BEFORE the body runs — so ``sys.modules`` alone would miss it. A comment
#: mention costs only the already-cached import (a performance no-op, never a
#: behaviour change); the cost of a miss is a real provider reaching for a live
#: sandbox, so the set errs wide.
_E2B_BRIDGE_MENTION_TOKENS: tuple[str, ...] = ("pipeline_engine", "node_runner")

#: Seam tokens = the reaching set: a ``tests/unit`` module naming ANY of these
#: patches, pulls or drives the dispatch seams the bridge installs. Every one of
#: them must also carry an ``_E2B_BRIDGE_MENTION_TOKENS`` token, which is what
#: makes ``_e2b_bridge_needed``'s source scan provably non-missing for that set
#: (enforced by ``tests/architecture/test_unit_e2b_bridge_gate.py``).
_E2B_BRIDGE_SEAM_TOKENS: tuple[str, ...] = (
    "node_runner",
    "_sandbox_agent_impl",
    "install_fake_dispatch",
    "fake_file_io",
    "_build_dispatch_provider",
    "_build_file_io_provider",
    "_build_log_tail_provider",
    "_build_isolation_provider",
)


def _e2b_bridge_needed(request: pytest.FixtureRequest) -> bool:
    """Conservative gate for the autouse ``_e2b_provider_bridge`` fixture.

    Returns True (install the bridge) when EITHER:

    1. ``sys.modules`` already holds ``farna.core.pipeline_engine.node_runner``
       — a module-level import in the test module, a conftest, or the app
       (``import farna.api.main`` loads the executor chain), all resolved
       during collection BEFORE fixture setup; the import is already paid, so
       installing costs only the (cheap) seam patching; or
    2. the test module's source mentions ``pipeline_engine`` or ``node_runner``
       — the lazy in-body-import shape, where the import happens during the
       test body, AFTER this fixture has already decided.

    Conservative by construction (the enumeration behind condition 2,
    re-verified 2026-10-13): the four seam call sites live ONLY inside
    ``node_runner`` itself (``_build_log_tail_provider`` /
    ``_build_isolation_provider`` / ``_build_file_io_provider`` /
    ``_build_dispatch_provider`` — each called from ``_sandbox_agent_impl``
    or its helpers), so a test cannot exercise them without loading that
    module. The loading is either already done (condition 1) or written in the
    test module's source (condition 2): every ``tests/unit`` module naming a
    seam token also names a predicate token (arch-pinned), every consumer of
    the ``e2b_bridge`` / ``install_fake_dispatch`` / ``fake_file_io`` /
    ``current_bridge`` / ``original_seam`` helpers does too, and the
    token-less ``e2b.AsyncSandbox`` patchers are provider-layer tests
    (``tests/unit/{core/,}runtime_provider/``, ``db/crud`` org deletion) that
    drive the ABC directly and never call the seams. An under-patch fails
    LOUDLY anyway — without the bridge the real ``_build_dispatch_provider``
    either fails closed (``RuntimeProviderError: ... refusing to provision
    without a provider``) or, with a hub-recognisable key, reaches for a live
    sandbox (observed: E2B HTTP 401 -> ``AuthenticationException``), never a
    silently-wrong green. Pinned by
    ``tests/architecture/test_unit_e2b_bridge_gate.py``.
    """
    if _E2B_BRIDGE_MARKER in sys.modules:
        return True
    return any(_test_module_mentions(request.node, token) for token in _E2B_BRIDGE_MENTION_TOKENS)


def _verify_identity_patch_needed(request: pytest.FixtureRequest) -> bool:
    """Conservative gate for the two auth-stack autouse fixtures.

    Returns True (install the patch/stub) when EITHER:

    1. ``sys.modules`` already holds ``farna.auth.dependencies`` or
       ``farna.api.dependencies`` — a module-level import in the test
       module or any earlier-loaded conftest, resolved before fixture setup;
       or
    2. the test module's source mentions ``farna.api`` or ``farna.auth``
       — the lazy in-body-import shape, where the import happens during the
       test body, AFTER this fixture has already decided.

    Conservative by construction: a test can only reach ``_verify_identity``
    (or observe the system-settings stub) by importing the module that
    defines it, and every such import is either already done (condition 1)
    or written literally in the test module's source (condition 2). The
    corpus check behind condition 2: no test module under ``tests/unit/``
    that lacks both tokens has an in-body import whose chain reaches
    ``farna.api.dependencies`` / ``farna.auth.dependencies``. An
    under-patch fails LOUDLY anyway (the real ``_verify_identity`` queries a
    live DB -> 401/503), never as a silently-wrong green. Pinned by
    ``tests/architecture/test_unit_auth_patch_gate.py``.
    """
    if any(marker in sys.modules for marker in _AUTH_STACK_MARKERS):
        return True
    return any(_test_module_mentions(request.node, token) for token in _AUTH_STACK_MENTION_TOKENS)


#: ``sys.modules`` markers meaning "this session has already loaded a surface
#: that can dial the audit factory". ``farna.core.audit_coverage`` is the
#: primary marker: every audited REST route module (~70 files under
#: ``farna/api/routes/``) imports ``audited`` from it at MODULE level, so
#: loading any of them — including via ``farna.api.main``'s router
#: collection and via ``farna.api.mcp_server`` (which imports route modules
#: at module level) — puts it in ``sys.modules`` before any fixture runs. A
#: test that imports ``audit_coverage`` directly lands here too. The three
#: ``farna.api.*`` entries are belt-and-braces for the MCP surface in case
#: its module-level import chain ever stops pulling the route modules in
#: (``mcp_audit._fresh_session`` itself lazy-imports ``audit_session`` only
#: at call time): ``mcp_server`` defines the ``mcp_audited`` tools,
#: ``mcp_audit`` defines the decorator, ``main`` is the app.
_AUDIT_SURFACE_MARKERS: tuple[str, ...] = (
    "farna.core.audit_coverage",
    "farna.api.main",
    "farna.api.mcp_server",
    "farna.api.mcp_audit",
)

#: Source tokens whose appearance in a test module marks it as able to reach
#: the audited surface INSIDE its body (a lazy ``from farna.api... import`` /
#: ``from farna.core.audit_coverage import ...`` / an
#: ``importlib.import_module("farna.api...")`` literal — all are strings in
#: its source). ``farna.api`` is deliberately one hop wide: any
#: ``farna.api.*`` module either is an audited surface or transitively loads
#: one. A false positive (token in a comment/docstring) costs only the
#: already-cached import — a performance no-op for a module that names the
#: surface, never a behaviour change. A false negative can be SILENT (a
#: fail-open audited route dials the real engine, writes a real audit row on
#: a live Postgres, and logs-and-continues), so the set errs wide and the
#: corpus pin lives in ``tests/architecture/test_unit_audit_double_gate.py``.
_AUDIT_SURFACE_MENTION_TOKENS: tuple[str, ...] = ("farna.api", "farna.core.audit_coverage")


def _audit_session_double_needed(request: pytest.FixtureRequest) -> bool:
    """Conservative gate for the audit-session-double autouse fixture.

    Returns True (install the double) when EITHER:

    1. ``sys.modules`` already holds one of ``_AUDIT_SURFACE_MARKERS`` — a
       module-level import in the test module, a conftest, or any
       earlier-loaded helper, resolved before fixture setup; or
    2. the test module's source mentions ``farna.api`` or
       ``farna.core.audit_coverage`` — the lazy in-body-import shape, where
       the import happens during the test body, AFTER this fixture has
       already decided.

    Conservative by construction: a test can only dial
    ``_shared_session_factory`` (the symbol the double replaces) by running
    an ``audited(...)`` route dependency or an ``mcp_audited`` tool — both of
    which require a module under ``farna.api`` (REST routes / ``main`` /
    ``mcp_server``) or ``farna.core.audit_coverage`` itself to be imported,
    and every such import is either already done (condition 1) or written
    literally in the test module's source (condition 2). The corpus check
    behind condition 2 (enumerated 2026-10-13): of ~1460 files under
    ``tests/unit/``, 391 have a module-level surface import (condition 1),
    61 more mention a token with no module-level import (condition 2), and
    NONE of the remaining ~1005 has any path to the surface — the only
    non-test modules under ``tests/`` that touch it are conftests (whose
    module-level imports land in ``sys.modules``) and
    ``tests/unit/mcp/helpers.py`` (lazy-only, imported exclusively by modules
    that already carry a marker or token). Under-patching is the dangerous
    direction here (silent real audit write, unlike the auth gate's loud
    401), so both conditions err wide. Pinned by
    ``tests/architecture/test_unit_audit_double_gate.py``.
    """
    if any(marker in sys.modules for marker in _AUDIT_SURFACE_MARKERS):
        return True
    return any(_test_module_mentions(request.node, token) for token in _AUDIT_SURFACE_MENTION_TOKENS)


@pytest.fixture(autouse=True)
def _far681_any_credential_default(request: pytest.FixtureRequest) -> None:
    """Default any-credential principal for the apply-moved routes (FAR-681).

    The FAR-681 slice-1/slice-2 endpoints resolve via
    ``get_current_tenant_user_or_api_key``; most endpoint fixtures here only
    override ``get_current_user`` (JWT). Without a default, every call to an
    apply-moved pipeline/trigger route 401s in those fixtures. This autouse
    default DERIVES the mk_ API-key principal from whatever the test's
    ``get_current_user`` override returns (same org/account/role), so the
    any-credential path behaves identically to the JWT path in each test.
    ``setdefault`` semantics: an explicit per-test
    ``get_current_tenant_user_or_api_key`` override always wins. When no JWT
    fixture exists, the wrapper FALLS THROUGH to the real any-credential
    dependency (the request's actual bearer — the real-dependency-stack auth
    tests keep exercising the live mk_ resolution).

    Registered in the UNIT conftest, not ``tests/unit/api/conftest.py``
    (FAR-1229). pytest keys a conftest's autouse-fixture names to the exact
    ``Package`` node object that was current when that conftest was parsed
    (``FixtureManager._node_autousenames`` is keyed by node identity). An
    explicit multi-file argv that detours OUT of ``tests/unit/api/`` and back
    in — e.g.
    ``pytest tests/unit/api/a.py tests/unit/test_x.py tests/unit/api/b.py`` —
    collects the later file under a FRESH ``tests/unit/api`` ``Package`` node
    that never had the conftest's autouse names registered, so the api-level
    autouse fixtures silently drop from that item's closure and its routes
    401. The unit-level conftest's fixtures ride a node shared by every argv
    item under ``tests/unit/``, so they are stable under any interleaving.
    (FAR-1597 moved ``_provisioned_system_engine`` here for the same reason.)

    FAR-1678: the app import is GATED. Installing the override needs the app
    singleton, and importing it cost every unit-test module — including pure
    logic packages that never touch the app — ~10 s of process startup. The
    install now runs only when the app is (or will be) loaded anyway: either
    ``farna.api.main`` is already in ``sys.modules`` (a module-level import
    in the test module or a conftest, resolved at collection time), or the
    test module's source mentions it (the lazy in-body import case). A
    pure-logic module pays neither cost, and its tests cannot observe the
    difference: the override lives on the app singleton, which such a test
    never constructs.
    """
    if _APP_MODULE not in sys.modules and not _test_module_mentions(request.node, _APP_MODULE):
        return

    from fastapi import Depends
    from fastapi.security import HTTPAuthorizationCredentials

    from farna.api.main import app
    from farna.auth.dependencies import (
        _bearer_optional,
        get_current_tenant_user_or_api_key,
        get_current_user,
    )
    from farna.auth.jwt import TenantPrincipal
    from farna.settings import Settings, get_settings

    async def _default_any_credential(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
        settings: Settings = Depends(get_settings),
    ):
        override = app.dependency_overrides.get(get_current_user)
        if override is None:
            return await get_current_tenant_user_or_api_key(credentials, settings)
        auth = override()
        return TenantPrincipal(
            username=auth.username,
            organisation_id=auth.organisation_id,
            account_id=auth.account_id,
            org_role=auth.org_role,
            is_system_admin=auth.is_system_admin,
        )

    app.dependency_overrides.setdefault(get_current_tenant_user_or_api_key, _default_any_credential)


# ---------------------------------------------------------------------------
# AsyncMock coroutine leaks: attribute them to the test that CREATED them
# ---------------------------------------------------------------------------
#
# WHY
# ---
# ``AsyncMock`` auto-creates every attribute as an ``AsyncMock``. When production
# code calls one of those members SYNCHRONOUSLY (``session.add(...)``,
# ``result.scalar_one_or_none()``, ``session.in_transaction()``) it receives a
# coroutine that nothing ever awaits. CPython only reports it when the object is
# finalised - and the object routinely outlives its test (here: it was held in a
# request frame's locals, and that frame was kept alive by the exception
# traceback). The report therefore lands on an arbitrary LATER test on the same
# xdist worker: the 2026-11-01 flake that failed ``test_run_admission``,
# ``trigger_engine``, ``scim``, ``deploy_workflow_rehearsal`` and
# ``test_run_validate_nav_config``, while the actual leak lived in
# ``tests/unit/api/test_self_lockout.py``.
#
# HOW
# ---
# 1. ``AsyncMockMixin._execute_mock_call`` is wrapped ONCE per process. Every
#    coroutine an ``AsyncMock`` call produces is recorded - one list append per
#    mock call. Measured on the 9,783-test ``tests/unit/api`` subset under
#    ``-n auto``: 257.7 s with the guard off vs 258.6 s with it on (+0.3%).
# 2. ``sys.set_coroutine_origin_tracking_depth`` makes CPython record each
#    coroutine's creation stack, so a leaked object can name its creator with
#    no tracemalloc. (``PYTHONTRACEMALLOC=25`` is the usual recipe, but it
#    slowed the 44k-test unit suite past a 40-minute bound; origin tracking
#    costs a bounded stack walk per coroutine instead.)
# 3. At each unit test's teardown, the coroutines recorded since the previous
#    teardown are inspected. One that never started (live ``cr_frame``, unset
#    ``cr_await``, not running) is a never-awaited coroutine, and the test that
#    created it FAILS BY NAME with the creation stack.
#
# NO SILENT SUPPRESSION
# ---------------------
# ``coro.close()`` is never called and no warning is filtered: the leak is
# reported as a hard failure. Reported objects are RETAINED for the rest of the
# session on purpose - dropping the last reference would let the standard
# ``RuntimeWarning`` fire later and fail an innocent test, which is the exact
# mis-attribution this hook exists to remove.
#
# ``MODULO_TEST_COROUTINE_ATTRIBUTION=0`` disables the instrumentation for a
# local debugging session. It is ON by default, so the CI unit run (``pytest
# tests/unit/``) always has it without extra wiring.
_ATTRIBUTION_ENV = "MODULO_TEST_COROUTINE_ATTRIBUTION"
_ATTRIBUTION_ENABLED = os.environ.get(_ATTRIBUTION_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}

#: ``sys.set_coroutine_origin_tracking_depth`` frames per coroutine. Deep enough
#: to walk out of unittest.mock -> the production frame -> the test/fixture that
#: built the double; shallow enough to stay cheap.
_COROUTINE_ORIGIN_DEPTH = 20

#: Coroutines produced by ``AsyncMock`` calls, in creation order.
_RECORDED_COROUTINES: list[object] = []
#: Index into ``_RECORDED_COROUTINES`` already inspected (index-based instead of
#: clearing at setup, so a coroutine created by a fixture that is set up BEFORE
#: this fixture is still attributed rather than silently dropped).
_INSPECTED_UPTO = 0
#: Leaks already reported, retained so the same object cannot fail a later test.
_ATTRIBUTED_LEAKS: list[object] = []
#: Upper bound on retention; a pathological leak loop must not grow the session
#: memory without bound. Past the cap the standard warning may fire again (the
#: originating test has already failed by then).
_ATTRIBUTED_LEAKS_CAP = 256
#: Trim the already-inspected prefix once it grows this large.
_RECORDED_TRIM_THRESHOLD = 8192

_WRAPPER_MARKER = "_modulo_coroutine_attribution_wrapper"


def _executes_recorded(fn: object) -> bool:
    return getattr(fn, _WRAPPER_MARKER, False) is True


def _install_coroutine_recorder() -> None:
    """Wrap ``AsyncMockMixin._execute_mock_call`` once per process."""
    if _executes_recorded(AsyncMockMixin._execute_mock_call):
        return
    original = AsyncMockMixin._execute_mock_call

    def _record_asyncmock_coroutine(self: object, /, *args: object, **kwargs: object) -> object:
        coroutine = original(self, *args, **kwargs)  # type: ignore[misc]
        _RECORDED_COROUTINES.append(coroutine)
        return coroutine

    setattr(_record_asyncmock_coroutine, _WRAPPER_MARKER, True)
    AsyncMockMixin._execute_mock_call = _record_asyncmock_coroutine  # type: ignore[method-assign]


def _is_never_awaited(coroutine: object) -> bool:
    """True for a coroutine that was created and never started.

    A completed coroutine has no frame; a suspended one has ``cr_await`` set
    (the object it is currently awaiting). Neither is the "never awaited"
    defect, so both are excluded.
    """
    if getattr(coroutine, "cr_frame", None) is None:
        return False
    if getattr(coroutine, "cr_running", False):
        return False
    return getattr(coroutine, "cr_await", None) is None


def _format_creation_stack(coroutine: object) -> str:
    """Render the creation stack CPython recorded for *coroutine*.

    Frames inside pytest/pluggy and this module's wrapper are the test harness
    calling the test - they are identical for every leak and never the cause,
    so they are dropped to keep the message pointed at the guilty code. If the
    whole stack is harness frames the raw list is rendered instead, so a
    documented "no stack" never hides real information.
    """
    origin = getattr(coroutine, "cr_origin", None)
    if not origin:
        return "  (no creation stack was recorded for this object)"
    frames = [
        (filename, lineno, funcname)
        for filename, lineno, funcname in reversed(origin)
        if funcname != "_record_asyncmock_coroutine"
        and "/site-packages/_pytest/" not in filename
        and "/site-packages/pluggy/" not in filename
    ]
    if not frames:
        frames = [(filename, lineno, funcname) for filename, lineno, funcname in reversed(origin)]
    lines = ["  Coroutine created at (most recent call last):"]
    for filename, lineno, funcname in frames:
        lines.append(f'    File "{filename}", line {lineno}, in {funcname}')
        source = linecache.getline(filename, lineno).strip()
        if source:
            lines.append(f"      {source}")
    return "\n".join(lines)


def _report_leaks(nodeid: str, leaks: list[object]) -> None:
    """Fail *nodeid* for every never-awaited AsyncMock coroutine it created."""
    details = "\n\n".join(
        f"[{index}] {_format_creation_stack(coroutine)}" for index, coroutine in enumerate(leaks, start=1)
    )
    pytest.fail(
        f"{len(leaks)} never-awaited AsyncMock coroutine(s) created during {nodeid}.\n\n"
        "A synchronous call on an AsyncMock - or a synchronous read of an AsyncMock's\n"
        "result - returns a coroutine that nothing awaits. Type the double to match the\n"
        "real contract: MagicMock for synchronous members (session.add / in_transaction /\n"
        "get_bind) and a plain MagicMock result for `await session.execute(...)` whose\n"
        "readers (scalar_one_or_none / first / scalars / all) are called synchronously.\n"
        "See tests/unit/api/mock_session.py.\n\n"
        f"{details}",
        pytrace=False,
    )


if _ATTRIBUTION_ENABLED:
    # Enabled before any test module is imported, so every coroutine created in
    # this session carries its creation stack.
    sys.set_coroutine_origin_tracking_depth(_COROUTINE_ORIGIN_DEPTH)
    _install_coroutine_recorder()


@pytest.fixture(autouse=True)
def _attribute_leaked_asyncmock_coroutines(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Fail the test that created a never-awaited AsyncMock coroutine.

    Index-based rather than "clear at setup": a coroutine created by a fixture
    that runs before this one is still inside the window, so nothing is silently
    dropped. The scan is a list slice plus a cheap predicate per object - it
    costs nothing measurable when no leak exists, and it only builds a creation
    stack (``cr_origin``) for the object already known to be leaking.
    """
    yield
    if not _ATTRIBUTION_ENABLED:
        return

    global _INSPECTED_UPTO
    pending = _RECORDED_COROUTINES[_INSPECTED_UPTO:]
    _INSPECTED_UPTO = len(_RECORDED_COROUTINES)
    if _INSPECTED_UPTO > _RECORDED_TRIM_THRESHOLD:
        del _RECORDED_COROUTINES[:_INSPECTED_UPTO]
        _INSPECTED_UPTO = 0

    leaks = [coroutine for coroutine in pending if _is_never_awaited(coroutine)]
    if not leaks:
        return

    # Retain the reported objects so their RuntimeWarning cannot be re-emitted
    # against a later, innocent test. The leak itself is a hard failure below.
    if len(_ATTRIBUTED_LEAKS) < _ATTRIBUTED_LEAKS_CAP:
        _ATTRIBUTED_LEAKS.extend(leaks)

    _report_leaks(request.node.nodeid, leaks)
