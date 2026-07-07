"""Admin tiers endpoint — lists all known tiers and their display labels."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.tier_catalog import list_tiers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tiers", tags=["admin-tiers"])


@router.get("")
async def list_tiers_endpoint(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    try:
        async with session.begin():
            tiers = await list_tiers(session)
        return {"tiers": tiers}
    except HTTPException:
        raise
    except ProgrammingError:
        raise HTTPException(status_code=503, detail="Database not available. Run migrations.")
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Resource already exists or constraint violation.")
    except Exception as e:
        logger.error("Unexpected error in list_tiers_endpoint: %s", str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
