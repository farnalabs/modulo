"""Admin tiers endpoint — lists all known tiers and their display labels."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
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
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(status_code=501, detail="Database not available. Run migrations.") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database error occurred.") from None
    except Exception:
        logger.exception("Unexpected error in list_tiers_endpoint")
        raise HTTPException(status_code=500, detail="Internal server error") from None
