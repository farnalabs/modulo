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
from modulo.auth.secret_storage import SecretStorageError, decode_stored_secret_scoped
from modulo.core.secret_patterns import (
    SENSITIVE_VALUE_MASK,
    is_sensitive_env_key,
    is_sensitive_key,
    mask_secret_values_in_text,
)
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

# Re-exported so API-layer callers can import from the documented location.
# Required because mypy runs under `strict` (no_implicit_reexport = True).
__all__ = [
    "SENSITIVE_VALUE_MASK",
    "is_sensitive_env_key",
    "is_sensitive_key",
    "mask_pipeline_graph_node",
    "merge_masked_config",
    "merge_masked_config_json",
    "merge_masked_graph_nodes",
]

_log = logging.getLogger(__name__)

# The DOM-side mask constant, the canonical secret-format redaction patterns,
# the value-pattern list, the sensitive-KEY classifier
# (``_SENSITIVE_KEY_PATTERNS`` / ``is_sensitive_key`` / ``is_sensitive_env_key``)
# and the shared raw patterns (``SECRET_VALUE_PATTERNS``,
# ``mask_secret_values_in_text``, ``GITHUB_PAT_PATTERN``, ``AWS_ACCESS_KEY_PATTERN``)
# are all defined ONCE in :mod:`modulo.core.secret_patterns` (the single source of
# truth) so the two modules can never drift. The API layer (runs.py) imports the
# value patterns from there directly. They live in ``core`` so the core redaction
# and export sites (error_codes.py, node_runner.py, soc2.py,
# workflow_import_export) can use the same definitions without violating the
# ``core-does-not-import-api`` contract; this module re-exports the names its
# callers import from here.


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


def merge_masked_config(current: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    """MERGE *update* into *current* without clobbering masked secrets.

    A masked placeholder (``SENSITIVE_VALUE_MASK``) never overwrites the stored
    secret (read-modify-write round-trip guard); an explicit ``None`` clears the
    key; a missing key is left intact. The merge is shallow. This is the single
    shared implementation previously duplicated in ``triggers.py``,
    ``connectors.py``, ``error_forwarder_config.py`` and ``mcp_server.py``.
    """
    merged = dict(current or {})
    for k, v in update.items():
        if isinstance(v, str) and v == SENSITIVE_VALUE_MASK:
            continue
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


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
            return await decode_stored_secret_scoped(
                session, provider.client_secret, settings.fernet_key, org_id=principal.organisation_id
            )
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
        _log.warning(
            "middleware.sensitive_mask.reveal_no_redis: Redis unavailable; "
            "reveal token cannot be persisted. Returning value without "
            "server-side expiry — caller must not cache beyond the displayed "
            "value.",
        )
        # Empty-string token signals "no server-side expiry exists" to the
        # client.  expires_in_seconds=0 makes the absence explicit so callers
        # do not rely on a token that was never stored.
        return RevealResponse(token="", value=actual_value, expires_in_seconds=0)  # nosec B106 -- empty-string token on Redis failure is a degrade-to-no-token sentinel, NOT a hardcoded password

    reveal_token = str(uuid.uuid4())
    try:
        await redis.setex(f"sensitive_reveal:{reveal_token}", 30, actual_value)
    finally:
        await redis.aclose()

    return RevealResponse(token=reveal_token, value=actual_value, expires_in_seconds=30)


# ---------------------------------------------------------------------------
# Pipeline graph node masking (FAR-1181)
# ---------------------------------------------------------------------------

_GRAPH_NODE_SECRET_FIELDS: tuple[str, ...] = (
    "env_vars",
    "context_files",
    "composite_parameter_values",
    "parameter_overrides",
)


def mask_pipeline_graph_node(node: dict[str, Any]) -> dict[str, Any]:
    """Mask credential-bearing fields on a pipeline graph node dict (FAR-1181).

    Returned dict is a NEW object — the caller's ``node`` is never mutated.

    Masking tiers (both reusing the shipped maskers — no new detection logic):
    - Key tier: env var keys matched by :func:`is_sensitive_env_key` get the
      whole value masked; remaining env keys are still scanned by the canonical
      value patterns (an opaque token under a non-sensitive key is masked).
    - Value tier: context file contents and remaining env values pass through
      :func:`mask_secret_values_in_text` (embedded-secret redaction).
    - Deep dicts (``composite_parameter_values``, ``parameter_overrides``) use
      the shipped :func:`mask_config_json` (key tier at every nesting depth,
      plus value tier).

    Fail-closed: if any masker raises, the node's secret-bearing fields are
    scrubbed wholesale (every value replaced with ``SENSITIVE_VALUE_MASK``)
    rather than returned raw.
    """
    try:
        masked = dict(node)
        env = node.get("env_vars")
        if isinstance(env, dict):
            masked["env_vars"] = {
                key: (mask_sensitive_value(value) if is_sensitive_env_key(key) else mask_secret_values_in_text(value))
                if isinstance(value, str)
                else value
                for key, value in env.items()
            }
        contexts = node.get("context_files")
        if isinstance(contexts, dict):
            masked["context_files"] = {
                key: mask_secret_values_in_text(value) if isinstance(value, str) else value
                for key, value in contexts.items()
            }
        for field in ("composite_parameter_values", "parameter_overrides"):
            values = node.get(field)
            if isinstance(values, dict):
                masked[field] = mask_config_json(values)
        return masked
    except Exception:
        _log.exception("pipeline_graph_node_masking_failed: fail-closed scrub")
        scrubbed = dict(node)
        for field in _GRAPH_NODE_SECRET_FIELDS:
            values = node.get(field)
            if isinstance(values, dict):
                scrubbed[field] = {key: SENSITIVE_VALUE_MASK for key, value in values.items() if isinstance(key, str)}
        return scrubbed


def merge_masked_graph_nodes(
    incoming_nodes: list[dict[str, Any]],
    stored_nodes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Resolve mask echoes in a full-replace graph write against the stored graph (FAR-1181).

    The graph read path masks ``env_vars`` / ``context_files`` /
    ``composite_parameter_values`` / ``parameter_overrides``
    (:func:`mask_pipeline_graph_node`). A full-replace write round-tripping
    that masked read would otherwise persist the mask literals over the stored
    secrets. Per node (matched by ``id``):

    - ``env_vars`` / ``context_files`` (flat ``str`` dicts): an incoming value
      containing ``SENSITIVE_VALUE_MASK`` is replaced with the stored value;
      a masked echo with NO stored counterpart is dropped (fail closed).
      Keys absent from the incoming field remain removed — full-replace
      semantics are preserved for non-echo keys.
    - ``composite_parameter_values`` / ``parameter_overrides``: a dict
      containing a masked echo anywhere is deep-merged against the stored dict
      via the shipped :func:`merge_masked_config_json`; an echo-free dict is
      taken wholesale (full-replace semantics — no silent resurrection of
      removed keys). A dict with no stored counterpart resolves echoes by
      dropping them.
    """
    stored_by_id: dict[str, dict[str, Any]] = {}
    for stored_node in stored_nodes or []:
        if isinstance(stored_node, dict) and stored_node.get("id") is not None:
            stored_by_id[str(stored_node["id"])] = stored_node

    resolved: list[dict[str, Any]] = []
    for node in incoming_nodes:
        if not isinstance(node, dict):
            resolved.append(node)
            continue
        stored = stored_by_id.get(str(node["id"])) if node.get("id") is not None else None
        updated = dict(node)
        for field in ("env_vars", "context_files"):
            incoming = updated.get(field)
            if not isinstance(incoming, dict):
                continue
            stored_field = stored.get(field) if stored is not None else None
            stored_field = stored_field if isinstance(stored_field, dict) else {}
            merged: dict[str, Any] = {}
            for key, value in incoming.items():
                if isinstance(value, str) and SENSITIVE_VALUE_MASK in value:
                    restored = stored_field.get(key)
                    if restored is not None:
                        merged[key] = restored
                    # else: mask echo with no stored counterpart — dropped.
                else:
                    merged[key] = value
            updated[field] = merged
        for field in ("composite_parameter_values", "parameter_overrides"):
            incoming = updated.get(field)
            if not isinstance(incoming, dict):
                continue
            stored_field = stored.get(field) if stored is not None else None
            if _contains_masked_echo(incoming):
                base = stored_field if isinstance(stored_field, dict) else {}
                updated[field] = merge_masked_config_json(base, incoming)
            # else: echo-free — keep the caller's value wholesale.
        resolved.append(updated)
    return resolved
