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
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_system_or_org_admin
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import SecretStorageError, decode_stored_secret
from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

# Re-exported so API-layer callers can import from the documented location.
# Required because mypy runs under `strict` (no_implicit_reexport = True).
__all__ = ["SENSITIVE_VALUE_MASK"]

_log = logging.getLogger(__name__)

# The DOM-side mask constant lives in :mod:`modulo.core.secret_patterns` (the
# single source of truth) so the two modules can never drift. The canonical
# secret-format redaction patterns, the value-pattern list and the shared raw
# patterns (``SECRET_VALUE_PATTERNS``, ``mask_secret_values_in_text``,
# ``GITHUB_PAT_PATTERN``, ``AWS_ACCESS_KEY_PATTERN``) are also defined ONCE in
# :mod:`modulo.core.secret_patterns`; the API layer (runs.py) imports them
# directly from there. They live in ``core`` (not here) so the core redaction
# sites (error_codes.py, node_runner.py, soc2.py) can use the same definitions
# without violating the ``core-does-not-import-api`` contract.

_SENSITIVE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "MODULO_USERS",
        "DATABASE_URL",
        "PYPI_TOKEN",
    }
)

_SENSITIVE_KEY_PATTERNS = frozenset(
    {
        "token",
        "secret",
        "api_key",
        "password",
        "passwd",
        "key",
        "credential",
        "database_url",
        "encryption",
        "signing",
        "private",
    }
)


def is_sensitive_key(key: str) -> bool:
    key_lower = key.lower().replace("-", "_").replace(" ", "_")
    return any(pattern in key_lower for pattern in _SENSITIVE_KEY_PATTERNS)


def is_sensitive_env_key(key: str) -> bool:
    return key in _SENSITIVE_ENV_KEYS or is_sensitive_key(key)


def mask_sensitive_value(value: str) -> str:
    return SENSITIVE_VALUE_MASK if value else value


def mask_config_json(config: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (mask_sensitive_value(v) if isinstance(v, str) and is_sensitive_key(k) else v) for k, v in config.items()
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


async def _fetch_value(
    payload: RevealRequest,
    session: AsyncSession,
    principal: TenantPrincipal,
    settings: Settings,
) -> str:
    resource_id = payload.resource_id
    field = payload.field

    try:
        resource_uuid = uuid.UUID(resource_id)
    except ValueError as exc:
        # The path body field is an untyped str (RevealRequest.resource_id);
        # parse it up front so a malformed id is a clean 400 instead of an
        # uncaught ValueError bubbling out of every resource branch as a 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_id must be a valid UUID",
        ) from exc

    if payload.resource_type == "connector":
        from modulo.db.models.connector_instance import ConnectorInstance

        connector_result = await session.execute(
            select(ConnectorInstance).where(
                ConnectorInstance.id == resource_uuid,
                ConnectorInstance.organisation_id == principal.organisation_id,
            )
        )
        ci = connector_result.scalar_one_or_none()
        if ci is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
        raw = ci.config_json.get(field, "") if field else json.dumps(ci.config_json)
        return raw if isinstance(raw, str) else json.dumps(raw)

    if payload.resource_type == "sso_provider":
        provider_result = await session.execute(
            select(SsoProvider).where(
                SsoProvider.id == resource_uuid,
                SsoProvider.organisation_id == principal.organisation_id,
            )
        )
        provider = provider_result.scalar_one_or_none()
        if provider is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO provider not found")
        if provider.client_secret is None:
            return ""
        try:
            return decode_stored_secret(provider.client_secret, settings.fernet_key)
        except SecretStorageError:
            _log.exception("middleware.sensitive_mask.invalid_sso_secret")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored SSO provider secret is invalid",
            ) from None

    if payload.resource_type == "observability":
        from modulo.db.models.organisation import Organisation

        config_result = await session.execute(
            select(Organisation.otel_config_json).where(Organisation.id == principal.organisation_id)
        )
        row = config_result.scalar_one_or_none()
        config: dict[str, Any] = row or {}
        if field:
            value = config.get(field, "")
            return value if isinstance(value, str) else json.dumps(value)
        return json.dumps(config)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown resource_type: {payload.resource_type}",
    )


@router.post("/reveal")
async def reveal_sensitive_value(
    payload: RevealRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_system_or_org_admin("admin.sensitive.manage"),
) -> RevealResponse:

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            actual_value = await _fetch_value(payload, session, principal, settings)

    except ProgrammingError:
        _log.exception("middleware.sensitive_mask")

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None

    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        return RevealResponse(token="", value=actual_value, expires_in_seconds=30)  # nosec B106 -- empty-string token on Redis failure is a degrade-to-no-token sentinel, NOT a hardcoded password

    reveal_token = str(uuid.uuid4())
    try:
        await redis.setex(f"sensitive_reveal:{reveal_token}", 30, actual_value)
    finally:
        await redis.aclose()

    return RevealResponse(token=reveal_token, value=actual_value, expires_in_seconds=30)
