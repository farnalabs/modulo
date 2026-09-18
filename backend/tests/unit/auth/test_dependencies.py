"""Tests for authentication dependency claim boundaries."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from modulo.auth.dependencies import (
    AccountNotFound,
    OrganisationMembershipNotFound,
    OrganisationNotFound,
    _verify_identity,
    get_current_tenant_user,
    require_system_admin,
)
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal


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
