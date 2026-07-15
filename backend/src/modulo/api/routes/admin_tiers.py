"""Admin tiers endpoint — lists all known tiers and their display labels."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.tier_catalog import list_tiers

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tiers", tags=["admin-tiers"])


@router.get("")
@handle_db_errors("admin.tiers.list_tiers_endpoint")
@router.get("")
async def list_tiers_endpoint(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        async with session.begin():
            tiers = await list_tiers(session)
        return {"tiers": tiers}
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except ProgrammingError:
        raise HTTPException(status_code=501, detail="Database not available. Run migrations.") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database error occurred.") from None
    except Exception:
        logger.exception("Unexpected error in list_tiers_endpoint")
        raise HTTPException(status_code=500, detail="Internal server error") from None
