"""Sensitive data masking utilities and reveal endpoint.

Provides DOM-safe masking for credentials, API keys, and secrets returned in
API responses. A server-authenticated reveal endpoint allows temporary
30-second unmasking via Redis-backed tokens.
"""

import json
import logging
import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, PlainSerializer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_system_or_org_admin
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import SecretStorageError, decode_stored_secret
from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK, mask_secret_values_in_text
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

# Re-exported so API-layer callers can import from the documented location.
# Required because mypy runs under `strict` (no_implicit_reexport = True).
__all__ = ["SENSITIVE_VALUE_MASK", "merge_masked_config_json"]

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


def _mask_config_value(value: Any, key: str | None = None) -> Any:
    """Recursively mask a config_json value.

    A string is masked when its key-path is sensitive (:func:`is_sensitive_key`)
    OR its value matches a secret-VALUE pattern (:func:`mask_secret_values_in_text`).
    Recursing through nested dicts and lists means a secret buried under a
    ``headers`` / ``params`` / ``operations`` / ``<resource>`` / ``body`` /
    ``base_url`` path (an embedded token) is masked too — previously only
    top-level string values under a sensitive key were masked, so a nested
    ``headers.Authorization`` or a token inside a ``base_url`` / ``path`` /
    ``body_template`` string leaked unmasked on the low-privilege
    ``connector.list`` surface.
    """
    if isinstance(value, dict):
        return {k: _mask_config_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_config_value(v, key) for v in value]
    if isinstance(value, str):
        if key is not None and is_sensitive_key(key):
            return mask_sensitive_value(value)
        return mask_secret_values_in_text(value)
    return value


def mask_config_json(config: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _mask_config_value(config))


def _is_masked_echo(value: Any) -> bool:
    return isinstance(value, str) and SENSITIVE_VALUE_MASK in value


def _contains_masked_echo(value: Any) -> bool:
    """Recursively detect any masked-echo string anywhere in *value*.

    Unlike :func:`_is_masked_echo` (top-level string only), this walks dict
    and list containers so a list-of-dicts whose elements carry a masked secret
    (e.g. a round-tripped ``operations`` entry) is correctly recognised as a
    partial GET->PATCH payload rather than a fully-specified value.
    """
    if isinstance(value, str):
        return _is_masked_echo(value)
    if isinstance(value, dict):
        return any(_contains_masked_echo(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_masked_echo(v) for v in value)
    return False


def merge_masked_config_json(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *incoming* into *current*, refusing to persist the DOM mask.

    A PATCH read-modify-write round-trip sends back the masked value it was
    handed by a prior GET. Persisting that mask literal would clobber the real
    stored secret, so any incoming string containing
    :data:`SENSITIVE_VALUE_MASK` is skipped at every nesting depth (the existing
    value is preserved). ``None`` values delete the key; nested dicts are merged
    recursively rather than replaced wholesale. A list value that contains NO
    masked echo is treated as the caller's complete intended value and replaces
    the stored list wholesale (so non-secret scalar lists such as the
    ``allowed_hosts`` SSRF egress allowlist can be shrunk or cleared); a list
    that DOES contain a masked echo is merged positionally so stored secrets
    are never clobbered.
    """
    return cast(dict[str, Any], _deep_merge(current, incoming))


def _deep_merge(current: Any, incoming: Any) -> Any:
    if isinstance(current, dict) and isinstance(incoming, dict):
        merged: dict[str, Any] = dict(current)
        for k, v in incoming.items():
            if _is_masked_echo(v):
                continue
            if isinstance(v, dict):
                merged[k] = _deep_merge(merged.get(k, {}), v)
            elif isinstance(v, list):
                merged[k] = _merge_list(merged.get(k), v)
            elif v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        return merged
    if isinstance(current, list) and isinstance(incoming, list):
        return _merge_list(current, incoming)
    return incoming


def _merge_list(current: Any, incoming: list[Any]) -> list[Any]:
    """Merge an incoming list into a stored list, skipping masked echoes.

    A PATCH read-modify-write round-trip sends back the masked list it was
    handed by a prior GET. Persisting those mask literals would clobber the
    real stored secrets, so every element that is a masked echo is skipped at
    its index (the stored value is preserved). Elements carrying a real change
    replace the stored element; new elements are appended; nested dicts / lists
    are merged recursively rather than replaced wholesale. An incoming list
    that is entirely masked echoes therefore leaves the stored list intact.
    """
    if not isinstance(incoming, list):
        return incoming
    # A fully-specified (non-secret) list is a WHOLE-LIST REPLACEMENT, not a
    # positional merge. The GET->PATCH round-trip only re-emits masked echoes
    # for list elements that actually contain secrets; any list that carries NO
    # masked echo is the caller's complete intended value, so honour shrink and
    # removal (e.g. narrowing the ``allowed_hosts`` SSRF/egress allowlist) rather
    # than silently preserving stale tail elements. Only a list that DOES
    # contain a masked echo falls through to the position-preserving merge, so a
    # stored secret can never be clobbered by a round-tripped mask literal.
    if not _contains_masked_echo(incoming):
        return list(incoming)
    merged_list: list[Any] = list(current) if isinstance(current, list) else []
    for idx, item in enumerate(incoming):
        if isinstance(item, dict):
            if idx < len(merged_list) and isinstance(merged_list[idx], dict):
                merged_list[idx] = _deep_merge(merged_list[idx], item)
            else:
                merged_list.append(item)
        elif isinstance(item, list):
            if idx < len(merged_list) and isinstance(merged_list[idx], list):
                merged_list[idx] = _merge_list(merged_list[idx], item)
            else:
                merged_list.append(item)
        elif _is_masked_echo(item):
            continue
        elif idx < len(merged_list):
            merged_list[idx] = item
        else:
            merged_list.append(item)
    return merged_list


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
