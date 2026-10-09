"""Global fixtures for all unit tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

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
def _patch_verify_identity() -> None:
    """Prevent _verify_identity from connecting to a real database.

    The _verify_identity function in auth/dependencies creates its own
    database engine and queries the real DB to check account/org existence.
    This bypasses all FastAPI dependency overrides, causing 401 errors when
    the local Postgres is running but the test UUIDs don't match real data.
    """
    with patch("modulo.auth.dependencies._verify_identity", new=AsyncMock(return_value=None)):
        yield


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
