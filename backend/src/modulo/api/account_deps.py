"""Account-level routing dependency for FastAPI.

Resolves the current user's CustomerAccount context via their UserMembership.
This sits on top of the existing org-level RLS system.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.user_membership import resolve_account_for_user, resolve_role_for_user_in_ca
from modulo.db.models.customer_account import CustomerAccount

_log = logging.getLogger(__name__)


class AccountContext:
    """Resolved account context for the current request."""

    def __init__(
        self,
        account_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        account: CustomerAccount,
    ) -> None:
        self.account_id = account_id
        self.user_id = user_id
        self.role = role
        self.account = account


async def get_current_account(
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> AccountContext:
    """Resolve the current user's CustomerAccount context.

    Returns 403 if the user has no active account membership.
    """
    try:
        async with session.begin():
            account = await resolve_account_for_user(session, principal.account_id)
            if account is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No active account membership found.",
                )
            role = await resolve_role_for_user_in_ca(session, principal.account_id, account.id)
            if role is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No active account membership found.",
                )
            return AccountContext(
                account_id=account.id,
                user_id=principal.account_id,
                role=role,
                account=account,
            )
    except HTTPException:
        raise
    except Exception:
        _log.exception("get_current_account failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve account context.",
        ) from None
