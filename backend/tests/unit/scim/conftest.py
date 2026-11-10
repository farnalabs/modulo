"""Test configuration for SCIM unit tests.

Sets minimal env vars so ``get_settings()`` (called by module-level imports
in ``modulo.api.main``) can construct a ``Settings`` instance. Shared
constants and factories live in ``helpers.py``; the SCIM ``audited()`` seams
and the dependency-override lifecycle live here so every SCIM test module
gets them exactly once.
"""

import os
from collections.abc import AsyncGenerator, Callable, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.unit.scim.helpers import ORG_ID, USER_ID, make_mock_session, make_settings

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")


@pytest.fixture(autouse=True)
def stub_scim_audit() -> Generator[None, None, None]:
    """FAR-1472: stub the two seams the fail-closed ``audited(...)`` dependency
    needs — the fresh ``audit_session`` (a real engine; the unit tier has no
    database) and the SCIM actor resolver, whose admin-caller query runs on the
    route session these tests replace with a bare double. The dependency itself
    (and its fail-closed policy) still runs. Covered end-to-end for the real
    resolver by ``TestScimAuditPrincipal``."""

    from farna.api.main import app
    from farna.api.routes.scim import get_scim_audit_principal
    from farna.auth.jwt import TenantPrincipal
    from farna.core.audit_coverage import audit_session
    from tests.unit.api.mock_session import configure_mock_session

    async def _audit_session() -> AsyncGenerator[AsyncMock, None]:
        session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    async def _audit_principal() -> TenantPrincipal:
        return TenantPrincipal(
            username="scim",
            organisation_id=ORG_ID,
            account_id=USER_ID,
            org_role="admin",
        )

    app.dependency_overrides[audit_session] = _audit_session
    app.dependency_overrides[get_scim_audit_principal] = _audit_principal
    yield
    app.dependency_overrides.pop(audit_session, None)
    app.dependency_overrides.pop(get_scim_audit_principal, None)


@pytest.fixture
def scim_client() -> Generator[Callable[..., TestClient], None, None]:
    """Build a ``TestClient`` with the standard SCIM dependency doubles.

    Tests that need non-default ``Settings`` (no token, no licence, empty
    public URL) or a custom plan context call this instead of editing
    ``app.dependency_overrides`` by hand. Overrides are removed in teardown, so
    a failing assertion cannot leak state into the next test.
    """

    from farna.api.dependencies import _get_engine, get_db_session
    from farna.api.main import app
    from farna.auth.scim_auth import ScimPrincipal, get_scim_plan_context, get_scim_principal
    from farna.settings import get_settings

    installed: list[Callable[..., object]] = []

    def _install(
        *,
        settings: Callable[[], object] = make_settings,
        session: MagicMock | None = None,
        principal: bool = True,
        plan_context: Callable[[], object] | None = None,
    ) -> TestClient:
        mock_session = session if session is not None else make_mock_session()

        async def _override_session() -> AsyncGenerator[MagicMock, None]:
            yield mock_session

        overrides: dict[Callable[..., object], Callable[..., object]] = {
            # Wrap the factory: FastAPI inspects the provider's signature, and
            # ``make_settings(**overrides)`` would expose a required ``overrides``
            # query parameter on every route the dependency is attached to.
            get_settings: lambda: settings(),
            get_db_session: _override_session,
            _get_engine: lambda: MagicMock(),
        }
        if principal:
            overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=ORG_ID)
        if plan_context is not None:
            overrides[get_scim_plan_context] = plan_context

        for dependency, provider in overrides.items():
            app.dependency_overrides[dependency] = provider
            installed.append(dependency)
        return TestClient(app)

    yield _install
    for dependency in installed:
        app.dependency_overrides.pop(dependency, None)
