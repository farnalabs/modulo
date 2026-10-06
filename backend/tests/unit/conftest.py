"""Global fixtures for all unit tests."""

from unittest.mock import AsyncMock, patch

import pytest

from tests.helpers.audit_session import patch_audit_session_factory
from tests.unit._e2b_sandbox_bridge import install_bridge


@pytest.fixture(autouse=True)
def _isolated_audit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the ``audited(...)`` fresh-session write to an in-memory double.

    ``audit_coverage.audit_session`` builds its session from
    ``_shared_session_factory`` (lazy-imported process-shared engine), so
    patching that factory covers every annotated route in the unit suite -
    including tests that ``dependency_overrides.clear()`` mid-test, which would
    defeat a dependency override on ``audit_session`` itself. Integration tests
    are deliberately untouched: they run against real Postgres and exercise the
    genuine append.
    """
    patch_audit_session_factory(monkeypatch)


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
