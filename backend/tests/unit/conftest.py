"""Global fixtures for all unit tests."""

import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.helpers.audit_session import install_audit_session_double
from tests.unit._e2b_sandbox_bridge import install_bridge


@pytest.fixture(autouse=True)
def _isolated_audit_session(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Route the ``audited(...)`` fresh-session write to an in-memory double.

    See ``tests.helpers.audit_session`` for why the double exists and why
    integration tests are deliberately left to exercise the genuine append.

    Installs the double through a scoped ``monkeypatch.context()`` and yields
    from inside it, so the unpatch of ``_shared_session_factory`` runs as this
    fixture's teardown - after the test body - rather than during ordinary
    finalization while in-flight ``fail_closed=True`` destroy routes are still
    finalizing.
    """
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
    ``modulo.auth.dependencies`` (~5 s measured 2026-10-13), and paying that
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
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))


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
    ``modulo.core.pipeline_engine.node_runner`` — a ~4.5 s chain measured
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

    ``modulo.api.dependencies`` resolves its settings via the module-level
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
    ``modulo.api.dependencies``, which itself executes
    ``modulo.auth.dependencies`` at module level (measured 2026-10-13) — so
    running it for every unit test is what pins pure-logic sessions onto the
    auth/app import stack even when the ``_verify_identity`` patch is gated.
    A test can only OBSERVE this stub by touching ``modulo.api.dependencies``
    (the object being patched), and every such test either already has it in
    ``sys.modules`` (module-level import, resolved before fixture setup) or
    names ``modulo.api`` / ``modulo.auth`` in its source (lazy in-body
    import; the only other chains into ``api.dependencies`` verified in the
    test corpus — ``modulo.auth.scim_auth`` — carry the ``modulo.auth``
    token too). Skip cannot strand a consumer; it only spares tests that
    never load the module being patched.
    """
    if not _verify_identity_patch_needed(request):
        return
    from modulo.api import dependencies as _deps

    monkeypatch.setattr(_deps, "get_settings", lambda: _ProvisionedSystemSettings())


#: The module whose import constructs the FastAPI application singleton
#: (``modulo.api.main.app``). Loading it costs ~10 s of CPU-bound route and
#: pydantic-model construction (measured 2026-10-11, FAR-1678), so the unit
#: suite must only load it when a test can actually reach the app.
_APP_MODULE = "modulo.api.main"

#: The module holding ``_verify_identity`` (and FastAPI's auth dependencies).
#: Importing it costs ~5 s (measured 2026-10-13), so the autouse
#: ``_patch_verify_identity`` / ``_provisioned_system_engine`` fixtures gate
#: on it instead of paying it for every unit test.
_AUTH_DEPS_MODULE = "modulo.auth.dependencies"

#: ``sys.modules`` markers meaning "this session has already loaded the
#: auth/app stack". ``modulo.api.dependencies`` is included because it
#: executes ``modulo.auth.dependencies`` at module level, so its presence
#: means the expensive import is paid regardless — patching then costs only
#: the (cheap) setattr.
_AUTH_STACK_MARKERS: tuple[str, ...] = (_AUTH_DEPS_MODULE, "modulo.api.dependencies")

#: Source tokens whose appearance in a test module marks it as able to reach
#: the auth/app stack INSIDE its body (a lazy ``from modulo.api... import`` /
#: ``from modulo.auth... import`` is a string in its source). ``modulo.api``
#: is deliberately one hop wide: any ``modulo.api.*`` import transitively
#: executes ``modulo.auth.dependencies``, so catching only ``_AUTH_DEPS_MODULE``
#: would miss e.g. an in-body ``from modulo.api.main import app``. A false
#: positive (token in a comment/docstring) costs only the already-cached
#: import — a performance no-op for a module that names the stack, never a
#: behaviour change. A false negative would leave a test hitting the REAL
#: ``_verify_identity`` (a live DB query -> 401), so the set errs wide.
_AUTH_STACK_MENTION_TOKENS: tuple[str, ...] = ("modulo.api", "modulo.auth")

#: Per-file cache for the lazy source scan below: (file path, token) ->
#: does the file contain the token? Filled on demand so the per-test gate
#: costs a dict lookup after the first test of a module.
_MENTION_CACHE: dict[tuple[str, str], bool] = {}


def _test_module_mentions(node: object, token: str) -> bool:
    """True when *node*'s test module's source contains *token*.

    A module-level ``from modulo.api.main import app`` (or any other token
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


def _verify_identity_patch_needed(request: pytest.FixtureRequest) -> bool:
    """Conservative gate for the two auth-stack autouse fixtures.

    Returns True (install the patch/stub) when EITHER:

    1. ``sys.modules`` already holds ``modulo.auth.dependencies`` or
       ``modulo.api.dependencies`` — a module-level import in the test
       module or any earlier-loaded conftest, resolved before fixture setup;
       or
    2. the test module's source mentions ``modulo.api`` or ``modulo.auth``
       — the lazy in-body-import shape, where the import happens during the
       test body, AFTER this fixture has already decided.

    Conservative by construction: a test can only reach ``_verify_identity``
    (or observe the system-settings stub) by importing the module that
    defines it, and every such import is either already done (condition 1)
    or written literally in the test module's source (condition 2). The
    corpus check behind condition 2: no test module under ``tests/unit/``
    that lacks both tokens has an in-body import whose chain reaches
    ``modulo.api.dependencies`` / ``modulo.auth.dependencies``. An
    under-patch fails LOUDLY anyway (the real ``_verify_identity`` queries a
    live DB -> 401/503), never as a silently-wrong green. Pinned by
    ``tests/architecture/test_unit_auth_patch_gate.py``.
    """
    if any(marker in sys.modules for marker in _AUTH_STACK_MARKERS):
        return True
    return any(_test_module_mentions(request.node, token) for token in _AUTH_STACK_MENTION_TOKENS)


#: The module that owns the four provider seams the e2b bridge patches
#: (``_build_dispatch_provider`` / ``_build_file_io_provider`` /
#: ``_build_log_tail_provider`` / ``_build_isolation_provider``). Importing it
#: pulls a ~4.5 s chain (measured 2026-10-13), so the autouse
#: ``_e2b_provider_bridge`` fixture only installs when a test can actually
#: reach the dispatch. ``executor`` and ``graph_cache`` import ``node_runner``
#: at module level, so ANY ``modulo.core.pipeline_engine`` load — package
#: ``__init__`` included — puts this marker in ``sys.modules``.
_E2B_BRIDGE_MARKER = "modulo.core.pipeline_engine.node_runner"

#: Source tokens whose appearance in a test module marks it as able to reach
#: the dispatch INSIDE its body: an in-body ``from
#: modulo.core.pipeline_engine... import`` / ``from ...node_runner import`` /
#: a ``monkeypatch.setattr("modulo.core.pipeline_engine.node_runner....")``
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

    1. ``sys.modules`` already holds ``modulo.core.pipeline_engine.node_runner``
       — a module-level import in the test module, a conftest, or the app
       (``import modulo.api.main`` loads the executor chain), all resolved
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
    ``modulo.api.main`` is already in ``sys.modules`` (a module-level import
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

    from modulo.api.main import app
    from modulo.auth.dependencies import (
        _bearer_optional,
        get_current_tenant_user_or_api_key,
        get_current_user,
    )
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import Settings, get_settings

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
