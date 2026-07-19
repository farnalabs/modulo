"""Tests for authentication dependency claim boundaries."""

import uuid

import pytest
from fastapi import HTTPException

from modulo.auth.dependencies import get_current_tenant_user
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

    result = await get_current_tenant_user(principal)

    assert isinstance(result, TenantPrincipal)
    assert result.organisation_id == organisation_id
    assert result.account_id == account_id
    assert result.org_role == "admin"


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
