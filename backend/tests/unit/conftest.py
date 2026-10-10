"""Global fixtures for all unit tests."""

from collections.abc import Generator
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
def _patch_verify_identity(monkeypatch: pytest.MonkeyPatch) -> None:
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
    """
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _e2b_provider_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
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
    """
    monkeypatch.setenv("E2B_API_KEY", "bridge-test-key")
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
def _provisioned_system_engine(monkeypatch: pytest.MonkeyPatch) -> None:
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
    is loaded before any test runs — so the provisioning is stable under
    any collection order.
    """
    from modulo.api import dependencies as _deps

    monkeypatch.setattr(_deps, "get_settings", lambda: _ProvisionedSystemSettings())


@pytest.fixture(autouse=True)
def _far681_any_credential_default() -> None:
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
    """
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
