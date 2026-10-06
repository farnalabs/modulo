"""Tests for authentication dependency claim boundaries."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import InvalidRequestError, OperationalError, SQLAlchemyError

from modulo.api.db_error_handling import MSG_SESSION_CONTRACT
from modulo.auth.dependencies import (
    AccountNotFound,
    OrganisationMembershipNotFound,
    OrganisationNotFound,
    _verify_identity,
    get_current_tenant_user,
    get_current_tenant_user_or_api_key,
    require_system_admin,
)
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings


@pytest.mark.asyncio
async def test_tenant_dependency_returns_validated_principal() -> None:
    organisation_id = uuid.uuid4()
    account_id = uuid.uuid4()
    principal = AuthenticatedPrincipal(
        username="tenant@example.com",
        organisation_id=organisation_id,
        account_id=account_id,
        org_role="admin",
    )

    from unittest.mock import patch

    with patch("modulo.auth.dependencies._verify_identity", return_value="admin"):
        result = await get_current_tenant_user(principal)

    assert isinstance(result, TenantPrincipal)
    assert result.organisation_id == organisation_id
    assert result.account_id == account_id
    assert result.org_role == "admin"  # _verify_identity returns the live role; degraded to claim when DB unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("organisation_id", "org_role"),
    [(None, "admin"), (uuid.uuid4(), None)],
    ids=["missing_org_id", "missing_org_role"],
)
async def test_tenant_dependency_rejects_missing_tenant_claims(
    organisation_id: uuid.UUID | None,
    org_role: str | None,
) -> None:
    principal = AuthenticatedPrincipal(
        username="system@example.com",
        organisation_id=organisation_id,
        account_id=uuid.uuid4(),
        org_role=org_role,
        is_system_admin=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_current_tenant_user(principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Organisation membership required"


def _principal(org_role: str | None = "admin", is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="tenant@example.com",
        organisation_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role=org_role,
        is_system_admin=is_system_admin,
    )


def _patch_identity_verify(*, rows: list[object], live_role: str | None) -> tuple[MagicMock, AsyncMock]:
    """Patch the engine/session plumbing used by _verify_identity.

    ``rows`` is consumed in order by the two SELECT EXISTS lookups (account,
    org) and then the live-role read. ``live_role`` short-circuits
    resolve_role_from_membership. Returns the patches followed by the role
    AsyncMock so callers can assert on it directly.
    """
    session = AsyncMock()
    results = []
    for row in rows:
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        results.append(result)
    session.execute = AsyncMock(side_effect=results)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    role_mock = AsyncMock(return_value=live_role)
    return (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
        patch("modulo.auth.dependencies.resolve_role_from_membership", role_mock),
        role_mock,
        session,
    )


@pytest.mark.asyncio
async def test_verify_identity_returns_live_role_when_membership_exists() -> None:
    engine_patch, factory_patch, settings_patch, role_patch, role_mock, _session = _patch_identity_verify(
        rows=[1, 1], live_role="admin"
    )

    with engine_patch, factory_patch, settings_patch, role_patch:
        role = await _verify_identity(_principal())

    assert role == "admin"
    role_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_identity_raises_account_not_found() -> None:
    engine_patch, factory_patch, settings_patch, role_patch, role_mock, _session = _patch_identity_verify(
        rows=[None, 1], live_role="admin"
    )

    with (
        engine_patch,
        factory_patch,
        settings_patch,
        role_patch,
        pytest.raises(AccountNotFound) as exc_info,
    ):
        await _verify_identity(_principal())

    assert exc_info.value.status_code == 401
    role_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_identity_raises_org_not_found() -> None:
    engine_patch, factory_patch, settings_patch, role_patch, role_mock, _session = _patch_identity_verify(
        rows=[1, None], live_role="admin"
    )

    with (
        engine_patch,
        factory_patch,
        settings_patch,
        role_patch,
        pytest.raises(OrganisationNotFound) as exc_info,
    ):
        await _verify_identity(_principal())

    assert exc_info.value.status_code == 401
    role_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_identity_raises_membership_not_found_when_no_live_role() -> None:
    engine_patch, factory_patch, settings_patch, role_patch, role_mock, _session = _patch_identity_verify(
        rows=[1, 1], live_role=None
    )

    with (
        engine_patch,
        factory_patch,
        settings_patch,
        role_patch,
        pytest.raises(OrganisationMembershipNotFound) as exc_info,
    ):
        await _verify_identity(_principal())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Organisation membership required"
    role_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_identity_sqlalchemy_error_maps_to_503() -> None:
    engine_patch, factory_patch, settings_patch, role_patch, _role_mock, session = _patch_identity_verify(
        rows=[1], live_role="admin"
    )
    session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))

    with (
        engine_patch,
        factory_patch,
        settings_patch,
        role_patch,
        pytest.raises(HTTPException) as exc_info,
    ):
        await _verify_identity(_principal())

    assert exc_info.value.status_code == 503
    assert "temporarily unavailable" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_identity_invalid_request_error_maps_to_500_not_503() -> None:
    """FAR-1481: a session-contract violation is a programming bug, not an outage.

    Before the guard the arm's ``except SQLAlchemyError`` caught the
    ``InvalidRequestError`` (a subclass) and answered 503 "Role verification
    temporarily unavailable" — a retry-inviting outage reply for a
    non-retryable local bug. Fail-before evidence: this test fails (503)
    without ``raise_session_contract_error`` as the arm's first statement.
    """
    engine_patch, factory_patch, settings_patch, role_patch, _role_mock, session = _patch_identity_verify(
        rows=[1], live_role="admin"
    )
    session.execute = AsyncMock(
        side_effect=InvalidRequestError("Autobegin is disabled on this Session; please call session.begin()")
    )

    with (
        engine_patch,
        factory_patch,
        settings_patch,
        role_patch,
        pytest.raises(HTTPException) as exc_info,
    ):
        await _verify_identity(_principal())

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == MSG_SESSION_CONTRACT


@pytest.mark.asyncio
async def test_verify_identity_operational_error_still_maps_to_503() -> None:
    """A GENUINE transient DB fault must keep the arm's 503 behaviour (FAR-1481)."""
    engine_patch, factory_patch, settings_patch, role_patch, _role_mock, session = _patch_identity_verify(
        rows=[1], live_role="admin"
    )
    session.execute = AsyncMock(side_effect=OperationalError("select 1", {}, Exception("connection lost")))

    with (
        engine_patch,
        factory_patch,
        settings_patch,
        role_patch,
        pytest.raises(HTTPException) as exc_info,
    ):
        await _verify_identity(_principal())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Role verification temporarily unavailable. Please try again."


@pytest.mark.asyncio
async def test_require_system_admin_rejects_non_admin() -> None:
    principal = _principal()

    with pytest.raises(HTTPException) as exc_info:
        await require_system_admin(principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "System admin role required"


@pytest.mark.asyncio
async def test_require_system_admin_passes_system_admin() -> None:
    principal = _principal(is_system_admin=True)

    result = await require_system_admin(principal)

    assert result is principal


# ---------------------------------------------------------------------------
# FAR-1481: the mk_ API-key resolution arm (get_current_tenant_user_or_api_key)
# carries the SAME unguarded except-SQLAlchemyError -> 503 shape the
# _verify_identity arm had; both were converted in one sweep.
# ---------------------------------------------------------------------------

_API_KEY = "mk_12345678_" + "x" * 32


def _api_key_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _api_key_failing_factory(side_effect: Exception) -> MagicMock:
    """Session factory whose first execute raises — the arm's first DB touch."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Sync mocks: _ensure_active_transaction() calls these synchronously (a
    # coroutine-returning AsyncMock would be created and never awaited).
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock()
    session.get_bind.return_value.dialect.name = "sqlite"
    session.execute = AsyncMock(side_effect=side_effect)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _credentials() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=_API_KEY)


@pytest.mark.asyncio
async def test_api_key_resolution_invalid_request_error_maps_to_500_not_503() -> None:
    """FAR-1481: the mk_ key path's session-contract violation is a 500, not a 503.

    Fail-before evidence: without the leading ``raise_session_contract_error``
    guard this answers 503 "Database temporarily unavailable." and fails.
    """
    factory = _api_key_failing_factory(
        InvalidRequestError("Autobegin is disabled on this Session; please call session.begin()")
    )

    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _api_key_settings())

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == MSG_SESSION_CONTRACT


@pytest.mark.asyncio
async def test_api_key_resolution_operational_error_still_maps_to_503() -> None:
    """A GENUINE transient DB fault on the mk_ path keeps the arm's 503 (FAR-1481)."""
    factory = _api_key_failing_factory(OperationalError("select 1", {}, Exception("connection lost")))

    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _api_key_settings())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Database temporarily unavailable."
