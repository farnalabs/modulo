"""Sensitive data masking utilities and reveal endpoint.

Provides DOM-safe masking for credentials, API keys, and secrets returned in
API responses. A server-authenticated reveal endpoint allows temporary
30-second unmasking via Redis-backed tokens.
"""

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, PlainSerializer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

SENSITIVE_VALUE_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022"

_SENSITIVE_ENV_KEYS: frozenset[str] = frozenset({
    "MODULO_USERS",
    "DATABASE_URL",
    "PYPI_TOKEN",
})

_SENSITIVE_KEY_PATTERNS = frozenset({
    "token", "secret", "api_key", "password", "passwd", "key", "credential",
    "database_url", "encryption", "signing", "private",
})


def is_sensitive_key(key: str) -> bool:
    key_lower = key.lower().replace("-", "_").replace(" ", "_")
    return any(pattern in key_lower for pattern in _SENSITIVE_KEY_PATTERNS)


def is_sensitive_env_key(key: str) -> bool:
    return key in _SENSITIVE_ENV_KEYS or is_sensitive_key(key)


def mask_sensitive_value(value: str) -> str:
    return SENSITIVE_VALUE_MASK if value else value


def mask_config_json(config: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (mask_sensitive_value(v) if isinstance(v, str) and is_sensitive_key(k) else v)
        for k, v in config.items()
    }


SensitiveValue = Annotated[
    str,
    PlainSerializer(
        lambda v: SENSITIVE_VALUE_MASK if v else v,
        return_type=str,
        when_used="always",
    ),
]


router = APIRouter(prefix="/api/v1/admin/sensitive", tags=["sensitive"])


class RevealRequest(BaseModel):
    resource_type: str
    resource_id: str
    field: str | None = None


class RevealResponse(BaseModel):
    token: str
    value: str
    expires_in_seconds: int = 30


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can reveal sensitive values",
        )


async def _fetch_value(
    body: RevealRequest,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
) -> str:
    resource_id = body.resource_id
    field = body.field

    if body.resource_type == "connector":
        from modulo.db.models.connector_instance import ConnectorInstance

        result = await session.execute(
            select(ConnectorInstance).where(
                ConnectorInstance.id == uuid.UUID(resource_id),
                ConnectorInstance.organisation_id == principal.organisation_id,
            )
        )
        ci = result.scalar_one_or_none()
        if ci is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
        raw = ci.config_json.get(field, "") if field else json.dumps(ci.config_json)
        return raw if isinstance(raw, str) else json.dumps(raw)

    if body.resource_type == "sso_provider":
        result = await session.execute(
            select(SsoProvider).where(
                SsoProvider.id == uuid.UUID(resource_id),
                SsoProvider.organisation_id == principal.organisation_id,
            )
        )
        provider = result.scalar_one_or_none()
        if provider is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO provider not found")
        return provider.client_secret or ""

    if body.resource_type == "observability":
        from modulo.db.models.organisation import Organisation

        result = await session.execute(
            select(Organisation.otel_config_json).where(Organisation.id == principal.organisation_id)
        )
        row = result.scalar_one_or_none()
        config = row or {}
        if field:
            return config.get(field, "")
        return json.dumps(config)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown resource_type: {body.resource_type}",
    )


@router.post("/reveal", response_model=RevealResponse)
async def reveal_sensitive_value(
    body: RevealRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> RevealResponse:
    _require_admin(principal)

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        actual_value = await _fetch_value(body, session, principal)

    token = str(uuid.uuid4())
    redis: Redis | None = None
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.setex(f"sensitive_reveal:{token}", 30, actual_value)
    except Exception:
        _log.warning("Redis unavailable for sensitive reveal token", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()

    return RevealResponse(token=token, value=actual_value, expires_in_seconds=30)
