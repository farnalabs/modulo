"""Admin tiers endpoint — lists all known tiers and their display labels."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.tier_catalog import list_tiers

router = APIRouter(prefix="/api/v1/admin/tiers", tags=["admin-tiers"])


@router.get("")
async def list_tiers_endpoint(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    tiers = await list_tiers(session)
    return {"tiers": tiers}
