"""Admin tiers endpoint — lists all known tiers and their display labels."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.tier_catalog import list_tiers
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tiers", tags=["admin-tiers"])


@handle_db_errors("admin.tiers.list_tiers_endpoint")
@router.get("")
async def list_tiers_endpoint(
    settings: Settings = Depends(get_settings),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    # Attempt Redis cache read (300s TTL — tiers don't change often)
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        cache_key = f"tiers:{current_user.organisation_id}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        logger.warning("tiers.cache_read_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()

    try:
        async with session.begin():
            tiers = await list_tiers(session)

        response_data = {"tiers": tiers}

        # Write to Redis cache (best-effort, 300s TTL)
        try:
            redis = Redis.from_url(
                settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
            )
            cache_key = f"tiers:{current_user.organisation_id}"
            await redis.setex(cache_key, 300, json.dumps(response_data, default=str))
        except Exception:
            logger.warning("tiers.cache_write_failed", exc_info=True)
        finally:
            if redis is not None:
                await redis.aclose()

        return response_data
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
