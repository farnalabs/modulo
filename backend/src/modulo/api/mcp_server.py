"""Remote MCP server — thin adapter over the ViewModel API.

Mounted at `/mcp` as a Starlette sub-application inside FastAPI.

Auth: API key bearer token (`Authorization: Bearer mk_<key>`).
      Validated by McpAuthMiddleware before the request reaches FastMCP.
      org_id and role are stored in a ContextVar for tool handlers.

Dual-layer enforcement:
  1. Middleware: validates key, rejects unauthenticated requests.
  2. Tool layer: checks role (operator vs runner) before sensitive ops.

Org context validated per-event for streaming (SSE) connections.
"""

import asyncio
import contextvars
import json
import logging
import re
import threading
import time
import traceback as _traceback
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any, cast
from urllib.parse import quote, urlencode

from jwt import InvalidTokenError as JWTError
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from modulo.api.dependencies import (
    get_or_create_engine,
    get_or_create_session_factory,
)
from modulo.api.middleware.rate_limiter import RateLimitMiddleware as RateLimiterMiddleware
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.api.routes.evals import _EVAL_TYPE_PATTERN
from modulo.api.routes.triggers import _streak_status_for
from modulo.auth.api_key import (
    ApiKeyInvalidError,
    validate_api_key,
)
from modulo.auth.api_key import (
    create_api_key as auth_create_api_key,
)
from modulo.auth.api_key import (
    list_api_keys as auth_list_api_keys,
)
from modulo.auth.api_key import (
    revoke_api_key as auth_revoke_api_key,
)
from modulo.auth.dependencies import resolve_role_from_membership
from modulo.auth.oauth import (
    check_oauth_token_family_valid,
    clamp_oauth_role,
    decode_oauth_access_token,
    scopes_required_role,
)
from modulo.auth.permissions import _clamp_role, set_authz_enforce
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level
from modulo.core.analytics.builder import (
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsStatus,
    AnalyticsTriggerType,
)
from modulo.core.analytics.service import (
    AnalyticsDatabaseError,
    AnalyticsMigrationRequiredError,
    AnalyticsParams,
    AnalyticsQueryTimeoutError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
    run_analytics_query,
    run_concurrency_query,
)
from modulo.core.cost_controller.breakdown.constants import RAW_REPORTED_DISPLAY_CLAMP
from modulo.core.cost_controller.finalize import finalize_cancelled_run
from modulo.core.cron_helpers import (
    compute_next_fire,
    validate_cron_expression,
)

# ContextVars populated by McpAuthMiddleware before each request.
# Propagation: this server runs FastMCP in stateless HTTP mode, where each request
# spawns a fresh per-request server task *from the already-authenticated request
# coroutine* (StreamableHTTPSessionManager._handle_stateless_request calls
# task_group.start(...) at request time). asyncio/anyio copy the caller's context
# at task-creation time, so values set here in the middleware propagate to tool
# handlers. If a handler ever runs without this context, tenant resolution FAILS
# CLOSED (auth error) — there must never be a process-global fallback, because
# under concurrent multi-tenant load a global would resolve to whichever org
# authenticated last, leaking cross-tenant data.
from modulo.core.dispatch import dispatch_run
from modulo.core.documentation_indexer import DocumentationIndex
from modulo.core.exceptions import OrgDeletedError, SnapshotLockNotAvailableError
from modulo.core.feature_flags import resolve_plan_context
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
    NotTeamMemberError,
)
from modulo.core.library_service import (
    copy_to_adapt as library_copy_to_adapt,
)
from modulo.core.library_service import (
    get_primitive_by_slug,
    list_primitives,
)
from modulo.core.mcp.scope_validator import (
    MCPAuthorizationError,
    check_tool_scope,
    set_request_allowed_tools,
)
from modulo.core.pipeline_engine.error_codes import map_legacy_code, present_error
from modulo.core.rate_limiter import TokenBucketRegistry
from modulo.core.trigger_streak import (
    anchor_trigger_streak_epoch,
    clear_trigger_streak_after_reenable,
)
from modulo.db.capacity import StorageExhaustedError
from modulo.db.crud.hitl_gate_guard import GuardrailBindingStripDenied, HitlGateWeakeningDenied
from modulo.db.crud.model_backend import create_model_backend as db_create_model_backend
from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.run import get_run
from modulo.db.crud.schema import create_schema as db_create_schema
from modulo.db.crud.schema import get_schema
from modulo.db.crud.schema import list_schemas as db_list_schemas
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.settings_resolver import resolve_authz_enforce
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_CT_APPLICATION_JSON = "application/json"
_MSG_TOKEN_REVOKED = "Token revoked or expired - re-authenticate"  # nosec B105 -- user-facing error message string, NOT a secret credential
_MSG_ERROR_TOKEN_REVOKED = "error: Token revoked or expired - re-authenticate"  # nosec B105 -- user-facing error message string, NOT a secret credential
_MSG_DB_MIGRATION_REQUIRED = "Database migration required. Run `alembic upgrade head`."
_MSG_DB_MIGRATION_REQUIRED_HEADS = "Database migration required. Run alembic upgrade heads."
_MSG_TRIGGER_NOT_FOUND = "Trigger not found"
_MSG_UUID_PARSE_FAILED = "UUID parse failed"
_MSG_CREATE_API_KEY_FAILED = "create_api_key failed"
_MSG_LIST_API_KEYS_FAILED = "list_api_keys failed"
_MSG_REVOKE_API_KEY_FAILED = "revoke_api_key failed"
_MSG_CREATE_SCHEMA_FAILED = "create_schema failed"
_MSG_DB_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable"
_MSG_FEATURE_NOT_AVAILABLE_MIGRATE = "Feature is not available. Run database migrations to enable it."
_MSG_DB_ERROR_TRY_AGAIN = "Database error occurred. Please try again."
_MSG_UNEXPECTED_ERROR = "An unexpected error occurred"
_MSG_MCP_AUTH_DB_UNAVAILABLE = "mcp.auth.db_unavailable"
_CODE_PERMISSION_API_KEY_ROLE_CAP = "permission.api_key_role_cap"
_JSON_AUTH_DB_UNAVAILABLE = '{"error":"temporarily_unavailable","detail":"Auth backend temporarily unavailable"}'
_JSON_FORBIDDEN_ORG_MEMBERSHIP = '{"error":"forbidden","detail":"Organisation membership required"}'
_BASIC_PREFIX = "Basic "

_MCP_SANITIZE_STRING_MAX = 256
_MCP_BREAKDOWN_KEYS = frozenset(
    {
        "component",
        "display_name",
        "source",
        "formula_applied",
        "rate_usd",
        "amount_usd",
        "basis",
        "missing_self_report",
        "error",
        "total_clamped",
    }
)


def _sanitize_mcp_string(value: str) -> str:
    """Truncate an agent-controlled string to 256 chars + strip control chars."""
    cleaned = "".join(ch for ch in value if ch == "\t" or ord(ch) >= 32)
    return cleaned[:_MCP_SANITIZE_STRING_MAX]


def _clamp_mcp_number(value: float) -> float:
    """Magnitude-clamp any numeric field that can carry a hostile raw value
    (e.g. ``basis.raw_reported`` of 1e300) at 1e6 for display — the MCP surface
    cannot render 1e300. Mirrors the breakdown serializer's display clamp.
    """
    try:
        d = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return 0.0
    if not d.is_finite() or abs(d) > RAW_REPORTED_DISPLAY_CLAMP:
        return float(RAW_REPORTED_DISPLAY_CLAMP)
    return float(d)


def _sanitize_mcp_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {k: _sanitize_mcp_basis_value(v) for k, v in value.items()}


def _sanitize_mcp_sequence(value: list[Any]) -> list[Any]:
    return [_sanitize_mcp_basis_value(v) for v in value]


def _sanitize_mcp_basis_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_mcp_string(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _clamp_mcp_number(value)
    if isinstance(value, dict):
        return _sanitize_mcp_mapping(value)
    if isinstance(value, list):
        return _sanitize_mcp_sequence(value)
    if value is None:
        return None
    return _sanitize_mcp_string(str(value))


def _sanitize_cost_breakdown(breakdown: Any) -> list[dict[str, Any]]:
    """MCP whole-resource sanitize of a run's ``cost_breakdown``.

    Every agent-controlled string — ``component``, ``display_name``,
    ``formula_applied``, and recursively every string in ``basis`` — is
    truncated to 256 chars + stripped of control chars; numeric/boolean fields
    are type-validated; out-of-shape keys are stripped. Numeric fields that can
    carry a hostile raw magnitude (``basis.raw_reported`` /
    ``basis.per_node_raw``) are magnitude-clamped at 1e6 for display.
    """
    if not isinstance(breakdown, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        out = _sanitize_cost_breakdown_entry(entry)
        sanitized.append(out)
    return sanitized


def _sanitize_cost_breakdown_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a single cost-breakdown entry (key-filtering + type-dispatch)."""
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key not in _MCP_BREAKDOWN_KEYS:
            continue
        if key == "basis":
            out[key] = _sanitize_mcp_basis_value(value)
        elif isinstance(value, str):
            out[key] = _sanitize_mcp_string(value)
        elif isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = _clamp_mcp_number(value)
        else:
            out[key] = _sanitize_mcp_string(str(value))
    return out


_MCP_COST_ROLLUP_ZERO = Decimal("0.000000")
_MCP_COST_ROLLUP_QUANTUM = Decimal("0.000001")


def _quantize_mcp_cost_rollup(value: Decimal) -> Decimal:
    """Normalise a cost rollup value to 6 decimal places (Numeric(14, 6) scale).

    Mirrors the REST runs API's ``_quantize_cost_rollup`` so the MCP run
    resources render the same ``child_runs_cost_usd`` / ``aggregate_cost_usd``
    values as the REST surface.
    """
    return value.quantize(_MCP_COST_ROLLUP_QUANTUM)


def _format_breakdown_line(entry: dict[str, Any]) -> str:
    """Compact single-line rendering of a sanitized breakdown entry."""
    name = entry.get("display_name") or entry.get("component") or "component"
    amount = entry.get("amount_usd")
    if amount is None:
        amount = entry.get("rate_usd")
    source = entry.get("source", "")
    parts = [f"- {name} ({entry.get('component', '')}): ${amount or '0.000000'}"]
    if source:
        parts.append(source)
    if entry.get("missing_self_report") is True:
        parts.append("(not reported)")
    if entry.get("error"):
        parts.append(f"({entry['error']})")
    return " ".join(parts)


_RETRY_DB = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(OperationalError),
    reraise=True,
    before_sleep=before_sleep_log(_log, logging.WARNING),
)

_ctx_org_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_org_id")
_ctx_role: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_role")
_ctx_key_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_key_id")
_ctx_auth_token: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_token")
_ctx_user_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_user_id")
_ctx_auth_type: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_type")
_ctx_team_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar("mcp_team_id")
# FAR-436: node-level allowed_tools for a run-scoped sandbox key. Set by
# ``_authenticate_api_key`` when the caller is a run-scoped key; None (default)
# means no node-level narrowing (legacy behaviour). Empty list = deny-all.
_ctx_node_allowed_tools: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "mcp_node_allowed_tools", default=None
)


class McpAuthContextError(LookupError):
    """Raised when a handler runs without an authenticated tenant context.

    Fail-closed guard: tenant scope must come from the request-scoped
    ContextVars set by McpAuthMiddleware. There is deliberately no
    process-global fallback and no placeholder org.
    """


def _ctx_org_id_val() -> uuid.UUID:
    """Get org_id from the request context. Fails closed if unset."""
    v = _ctx_org_id.get(None)
    if v is None:
        raise McpAuthContextError("No authenticated organisation context for this MCP request")
    return v


def _ctx_user_id_val() -> uuid.UUID:
    """Get user/account_id from the request context. Fails closed if unset."""
    v = _ctx_user_id.get(None)
    if v is None:
        raise McpAuthContextError("No authenticated user context for this MCP request")
    return v


def _ctx_role_val() -> str | None:
    """Get role from the request context (None if unset — scope checks then fail closed)."""
    return _ctx_role.get(None)


def _ctx_team_id_val() -> uuid.UUID | None:
    """Get the team boundary of the current request (None when no team boundary).

    Set by ``McpAuthMiddleware`` only when the caller authenticated with a
    team-scoped API key (non-null ``OrgApiKey.team_id``). Org-wide API keys,
    OAuth access tokens and regular JWTs carry no team boundary and resolve
    to ``None`` — they are org-role-only, matching the REST layer.
    """
    return _ctx_team_id.get(None)


def _ctx_node_allowed_tools_val() -> list[str] | None:
    """Get the node-level allowed_tools for the current request (None = no narrowing).

    Set by ``_authenticate_api_key`` when the caller is a run-scoped sandbox
    key whose node declares ``capability_scope.allowed_tools``. ``None`` (the
    default) preserves pre-scope behaviour; an empty list is deny-by-default.
    """
    return _ctx_node_allowed_tools.get(None)


def _check_agent_tool_scope(tool_name: str, action: str | None = None) -> None:
    """Reuse the ``check_tool_scope`` chokepoint with node-level allowed_tools (FAR-436).

    Every live MCP agent tool-call is gated at this single chokepoint: the role
    must still permit the tool, AND (when the node declares
    ``capability_scope.allowed_tools``) the tool must be on the node's
    allow-list. Absent scope = legacy behaviour (role check only).
    """
    check_tool_scope(
        _ctx_role_val(),
        tool_name,
        action=action,
        allowed_tools=_ctx_node_allowed_tools_val(),
    )


def _team_scoped_key_mismatch(owner_team_id: uuid.UUID | None) -> bool:
    """True when a team-scoped API key must not access a resource owned by *owner_team_id*.

    The boundary only applies to team-scoped API keys (non-null
    ``_ctx_team_id``): org-wide keys and user/OAuth tokens have no team
    boundary. A resource with no owning team (org-level pipeline) is
    accessible to any team-scoped key; a resource owned by a different team
    is blocked.
    """
    key_team_id = _ctx_team_id.get(None)
    if key_team_id is None:
        return False
    return owner_team_id is not None and owner_team_id != key_team_id


def _team_scope_error(resource_kind: str, resource_id: str) -> dict[str, Any]:
    """Error dict for a team-boundary violation by a team-scoped API key."""
    key_team_id = _ctx_team_id.get(None)
    return {
        "error": "team_boundary_violation",
        "detail": (
            f"This API key is scoped to team {key_team_id} and cannot access "
            f"{resource_kind} {resource_id} owned by another team"
        ),
    }


def _team_scope_error_str(resource_kind: str, resource_id: str) -> str:
    """String error (resource surface) for a team-boundary violation."""
    err = _team_scope_error(resource_kind, resource_id)
    return f"error: {err['error']} — {err['detail']}"


async def _pipeline_owner_team_id(session: AsyncSession, pipeline_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve the owning team of a pipeline (None for org-level pipelines).

    Thin wrapper over the shared ``team_scope`` resolver so MCP guards and the
    DB list filters share one effective-owner source.
    """
    from modulo.db.crud.team_scope import pipeline_owner_team_id

    return await pipeline_owner_team_id(session, pipeline_id)


async def _run_owner_team_id(session: AsyncSession, run: Run) -> uuid.UUID | None:
    """Resolve the effective owning team of a run (None for org-level runs).

    ``Run.owner_team_id`` is the source of truth (snapshot at creation, see
    ``create_run``), but pre-existing runs predate the stamp and carry NULL.
    Falling back to the pipeline's current ``owner_team_id`` keeps the
    boundary enforced for those rows too — a NULL run owner must never mean
    "visible to every team-scoped key".
    """
    if run.owner_team_id is not None:
        return run.owner_team_id
    return await _pipeline_owner_team_id(session, run.pipeline_id)


# PRD §7.18: MCP trigger_pipeline is limited to 60 calls/min per client. All
# MCP tools share the /mcp HTTP path, so the middleware can't differentiate
# this tool (it is capped by the general 200/min rule); the 60/min limit is
# enforced here at the application level with a per-client in-memory bucket.
_TRIGGER_PIPELINE_RATE = 60 / 60.0  # 60 tokens per 60s window
_TRIGGER_PIPELINE_BURST = 60

_trigger_pipeline_limiter = TokenBucketRegistry(
    rate=_TRIGGER_PIPELINE_RATE,
    burst=_TRIGGER_PIPELINE_BURST,
)


def _trigger_pipeline_client_key() -> str:
    """Derive the per-client key for the trigger_pipeline rate limit.

    Mirrors the middleware ``_client_key`` identity: API-key calls are keyed
    by org + key id, OAuth/JWT calls by org + user id. Distinct clients never
    share a bucket.
    """
    org = _ctx_org_id.get(None)
    org_s = str(org) if org is not None else "unknown"
    auth_type = _ctx_auth_type.get(None) or "unknown"
    if auth_type == "api_key":
        key_id = _ctx_key_id.get(None)
        client = f"ak:{key_id}" if key_id is not None else "ak:unknown"
    else:
        uid = _ctx_user_id.get(None)
        client = f"user:{uid}" if uid is not None else "user:unknown"
    return f"trigger_pipeline:{org_s}:{auth_type}:{client}"


async def _trigger_pipeline_rate_allowed() -> bool:
    """Consume one token from the caller's trigger_pipeline bucket.

    Returns False once the caller exceeds 60 calls/min (rate=1.0, burst=60).
    """
    return await _trigger_pipeline_limiter.consume(_trigger_pipeline_client_key())


# API-key role-cap degradation counter (ADR 017 DECISION 4): increments every
# time a live-role clamp demotes a key (or an owner removal kills one), so mass
# key-degradation is visible in logs and metrics. Module-level + lock, mirroring
# the CatchAllMiddleware counter pattern.
_api_key_role_cap_count: int = 0
_api_key_role_cap_lock = threading.Lock()


def _record_api_key_role_cap(
    *,
    minted_role: str,
    effective_role: str,
    org_id: uuid.UUID,
    degraded: bool,
    key_id: uuid.UUID | None = None,
) -> None:
    """Log + count an API-key role-cap clamp (degrade or deny-on-removal).

    ``degraded=True`` when the effective role is lower than the minted role
    (demoted operator); ``degraded=False`` with ``effective_role=""`` when the
    owner was removed/deactivated (key dies).
    """
    global _api_key_role_cap_count
    with _api_key_role_cap_lock:
        _api_key_role_cap_count += 1
    _log.warning(
        _CODE_PERMISSION_API_KEY_ROLE_CAP,
        extra={
            "minted_role": minted_role,
            "effective_role": effective_role,
            "org_id": str(org_id),
            "key_id": str(key_id) if key_id else None,
            "degraded": degraded,
            "total_caps": _api_key_role_cap_count,
        },
    )


def get_api_key_role_cap_count() -> int:
    """Return the total number of API-key role-cap clamps recorded."""
    with _api_key_role_cap_lock:
        return _api_key_role_cap_count


async def _set_authz_enforce(org_id: uuid.UUID) -> None:
    """Resolve the per-org authz-enforce kill-switch flag into the ContextVar.

    ``check_tool_scope`` reads it via ``assert_org_role``. Fail-closed: on any
    read error the flag defaults to enforcement ON (True) and the failure is
    logged under the structured kill-switch key. ADR 017 DECISION 3.
    """
    try:
        async with _session(org_id) as s:
            enforce = await resolve_authz_enforce(s, org_id)
    except Exception:
        _log.warning("permission.kill_switch_read_failed", exc_info=True)
        enforce = True
    set_authz_enforce(enforce)


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-global session factory, sharing the engine from dependencies.py."""
    settings = get_settings()
    return get_or_create_session_factory(get_or_create_engine(settings))


@asynccontextmanager
async def _session(org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    factory = _get_session_factory()
    async with factory() as s, s.begin():
        await set_rls_org(s, org_id)
        try:
            uid = _ctx_user_id_val()
            role = _ctx_role_val() or ""
            await set_rls_user_context(s, uid, role)
        except (LookupError, ValueError):
            _log.warning("mcp.session_user_context_failed", exc_info=True)
        yield s


# ---------------------------------------------------------------------------
# Per-event auth validation
# ---------------------------------------------------------------------------

# TTL-bounded live-role cache for SSE per-event revalidation (ADR 017): at most
# one org_memberships read per connection per window, so a demoted admin loses
# scope mid-stream without a DB round-trip on every event.
_LIVE_ROLE_TTL_SECONDS = 15.0
_MAX_LIVE_ROLE_CACHE = 1024
_live_role_cache: dict[str, tuple[float, str | None]] = {}


def _evict_stale_live_role_cache(now: float) -> None:
    """Evict expired entries, then drop oldest few if still over capacity."""
    if len(_live_role_cache) < _MAX_LIVE_ROLE_CACHE:
        return
    for key in [k for k, v in _live_role_cache.items() if now - v[0] >= _LIVE_ROLE_TTL_SECONDS]:
        _live_role_cache.pop(key, None)
    overflow = len(_live_role_cache) - _MAX_LIVE_ROLE_CACHE + 1
    if overflow > 0:
        oldest = sorted(_live_role_cache.items(), key=lambda kv: kv[1][0])[:overflow]
        for key, _ in oldest:
            _live_role_cache.pop(key, None)


async def _revalidate_live_role(token: str, account_id: uuid.UUID, org_id: uuid.UUID) -> str | None:
    """TTL-bounded live-role re-read for a JWT principal (ADR 017).

    Returns the live org role, or None if the membership is missing/deactivated
    (removed user) or the read failed — the caller then denies (fail closed,
    matching the ``validate_current_auth`` posture). The cache is keyed by the
    connection's auth token, so it acts as a per-connection timestamp cache.
    """
    now = time.monotonic()
    cached = _live_role_cache.get(token)
    if cached is not None and now - cached[0] < _LIVE_ROLE_TTL_SECONDS:
        return cached[1]

    live_role: str | None = None
    try:
        async with _session(org_id) as s:
            live_role = await resolve_role_from_membership(
                s,
                str(account_id),
                str(org_id),
            )
    except SQLAlchemyError:
        _log.warning("permission.live_role_read_failed", exc_info=True)
        live_role = None

    _evict_stale_live_role_cache(now)
    _live_role_cache[token] = (now, live_role)
    return live_role


async def _validate_api_key_live(token: str, org_id: uuid.UUID) -> bool:
    """Re-validate an API-key credential and clamp its role against the live role."""
    async with _session(org_id) as s:
        key = await validate_api_key(s, token, org_id)
    # ADR 017 DECISION 4 — clamp on every per-event re-validation too.
    # The stored key.role is the minted role; the effective role is
    # min(minted, live), resolved TTL-bounded through the same cache
    # the JWT path uses (per-connection keyed by token).
    account_id = _ctx_user_id.get(None)
    if account_id is None:
        return False
    live_role = await _revalidate_live_role(token, account_id, org_id)
    if live_role is None:
        _record_api_key_role_cap(
            minted_role=key.role,
            effective_role="",
            org_id=org_id,
            degraded=False,
            key_id=key.id,
        )
        return False
    clamped = _clamp_role(key.role, live_role)
    if not clamped:
        _record_api_key_role_cap(
            minted_role=key.role,
            effective_role="",
            org_id=org_id,
            degraded=False,
            key_id=key.id,
        )
        return False
    if clamped != key.role:
        _record_api_key_role_cap(
            minted_role=key.role,
            effective_role=clamped,
            org_id=org_id,
            degraded=True,
            key_id=key.id,
        )
    _ctx_role.set(clamped)
    _ctx_team_id.set(key.team_id)
    return True


async def _validate_principal_live(token: str, principal: Any) -> bool:
    """Re-validate a regular JWT principal's live org role."""
    if principal.organisation_id is None:
        return False
    live_role = await _revalidate_live_role(
        token,
        principal.account_id,
        principal.organisation_id,
    )
    if live_role is None:
        return False
    _ctx_role.set(live_role)
    _ctx_team_id.set(None)  # user tokens carry no team boundary
    return True


async def _validate_oauth_live(token: str) -> bool:
    """Re-validate an OAuth access token credential against its token family."""
    settings = get_settings()
    try:
        claims = decode_oauth_access_token(token, settings.secret_key)
    except JWTError:
        # Regular JWT (used by Remy) — skip OAuth token family check
        try:
            from modulo.auth.jwt import decode_principal

            principal = decode_principal(token, settings.secret_key)
        except JWTError:
            return False
        return await _validate_principal_live(token, principal)
    async with _session(claims.organisation_id) as s:
        if not await check_oauth_token_family_valid(
            s,
            family_id=claims.token_family,
            client_id=claims.client_id,
            org_id=claims.organisation_id,
        ):
            return False
    # ADR 017: re-resolve the account's LIVE role (TTL-bounded per
    # connection) and re-apply the scope→live clamp so a demoted
    # operator loses scope mid-stream too.
    live_role = await _revalidate_live_role(
        token,
        claims.account_id,
        claims.organisation_id,
    )
    if live_role is None:
        return False
    _ctx_role.set(clamp_oauth_role(scopes_required_role(claims.scopes), live_role))
    _ctx_team_id.set(None)  # user tokens carry no team boundary
    return True


async def validate_current_auth() -> bool:
    """Re-validate the current auth credential for per-event SSE enforcement.

    Checks the stored credential against the DB/issuer to detect mid-session
    revocation, expiry, or OAuth token family blacklisting. For JWT principals
    the LIVE org role is re-resolved (TTL-bounded) and ``_ctx_role`` is re-set
    so a demoted admin loses scope mid-stream (ADR 017).
    Returns True if the credential is still valid, False otherwise.

    Fail closed: the credential and org come exclusively from the
    request-scoped ContextVars set by ``McpAuthMiddleware``. If any of them
    is missing, the request is treated as unauthenticated — there is no
    process-global fallback.
    """
    auth_type = _ctx_auth_type.get(None)
    token = _ctx_auth_token.get(None)
    org_id = _ctx_org_id.get(None)

    if auth_type is None:
        auth_type = "api_key" if token and token.startswith("mk_") else None

    if auth_type is None or token is None or org_id is None:
        return False

    try:
        if auth_type == "api_key":
            return await _validate_api_key_live(token, org_id)
        if auth_type == "oauth":
            return await _validate_oauth_live(token)
        return False
    except (ApiKeyInvalidError, JWTError):
        return False
    except Exception:
        _log.exception("validate_current_auth failed")
        return False


def _extract_bearer_token(request: Request) -> tuple[str | None, Response | None]:
    """Extract the Bearer token from the Authorization header.

    Returns ``(token, None)`` on success or ``(None, error_response)`` when the
    header is missing or not a Bearer token.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, Response(
            '{"error":"unauthorized","detail":"Bearer token required"}',
            status_code=401,
            media_type=_CT_APPLICATION_JSON,
        )
    token = auth_header[len("Bearer ") :].strip()
    return token, None


def _extract_node_id_from_key_name(key_name: str | None) -> str | None:
    """Parse a per-node sandbox key ``name`` for its ``node_id`` (FAR-436).

    ``mint_run_api_key`` names run-scoped keys ``run:<run_id>:node:<node_id>``.
    Returns ``None`` when the name is not a run-scoped sandbox key (a normal
    user/org API key), so those callers are never narrowed.
    """
    if not key_name:
        return None
    marker = ":node:"
    idx = key_name.rfind(marker)
    if idx < 0:
        return None
    node_id = key_name[idx + len(marker) :].strip()
    return node_id or None


async def _node_allowed_tools_for_key(
    *, org_id: uuid.UUID, run_id: uuid.UUID | None, key_name: str
) -> list[str] | None:
    """Resolve a run-scoped key's node-level ``capability_scope.allowed_tools`` (FAR-436).

    A run-scoped sandbox key (``run_id`` set) is minted per sandbox_agent NODE,
    so the node's snapshot ``capability_scope`` narrows the MCP tools that
    node's agent may call. Returns ``None`` for a non-run key or a node with no
    ``capability_scope`` (unrestricted — the pre-scope default); an explicit
    empty allow-list is returned untouched (deny-by-default). Any resolution
    failure returns ``None`` (unrestricted) with a log — a scope misread must
    never break the agent's MCP stream, and it cannot widen beyond the Agent's
    grants either (the role check still runs).
    """
    if run_id is None:
        return None
    node_id = _extract_node_id_from_key_name(key_name)
    if node_id is None:
        return None
    from sqlalchemy import select

    from modulo.db.models.pipeline_snapshot import PipelineSnapshot

    try:
        async with _session(org_id) as s:
            snapshot_id = (
                await s.execute(
                    select(Run.snapshot_id).where(
                        Run.id == run_id,
                        Run.organisation_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if snapshot_id is None:
                return None
            snapshot = await s.get(PipelineSnapshot, snapshot_id)
            if snapshot is None:
                return None
            for node in snapshot.graph_json.get("nodes", []):
                if str(node.get("id")) == node_id:
                    scope = node.get("capability_scope") or {}
                    allowed = scope.get("allowed_tools")
                    if isinstance(allowed, list):
                        return [str(t) for t in allowed]
                    return None
            return None
    except (SQLAlchemyError, TimeoutError):
        _log.exception(
            "mcp.scope.node_allowed_tools_resolve_failed",
            extra={"run_id": str(run_id), "node_id": node_id},
        )
        return None


async def _authenticate_api_key(
    request: Request,
    token: str,
) -> tuple[bool, Response | None]:
    """Authenticate an API-key bearer token (``mk_`` prefix).

    Sets the ``_ctx_*`` contextvars and ``request.scope["auth_principal"]`` on
    success. Returns ``(True, None)`` when the key authenticated (the caller
    should proceed to ``call_next``) or ``(False, error_response)`` on failure.
    """
    try:
        # org_api_keys has RLS enabled (migration 0005, _STRICT_RLS) and
        # the key's org is unknown until the record is read — a plain
        # lookup in an empty org context would be filtered out by RLS
        # and reject every valid key. On Postgres the runtime app role
        # is RLS-subject (a non-owner DML-granted role), so the org is
        # resolved through a SECURITY DEFINER function owned by the
        # migration role rather than SET row_security TO OFF (which
        # only bypasses RLS for owners and raises for a regular role).
        # On generic backends there is no RLS, so a plain prefix scan
        # works. Then re-validate inside the org context before
        # trusting the key.
        from sqlalchemy import select, text

        from modulo.auth.api_key import _MK_PREFIX, _PREFIX_LEN
        from modulo.db.models.api_key import OrgApiKey
        from modulo.db.rls import _ensure_active_transaction

        prefix = token[len(_MK_PREFIX) :][:_PREFIX_LEN]
        factory = _get_session_factory()
        async with factory() as s, s.begin():
            dialect = await _ensure_active_transaction(s)
            if dialect == "postgresql":
                org_id = (
                    await s.execute(
                        text("SELECT public.lookup_api_key_org(:prefix)"),
                        {"prefix": prefix},
                    )
                ).scalar_one_or_none()
            else:
                key_record = (
                    await s.execute(
                        select(OrgApiKey).where(
                            OrgApiKey.lookup_prefix == prefix,
                            OrgApiKey.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                org_id = key_record.organisation_id if key_record is not None else None
        if org_id is None:
            raise ApiKeyInvalidError

        # Now re-validate within the correct RLS context.
        async with _session(org_id) as s:
            key = await validate_api_key(s, token, org_id=org_id)
            # ADR 017 DECISION 4 — live role-cap on EVERY MCP call. The
            # stored key.role is the minted role; the effective role is
            # min(minted, live). A demoted operator's key degrades to
            # the live role on the next call (never persisted — the ORM
            # flushes last_used_at, so mutating key.role here would
            # permanently store the demotion). An owner removed from
            # the org (no live membership) makes the key die (401).
            live_role = await resolve_role_from_membership(
                s,
                str(key.account_id),
                str(key.organisation_id),
            )
            clamped = _clamp_role(key.role, live_role)
            if not clamped:
                _record_api_key_role_cap(
                    minted_role=key.role,
                    effective_role="",
                    org_id=key.organisation_id,
                    degraded=False,
                    key_id=key.id,
                )
                raise ApiKeyInvalidError
            if clamped != key.role:
                _record_api_key_role_cap(
                    minted_role=key.role,
                    effective_role=clamped,
                    org_id=key.organisation_id,
                    degraded=True,
                    key_id=key.id,
                )
        org_id = key.organisation_id
        _ctx_org_id.set(org_id)
        _ctx_role.set(clamped)
        _ctx_key_id.set(key.id)
        _ctx_team_id.set(key.team_id)
        _ctx_user_id.set(key.account_id)
        _ctx_auth_token.set(token)
        _ctx_auth_type.set("api_key")
        # FAR-436: a run-scoped sandbox key narrows the agent's MCP tool-call
        # loop to the node's capability_scope.allowed_tools (deny-by-default
        # within the scope). Non-run keys / scoped-less nodes resolve to None
        # (unrestricted — legacy behaviour). Empty allow-list = deny-all.
        _ctx_node_allowed_tools.set(
            await _node_allowed_tools_for_key(
                org_id=org_id,
                run_id=key.run_id,
                key_name=key.name,
            )
        )
        request.scope["auth_principal"] = {
            "type": "api_key",
            "org_id": str(org_id),
            "prefix": token[3:11],
        }
    except ApiKeyInvalidError:
        return False, Response(
            '{"error":"unauthorized","detail":"Invalid or revoked API key"}',
            status_code=401,
            media_type=_CT_APPLICATION_JSON,
        )
    except (SQLAlchemyError, TimeoutError):
        _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
        return False, Response(
            _JSON_AUTH_DB_UNAVAILABLE,
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    return True, None


async def _authenticate_oauth_jwt(
    request: Request,
    token: str,
    settings: Any,
) -> tuple[bool, Response | None, Any]:
    """Authenticate an OAuth access token (JWT), falling back to a regular JWT.

    Returns ``(handled, error_response, claims)``:

    * ``(True, None, None)`` — a regular JWT (Remy) fully authenticated; the
      caller should ``call_next`` and return.
    * ``(False, None, claims)`` — an OAuth access token decoded successfully;
      the caller continues to the token-family check with ``claims``.
    * ``(False, error_response, None)`` — authentication failed.

    Sets the ``_ctx_*`` contextvars and ``request.scope["auth_principal"]`` for
    the regular-JWT fallback path. The OAuth claims path defers context-setting
    until after the token-family and scope checks (in the caller).
    """
    try:
        claims = decode_oauth_access_token(token, settings.secret_key)
    except JWTError:
        # Fall back to regular JWT access token (used by Remy MCP tool calls).
        try:
            from modulo.auth.jwt import decode_principal

            principal = decode_principal(token, settings.secret_key)
        except JWTError:
            return (
                False,
                Response(
                    '{"error":"unauthorized","detail":"Invalid or expired access token"}',
                    status_code=401,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        if principal.organisation_id is None:
            return (
                False,
                Response(
                    _JSON_FORBIDDEN_ORG_MEMBERSHIP,
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        # ADR 017: no claim-less default-up. A None role claim fails closed,
        # and the LIVE role is re-read from org_memberships so a demoted or
        # removed member loses access on the very next request.
        if principal.org_role is None:
            return (
                False,
                Response(
                    '{"error":"forbidden","detail":"No org role claim on token"}',
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        try:
            async with _session(principal.organisation_id) as s:
                live_role = await resolve_role_from_membership(
                    s,
                    str(principal.account_id),
                    str(principal.organisation_id),
                )
        except (SQLAlchemyError, TimeoutError):
            _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
            return (
                False,
                Response(
                    _JSON_AUTH_DB_UNAVAILABLE,
                    status_code=503,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        if live_role is None:
            return (
                False,
                Response(
                    _JSON_FORBIDDEN_ORG_MEMBERSHIP,
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        _ctx_org_id.set(principal.organisation_id)
        _ctx_role.set(live_role)
        _ctx_key_id.set(uuid.UUID(int=0))
        _ctx_user_id.set(principal.account_id)
        _ctx_auth_token.set(token)
        _ctx_auth_type.set("oauth")
        _ctx_team_id.set(None)  # user tokens carry no team boundary
        request.scope["auth_principal"] = {
            "type": "user",
            "org_id": str(principal.organisation_id) if principal.organisation_id else "",
            "user_id": str(principal.account_id) if principal.account_id else "",
        }
        return True, None, None
    return False, None, claims


async def _verify_oauth_token_family(
    _token: str,
    claims: Any,
) -> Response | None:
    """Return an error response if the OAuth token family is blacklisted.

    Returns ``None`` when the family is valid (the caller should continue), or
    the appropriate error ``Response`` otherwise.
    """
    try:
        async with _session(claims.organisation_id) as s:
            valid = await check_oauth_token_family_valid(
                s,
                family_id=claims.token_family,
                client_id=claims.client_id,
                org_id=claims.organisation_id,
            )
            if not valid:
                return Response(
                    '{"error":"unauthorized","detail":"Token family revoked"}',
                    status_code=401,
                    media_type=_CT_APPLICATION_JSON,
                )
    except (SQLAlchemyError, TimeoutError):
        _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
        return Response(
            _JSON_AUTH_DB_UNAVAILABLE,
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    except Exception:
        _log.exception("OAuth token family check failed")
        return Response(
            '{"error":"unauthorized","detail":"Token validation failed"}',
            status_code=401,
            media_type=_CT_APPLICATION_JSON,
        )
    return None


async def _dispatch_unauth_paths(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response | None:
    """Return the response when the path is served unauthenticated, else None.

    These endpoints manage their own auth (health checks, and the OAuth protocol
    endpoints which authenticate via client_id + client_secret).
    """
    clean = request.url.path.rstrip("/")
    if clean in ("/mcp/healthz", "/healthz"):
        resp: Response = await call_next(request)
        return resp
    if clean in ("/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/refresh"):
        resp2: Response = await call_next(request)
        return resp2
    return None


async def _finalize_oauth_principal(
    request: Request,
    token: str,
    claims: Any,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Resolve the OAuth principal role, set request context, and continue.

    Fail-closed: a DB read failure or missing/deactivated membership denies
    the request (ADR 017 — the scope grant is clamped to the account's live
    org role so a demoted operator loses scope on the next call).
    """
    # Resolve role from scopes (highest scope wins) — the scope grant is then
    # CLAMPED to the account's LIVE org role so a demoted operator loses scope
    # on the very next call. Fail-closed: a DB read failure or
    # missing/deactivated membership denies.
    scope_role = scopes_required_role(claims.scopes)
    try:
        async with _session(claims.organisation_id) as s:
            live_role = await resolve_role_from_membership(
                s,
                str(claims.account_id),
                str(claims.organisation_id),
            )
    except (SQLAlchemyError, TimeoutError):
        _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
        return Response(
            _JSON_AUTH_DB_UNAVAILABLE,
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    if live_role is None:
        return Response(
            _JSON_FORBIDDEN_ORG_MEMBERSHIP,
            status_code=403,
            media_type=_CT_APPLICATION_JSON,
        )
    role = clamp_oauth_role(scope_role, live_role)

    _ctx_org_id.set(claims.organisation_id)
    _ctx_role.set(role)
    _ctx_key_id.set(uuid.UUID(int=0))  # sentinel for OAuth clients
    _ctx_user_id.set(claims.account_id)
    _ctx_auth_token.set(token)
    _ctx_auth_type.set("oauth")
    _ctx_team_id.set(None)  # user tokens carry no team boundary
    request.scope["auth_principal"] = {
        "type": "user",
        "org_id": str(claims.organisation_id),
        "user_id": str(claims.account_id),
    }

    await _set_authz_enforce(claims.organisation_id)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on every MCP request.

    Supports two credential types (checked in order):
    1. API key bearer token (``mk_`` prefix)
    2. OAuth 2.0 access token (JWT with purpose=oauth_access)
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        unauth = await _dispatch_unauth_paths(request, call_next)
        if unauth is not None:
            return unauth

        # FAR-418: lift the node-level allowed_tools allow-list (if the calling
        # agent forwarded it) into the request-scoped ContextVar consumed by
        # check_tool_scope. Absent/empty header = UNRESTRICTED, preserving the
        # pre-scope behaviour for all non-node tool calls.
        raw_allowed = request.headers.get("X-Modulo-Allowed-Tools")
        if raw_allowed:
            set_request_allowed_tools([t.strip() for t in raw_allowed.split(",") if t.strip()])
        else:
            set_request_allowed_tools(None)

        token, auth_err = _extract_bearer_token(request)
        if auth_err is not None:
            return auth_err
        if token is None:
            raise RuntimeError("McpAuthMiddleware.dispatch: token unresolved after successful extraction")

        # Try API key first (backwards compatible).
        if token.startswith("mk_"):
            handled, api_err = await _authenticate_api_key(request, token)
            if api_err is not None:
                return api_err
            if handled:
                await _set_authz_enforce(_ctx_org_id.get())
                return await call_next(request)

        # Try OAuth access token (JWT).
        settings = get_settings()
        handled2, oauth_err, claims = await _authenticate_oauth_jwt(request, token, settings)
        if oauth_err is not None:
            return oauth_err
        if handled2:
            await _set_authz_enforce(_ctx_org_id.get())
            return await call_next(request)

        # Verify token family is not blacklisted.
        family_err = await _verify_oauth_token_family(token, claims)
        if family_err is not None:
            return family_err

        return await _finalize_oauth_principal(request, token, claims, call_next)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Modulo",
    instructions=(
        "Modulo is agent governance for your agentic SDLC. "
        "Use create_pipeline to define new pipelines, trigger_pipeline to fire runs, get_run_status to track them, "
        "get_run_output to inspect node outputs, "
        "and review_hitl to handle human-in-the-loop gates."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _tool_error(msg: str) -> dict[str, Any]:
    """Return a safe error dict so internal traces don't leak to the MCP client."""
    return {"error": "internal_error", "detail": msg}


def _tool_auth_error(msg: str) -> dict[str, Any]:
    """Return an auth-expired error dict for revoked/expired credentials."""
    return {"error": "auth_expired", "detail": msg}


def _parse_uuid_param(value: str, field: str) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse a UUID tool param, returning ``(value, None)`` or ``(None, error_dict)``."""
    try:
        return uuid.UUID(value), None
    except ValueError:
        return None, {"error": "invalid_id", "field": field, "detail": f"Invalid UUID format: {value}"}


def _serialize_edges(edges: list[Any]) -> list[dict[str, Any]]:
    """Serialize pipeline-graph edges to the MCP response shape."""
    return [
        {
            "id": str(e.id),
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
        }
        for e in edges
    ]


def _serialize_run_evals(evals: list[Any]) -> list[dict[str, Any]]:
    """Serialize run eval rows to the MCP response shape."""
    return [
        {
            "id": str(e.id),
            "eval_id": str(e.eval_id),
            "node_id": str(e.node_id) if e.node_id else None,
            "passed": e.passed,
            "score": e.score,
            "detail": e.detail,
            "evaluated_at": e.evaluated_at.isoformat() if e.evaluated_at else None,
        }
        for e in evals
    ]


def _iso_or_none(value: Any) -> str | None:
    """ISO-format a datetime column, or None."""
    return value.isoformat() if value else None


def _parse_basic_auth_header(request: Request, params: dict[str, str]) -> tuple[dict[str, str], JSONResponse | None]:
    """Parse an HTTP Basic Authorization header into client credentials.

    Extracts ``client_id`` / ``client_secret`` from the ``Authorization``
    header when present, only filling fields the form body did not supply.
    Returns ``(creds_delta, None)`` on success or ``({}, error_response)`` on a
    malformed header. RFC 6749 §2.3.1.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith(_BASIC_PREFIX):
        return {}, None
    import base64 as _base64

    try:
        decoded = _base64.b64decode(auth_header[len(_BASIC_PREFIX) :]).decode("utf-8")
        basic_id, _, basic_secret = decoded.partition(":")
    except Exception:
        return {}, JSONResponse(
            {"error": "invalid_request", "detail": "Malformed Basic Authorization header"},
            status_code=400,
        )
    creds: dict[str, str] = {}
    if not params.get("client_secret", ""):
        creds["client_secret"] = basic_secret
    if not params.get("client_id", ""):
        creds["client_id"] = basic_id
    return creds, None


async def _load_trigger_row(s: AsyncSession, org_id: uuid.UUID, tid: uuid.UUID) -> Any | None:
    """Load a non-deleted trigger row, org-scoped. None when not found."""
    from sqlalchemy import select

    from modulo.db.models.trigger import Trigger

    q = select(Trigger).where(
        Trigger.id == tid,
        Trigger.organisation_id == org_id,
        Trigger.deleted_at.is_(None),
    )
    return (await s.execute(q)).scalar_one_or_none()


def _validate_trigger_numbers(
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
) -> dict[str, Any] | None:
    """Validate the numeric trigger fields; returns an error dict, or None."""
    if max_concurrent_runs is not None and max_concurrent_runs < 1:
        return {"error": "validation", "field": "max_concurrent_runs", "detail": "must be >= 1"}
    if daily_spend_limit is not None and daily_spend_limit < 0:
        return {"error": "validation", "field": "daily_spend_limit", "detail": "must be >= 0"}
    return None


@mcp.tool(
    name="list_pipelines",
    description=(
        "List pipelines in the organisation with cursor-based pagination. Returns summaries. "
        "For raw text output, see the modulo://pipelines resource."
    ),
)
@_RETRY_DB
async def list_pipelines_tool(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        org_id = _ctx_org_id_val()
        from modulo.db.crud.pipeline import list_pipelines

        lim = max(1, min(limit, 100))
        async with _session(org_id) as s:
            result = await list_pipelines(s, cursor=cursor, page_size=lim, team_id=_ctx_team_id_val())
        return {
            "data": [{"id": str(p.id), "name": p.name, "visibility": p.visibility} for p in result.items],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except ProgrammingError:
        _log.exception("list_pipelines_tool failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_pipelines_tool failed")
        return _tool_error("Failed to list pipelines")


@mcp.tool(description="Create a new pipeline in the organisation. Returns the created pipeline details.")
@_RETRY_DB
async def create_pipeline(
    name: str,
    description: str | None = None,
    visibility: str = "org",
    max_concurrent_runs: int = 5,
    lock_wait_timeout_seconds: int = 300,
    node_timeout_seconds: int = 300,
    default_autonomy_level: str = "manual_approval",
    folder_id: str | None = None,
) -> dict[str, Any]:
    parsed_folder_id: uuid.UUID | None = None
    if folder_id is not None:
        try:
            parsed_folder_id = uuid.UUID(folder_id)
        except ValueError:
            return {"error": "invalid_folder_id", "detail": f"Invalid folder_id UUID: {folder_id}"}

    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_pipeline")
        from modulo.db.crud.pipeline import create_pipeline

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            pipeline = await create_pipeline(
                s,
                org_id=org_id,
                name=name,
                account_id=account_id,
                description=description,
                visibility=visibility,
                max_concurrent_runs=max_concurrent_runs,
                lock_wait_timeout_seconds=lock_wait_timeout_seconds,
                node_timeout_seconds=node_timeout_seconds,
                default_autonomy_level=default_autonomy_level,
                folder_id=parsed_folder_id,
            )

        return {
            "id": str(pipeline.id),
            "name": pipeline.name,
            "description": pipeline.description,
            "visibility": pipeline.visibility,
            "max_concurrent_runs": pipeline.max_concurrent_runs,
            "default_autonomy_level": pipeline.default_autonomy_level,
            "created_at": pipeline.created_at.isoformat() if pipeline.created_at else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("create_pipeline failed")
        return _tool_error("Failed to create pipeline")


def _mcp_run_item(r: Any, child_rollup: dict[Any, tuple[Any, int]]) -> dict[str, Any]:
    child_cost, child_count = child_rollup.get(r.id, (_MCP_COST_ROLLUP_ZERO, 0))
    child_cost = _quantize_mcp_cost_rollup(child_cost)
    own_cost = r.total_cost_usd if r.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
    _error_code, error_detail = present_error(r.error_code, r.error_detail, limit=200)
    return {
        "id": str(r.id),
        "pipeline_id": str(r.pipeline_id),
        "status": r.status,
        "trigger_type": r.trigger_type,
        "run_number": r.run_number,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "error_code": _error_code,
        "error_detail": error_detail,
        "total_cost_usd": float(r.total_cost_usd) if r.total_cost_usd is not None else None,
        "child_runs_cost_usd": float(child_cost),
        "child_runs_count": child_count,
        "aggregate_cost_usd": float(_quantize_mcp_cost_rollup(own_cost + child_cost)),
    }


@mcp.tool(
    description="List pipeline runs with filtering and cursor-based pagination.",
)
@_RETRY_DB
async def list_runs(
    pipeline_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        return await _list_runs_impl(pipeline_id, status, cursor, limit)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_runs failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_runs failed")
        return _tool_error("Failed to list runs")


async def _list_runs_impl(
    pipeline_id: str | None,
    status: str | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("list_runs")
    from modulo.db.crud.run import get_child_run_rollup
    from modulo.db.crud.run import list_runs as db_list_runs

    org_id = _ctx_org_id_val()
    pid = uuid.UUID(pipeline_id) if pipeline_id else None
    async with _session(org_id) as s:
        if pid is not None:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", str(pid))
        result = await db_list_runs(
            s,
            pipeline_id=pid,
            status=status,
            page=1,
            page_size=limit,
            cursor=cursor,
            team_id=_ctx_team_id_val(),
        )
        # Child-run cost+count rollup: ONE GROUP BY query for the whole
        # page, joined in Python — never a per-row aggregate (avoids N+1).
        run_ids = [r.id for r in result.items]
        child_rollup = await get_child_run_rollup(s, run_ids) if run_ids else {}
    items = [_mcp_run_item(r, child_rollup) for r in result.items]
    return {
        "items": items,
        "total": result.total,
        "next_cursor": result.next_cursor,
        "has_more": result.has_more,
    }


def _parse_mcp_datetime(value: str, name: str) -> datetime:
    """Parse an MCP date/datetime param (bare date or ISO datetime) into a datetime.

    Matches the REST surface: "2026-08-06" is accepted as midnight, ISO
    datetimes accept a trailing 'Z' (Python 3.11+ fromisoformat handles it).
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.combine(_date.fromisoformat(value), datetime.min.time())
    except ValueError:
        raise AnalyticsValidationError(f"{name}: invalid date value {value!r}") from None


def _analytics_deep_link(result: dict[str, Any], params: AnalyticsParams) -> str:
    """Relative /analytics deep link carrying the same filters as the query.

    Built from the RESOLVED result (``group_by``/``dimension``/``date_from``/
    ``date_to`` reflect the service's normalised effective range) plus the raw
    ``params`` filters (trigger/status/pipeline/folder/error_code). Emitted only
    on the MCP surface so Remy can hand the user a clickable, pre-filtered link
    to the /analytics view. The REST route keeps its clean ``AnalyticsResponse``
    contract — this field is presentation-only.
    """
    parts: list[tuple[str, str]] = [("group_by", str(result.get("group_by") or params.group_by.value))]
    dimension = result.get("dimension")
    if dimension:
        parts.append(("dimension", str(dimension)))
    if params.trigger_type is not None:
        parts.append(("trigger_type", params.trigger_type.value))
    if params.status is not None:
        parts.append(("status", params.status.value))
    parts.extend(("pipeline_id", str(pid)) for pid in params.pipeline_ids)
    if params.error_code is not None:
        parts.append(("error_code", params.error_code))
    if params.folder_id is not None:
        parts.append(("folder_id", str(params.folder_id)))
    date_from = result.get("date_from")
    if date_from:
        parts.append(("date_from", str(date_from)))
    date_to = result.get("date_to")
    if date_to:
        parts.append(("date_to", str(date_to)))
    return "/analytics?" + urlencode(parts)


async def _require_analytics_feature(org_id: uuid.UUID, settings: Any) -> dict[str, Any] | None:
    from modulo.core.feature_flags import resolve_plan_context
    from modulo.db.crud.organisation import get_organisation

    async with _session(org_id) as s:
        org = await get_organisation(s, org_id)
    async with _session(org_id) as s:
        plan_ctx = await resolve_plan_context(settings, s, org)
    if not plan_ctx.feature_enabled("analytics_page"):
        return {"error": "feature_required", "detail": "analytics_page is not available on your plan"}
    return None


def _parse_analytics_enums(
    group_by: str,
    trigger_type: str | None,
    status: str | None,
    dimension: str | None = None,
    error_detail: str = "",
) -> tuple[
    AnalyticsGroupBy | None,
    AnalyticsTriggerType | None,
    AnalyticsStatus | None,
    AnalyticsDimension | None,
    dict[str, Any] | None,
]:
    """Parse analytics enum params, returning ``(group_by, trigger, status, dimension, error)``."""
    try:
        grp = AnalyticsGroupBy(group_by)
        tt = AnalyticsTriggerType(trigger_type) if trigger_type is not None else None
        st = AnalyticsStatus(status) if status is not None else None
        dim = AnalyticsDimension(dimension) if dimension is not None else None
    except ValueError:
        return (
            None,
            None,
            None,
            None,
            {
                "error": "invalid_params",
                "detail": error_detail,
            },
        )
    return grp, tt, st, dim, None


def _parse_analytics_ids(
    pipeline_id: list[str] | None,
    folder_id: str | None,
) -> tuple[tuple[uuid.UUID, ...], uuid.UUID | None, dict[str, Any] | None]:
    """Parse the analytics pipeline/folder filters, returning ``(pids, fid, error)``."""
    pids: tuple[uuid.UUID, ...] = ()
    if pipeline_id:
        try:
            pids = tuple(uuid.UUID(p) for p in pipeline_id)
        except ValueError:
            return (), None, {"error": "invalid_params", "detail": "pipeline_id entries must be valid UUIDs"}

    fid: uuid.UUID | None = None
    if folder_id is not None:
        try:
            fid = uuid.UUID(folder_id)
        except ValueError:
            return (), None, {"error": "invalid_params", "detail": f"Invalid folder_id UUID: {folder_id}"}
    return pids, fid, None


def _build_analytics_params(
    group_by: AnalyticsGroupBy,
    auto_granularity: bool,
    trigger_type: AnalyticsTriggerType | None,
    status: AnalyticsStatus | None,
    dimension: AnalyticsDimension | None,
    pipeline_ids: tuple[uuid.UUID, ...],
    folder_id: uuid.UUID | None,
    error_code: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> AnalyticsParams:
    return AnalyticsParams(
        group_by=group_by,
        auto_granularity=auto_granularity,
        dimension=dimension,
        trigger_type=trigger_type,
        status=status,
        pipeline_ids=pipeline_ids,
        team_id=_ctx_team_id_val(),
        error_code=error_code,
        folder_id=folder_id,
        date_from=_parse_mcp_datetime(date_from, "date_from") if date_from is not None else None,
        date_to=_parse_mcp_datetime(date_to, "date_to") if date_to is not None else None,
        limit=max(1, min(limit, 1000)),
    )


def _parse_analytics_params(
    dimension: str | None,
    group_by: str,
    auto_granularity: bool,
    trigger_type: str | None,
    status: str | None,
    pipeline_id: list[str] | None,
    folder_id: str | None,
    error_code: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> tuple[AnalyticsParams | None, dict[str, Any] | None]:
    grp, tt, st, dim, enum_err = _parse_analytics_enums(
        group_by,
        trigger_type,
        status,
        dimension,
        error_detail=f"invalid enum value (dimension={dimension!r} group_by={group_by!r})",
    )
    if enum_err:
        return None, enum_err
    assert grp is not None  # nosec B101 -- _parse_analytics_enums returns (None, error) only on failure, already handled above
    pids, fid, id_err = _parse_analytics_ids(pipeline_id, folder_id)
    if id_err:
        return None, id_err
    params = _build_analytics_params(
        grp, auto_granularity, tt, st, dim, pids, fid, error_code, date_from, date_to, limit
    )
    return params, None


def _parse_analytics_concurrency_params(
    group_by: str,
    auto_granularity: bool,
    trigger_type: str | None,
    status: str | None,
    pipeline_id: list[str] | None,
    folder_id: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> tuple[AnalyticsParams | None, dict[str, Any] | None]:
    grp, tt, st, _dim, enum_err = _parse_analytics_enums(
        group_by,
        trigger_type,
        status,
        dimension=None,
        error_detail=f"invalid enum value (group_by={group_by!r})",
    )
    if enum_err:
        return None, enum_err
    assert grp is not None  # nosec B101 -- _parse_analytics_enums returns (None, error) only on failure, already handled above
    pids, fid, id_err = _parse_analytics_ids(pipeline_id, folder_id)
    if id_err:
        return None, id_err
    params = _build_analytics_params(grp, auto_granularity, tt, st, None, pids, fid, None, date_from, date_to, limit)
    return params, None


@dataclass(frozen=True)
class _AnalyticsQueryInput:
    """Raw MCP tool params for an analytics query (or concurrency query)."""

    dimension: str | None = None
    group_by: str = "day"
    auto_granularity: bool = False
    trigger_type: str | None = None
    status: str | None = None
    pipeline_id: list[str] | None = None
    error_code: str | None = None
    folder_id: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 1000


async def _query_analytics_impl(input: _AnalyticsQueryInput) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("query_analytics")

    org_id = _ctx_org_id_val()
    settings = get_settings()

    feat_err = await _require_analytics_feature(org_id, settings)
    if feat_err:
        return feat_err

    params, p_err = _parse_analytics_params(
        input.dimension,
        input.group_by,
        input.auto_granularity,
        input.trigger_type,
        input.status,
        input.pipeline_id,
        input.folder_id,
        input.error_code,
        input.date_from,
        input.date_to,
        input.limit,
    )
    if p_err:
        return p_err
    if params is None:
        raise RuntimeError("_query_analytics_impl: parse returned no error and no params")

    result = await run_analytics_query(
        org_id=org_id,
        params=params,
        factory=_get_session_factory(),
        settings=settings,
        account_id=_ctx_user_id_val(),
        org_role=_ctx_role_val() or "",
    )
    result["deep_link"] = _analytics_deep_link(result, params)
    return result


@mcp.tool(
    name="query_analytics",
    description=(
        "Query run analytics over the daily facts table. Returns a bucketed series "
        "(hour/day/week) with per-bucket count, cost, tokens, duration, success rate, "
        "failure and stall counts, queue wait, final idle, and output size. "
        "Accepts a repeated pipeline_id for A-vs-B comparisons in a single request, "
        "and error_code for filtering/grouping by failure code. The result also "
        "carries a `deep_link` to the /analytics view pre-filtered with the same "
        "parameters — share that link instead of dumping the raw buckets. Requires "
        "the analytics.query permission and the analytics_page plan feature."
    ),
)
@_RETRY_DB
async def query_analytics(
    dimension: str | None = None,
    group_by: str = "day",
    auto_granularity: bool = False,
    trigger_type: str | None = None,
    status: str | None = None,
    pipeline_id: list[str] | None = None,
    error_code: str | None = None,
    folder_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    try:
        return await _query_analytics_impl(
            _AnalyticsQueryInput(
                dimension=dimension,
                group_by=group_by,
                auto_granularity=auto_granularity,
                trigger_type=trigger_type,
                status=status,
                pipeline_id=pipeline_id,
                error_code=error_code,
                folder_id=folder_id,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except AnalyticsRateLimitedError:
        return {"error": "rate_limited", "detail": "Rate limit exceeded"}
    except AnalyticsValidationError as exc:
        return {"error": "invalid_params", "detail": exc.detail}
    except AnalyticsQueryTimeoutError as exc:
        return {"error": "query_timeout", "detail": str(exc)}
    except AnalyticsMigrationRequiredError as exc:
        return {"error": "migration_required", "detail": str(exc)}
    except AnalyticsDatabaseError as exc:
        return {"error": "database_error", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("query_analytics failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("query_analytics failed")
        return _tool_error("Failed to query analytics")


async def _query_analytics_concurrency_impl(input: _AnalyticsQueryInput) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("query_analytics_concurrency")

    org_id = _ctx_org_id_val()
    settings = get_settings()

    feat_err = await _require_analytics_feature(org_id, settings)
    if feat_err:
        return feat_err

    params, p_err = _parse_analytics_concurrency_params(
        input.group_by,
        input.auto_granularity,
        input.trigger_type,
        input.status,
        input.pipeline_id,
        input.folder_id,
        input.date_from,
        input.date_to,
        input.limit,
    )
    if p_err:
        return p_err
    if params is None:
        raise RuntimeError("_query_analytics_concurrency_impl: parse returned no error and no params")

    return await run_concurrency_query(
        org_id=org_id,
        params=params,
        factory=_get_session_factory(),
        settings=settings,
        account_id=_ctx_user_id_val(),
        org_role=_ctx_role_val() or "",
    )


@mcp.tool(
    name="query_analytics_concurrency",
    description=(
        "Query slot utilization / concurrency over the daily facts table. Returns a "
        "bucketed series (hour/day/week) with per-bucket max and average concurrent "
        "active runs (computed from [started_at, completed_at) overlap — a run "
        "spanning a bucket boundary counts in both) and max and average queued runs "
        "(created before started_at; never-started runs count as queued through the "
        "range). Also returns pool_reference: the org run_concurrency_limit, or a "
        "single filtered pipeline's max_concurrent_runs. Accepts a repeated "
        "pipeline_id filter. Requires the analytics.query permission and the "
        "analytics_page plan feature."
    ),
)
@_RETRY_DB
async def query_analytics_concurrency(
    group_by: str = "day",
    auto_granularity: bool = False,
    trigger_type: str | None = None,
    status: str | None = None,
    pipeline_id: list[str] | None = None,
    folder_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    try:
        return await _query_analytics_concurrency_impl(
            _AnalyticsQueryInput(
                group_by=group_by,
                auto_granularity=auto_granularity,
                trigger_type=trigger_type,
                status=status,
                pipeline_id=pipeline_id,
                folder_id=folder_id,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except AnalyticsRateLimitedError:
        return {"error": "rate_limited", "detail": "Rate limit exceeded"}
    except AnalyticsValidationError as exc:
        return {"error": "invalid_params", "detail": exc.detail}
    except AnalyticsQueryTimeoutError as exc:
        return {"error": "query_timeout", "detail": str(exc)}
    except AnalyticsMigrationRequiredError as exc:
        return {"error": "migration_required", "detail": str(exc)}
    except AnalyticsDatabaseError as exc:
        return {"error": "database_error", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("query_analytics_concurrency failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("query_analytics_concurrency failed")
        return _tool_error("Failed to query analytics concurrency")


@mcp.tool(
    name="get_pipeline_graph",
    description="Get the full graph (nodes + edges) of a pipeline by ID. "
    "Returns nodes with their configuration (agent_prompt, agent_command, template_id, timeout_seconds, etc.) "
    "and edges with their source/target/type. "
    "For pipelines that use sandbox_agent nodes, this is how you read the current agent_command before modifying it.",
)
@_RETRY_DB
async def get_pipeline_graph_tool(
    pipeline_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        from modulo.db.crud.pipeline import get_pipeline_graph

        org_id = _ctx_org_id_val()
        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        if pid is None:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": _MSG_UUID_PARSE_FAILED}

        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            result = await get_pipeline_graph(s, pid)

        if result is None:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

        nodes, edges = result
        edge_dicts = _serialize_edges(edges)

        return {
            "pipeline_id": pipeline_id,
            "nodes": nodes,
            "edges": edge_dicts,
            "node_count": len(nodes),
            "edge_count": len(edge_dicts),
        }
    except ProgrammingError:
        _log.exception("get_pipeline_graph_tool failed")
        return {
            "error": "migration_required",
            "detail": "Database migration may be required. Run alembic upgrade heads.",
        }
    except Exception:
        _log.exception("get_pipeline_graph_tool failed")
        return _tool_error("Failed to get pipeline graph")


async def _append_mcp_hitl_denial_audit(
    org_id: uuid.UUID, pipeline_id: uuid.UUID, exc: HitlGateWeakeningDenied
) -> None:
    """Append the hitl_gate_removal_denied audit event for an MCP denial.

    Runs in a fresh ``_session`` after the guarded write's transaction rolled
    back, so the denial is never lost (hitl-gate-removal-guard-plan.md v19 §5).
    Best-effort: an audit failure is logged but never masks the denial.
    """
    try:
        from modulo.core.audit_logger import append_audit_event

        payload = exc.payload_json or {
            "caller_type": "mcp",
            "reason_code": exc.reason_code,
            "denied": True,
            "affected_edges": [
                {"source_node_id": k[0], "target_node_id": k[1], "edge_type": k[2]} for k in exc.correlation_keys
            ],
            "weakening_types": exc.weakening_types,
        }
        async with _session(org_id) as s:
            try:
                actor_user_id = _ctx_user_id_val()
            except McpAuthContextError:
                actor_user_id = None
            await append_audit_event(
                s,
                org_id=org_id,
                event_type="hitl_gate_removal_denied",
                actor_user_id=actor_user_id,
                resource_type="pipeline",
                resource_id=pipeline_id,
                payload_json=payload,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("mcp.hitl_denial_audit_failed", extra={"org_id": str(org_id)})


async def _update_pipeline_graph_impl(
    pipeline_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("update_pipeline_graph")
    from modulo.core.team_visibility import (
        CONNECTOR_TEAM_MISMATCH,
        connector_team_mismatch_detail,
        extract_connector_bindings,
        find_connector_team_mismatches,
    )
    from modulo.db.crud.pipeline import replace_pipeline_graph

    org_id = _ctx_org_id_val()
    pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
    if pid_err:
        return pid_err
    assert pid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    # ADR 017 service-layer backstop + hitl-gate-removal-guard-plan.md v19:
    # the MCP surface is structurally excluded from gate weakening. The
    # guarded function hardcodes is_privileged=False when
    # caller_type=="mcp" (no DB query); the literal below is enforced by a
    # .semgrep/ rule (mcp call site must pass the literal, not a variable).
    from modulo.api.routes.pipelines import (
        PipelineGraphUpdate,
        _is_privileged,
    )

    is_privileged = _is_privileged(_ctx_role_val())

    # FAR-309 PR A review: the guardrail-binding strip guard lives in the
    # SERVICE LAYER (replace_pipeline_graph, under the row lock) so the MCP
    # surface inherits it — no separate call-site check. The admin flag is
    # resolved from the caller's org role; for MCP the service layer uses it
    # as-is (the MCP role is resolved at the tool boundary).
    _mcp_is_guardrail_admin = _ctx_role_val() == "admin"

    # Validate graph structure using Pydantic models (same as REST endpoint)
    from pydantic import ValidationError as _PydanticValidationError

    try:
        PipelineGraphUpdate.model_validate({"nodes": nodes, "edges": edges})
    except _PydanticValidationError as exc:
        return {
            "error": "validation_failed",
            "detail": f"Graph validation failed: {exc.errors(include_url=False)}",
        }

    # FAR-296 mode-aware sandbox_agent gate — the SAME shared helper the
    # Pydantic model, node runner, and GraphValidator use, applied to the
    # raw node dicts so this gate agrees with save-time and run-time
    # validation even if the Pydantic surface is bypassed.
    sandbox_err = _validate_sandbox_nodes(nodes)
    if sandbox_err:
        return sandbox_err

    try:
        async with _session(org_id) as s:
            from modulo.db.crud.pipeline import get_pipeline

            pipeline = await get_pipeline(s, pid)
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            if _team_scoped_key_mismatch(pipeline.owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            mismatches = await find_connector_team_mismatches(
                s,
                org_id=org_id,
                pipeline_owner_team_id=pipeline.owner_team_id,
                connector_bindings=extract_connector_bindings(nodes),
            )
            if mismatches:
                return {
                    "error": CONNECTOR_TEAM_MISMATCH,
                    "detail": connector_team_mismatch_detail(mismatches),
                }
            # FAR-309 PR A review: the guardrail-binding strip guard runs in the
            # service layer (replace_pipeline_graph, under the row lock) — the
            # MCP surface inherits it via caller_type="mcp".
            result = await replace_pipeline_graph(
                s,
                pipeline_id=pid,
                org_id=org_id,
                nodes=nodes,
                edges=edges,
                is_privileged=is_privileged,
                caller_type="mcp",
                is_guardrail_admin=_mcp_is_guardrail_admin,
            )
            if result is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            updated_nodes, updated_edges = result
    except HitlGateWeakeningDenied as exc:
        await _append_mcp_hitl_denial_audit(org_id, pid, exc)
        return {
            "error": "hitl_gate_removal_denied",
            "detail": str(exc),
            "reason_code": exc.reason_code,
            "affected_edges": [
                {"source_node_id": k[0], "target_node_id": k[1], "edge_type": k[2]} for k in exc.correlation_keys
            ],
        }
    except GuardrailBindingStripDenied as exc:
        return {
            "error": "guardrail_strip_forbidden",
            "detail": exc.detail,
        }

    return {
        "pipeline_id": pipeline_id,
        "nodes": updated_nodes,
        "edges": _serialize_edges(updated_edges),
        "node_count": len(updated_nodes),
        "edge_count": len(updated_edges),
    }


@mcp.tool(
    description="Set or replace the graph (nodes + edges) of an existing pipeline. "
    "Pass nodes as a list of dicts with id, node_type, agent_id, position (x, y), "
    "and edges as a list of dicts with id, source_node_id, target_node_id, edge_type. "
    "Returns the updated graph."
)
@_RETRY_DB
async def update_pipeline_graph(
    pipeline_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return await _update_pipeline_graph_impl(pipeline_id, nodes, edges)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("update_pipeline_graph failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("update_pipeline_graph failed")
        return _tool_error("Failed to update pipeline graph")


def _validate_sandbox_nodes(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Mode-aware sandbox_agent gate over raw node dicts.

    Applies the same shared validation helper the Pydantic model, node runner,
    and GraphValidator use. Imported from the lightweight sandbox_mode module
    (no LangGraph) to keep the API layer import-linter-clean. Returns None if
    all sandbox_agent nodes validate, otherwise the error dict.
    """
    from modulo.core.pipeline_engine.sandbox_mode import (
        _validate_sandbox_mode_config,
        validate_sandbox_agent_command_jinja,
    )

    for node in nodes:
        if node.get("node_type") == "sandbox_agent":
            try:
                _validate_sandbox_mode_config(node)
            except ValueError as exc:
                return {"error": "validation_failed", "field": "nodes", "detail": str(exc)}
            jinja_err = validate_sandbox_agent_command_jinja(node)
            if jinja_err:
                return {"error": "validation_failed", "field": "nodes", "detail": jinja_err}
    return None


def _apply_node_connector_binding(
    pipeline: Any,
    nid: uuid.UUID,
    node_id: str,
    connector_type: str,
    connector_instance_id: str,
) -> dict[str, Any] | None:
    """Bind the connector onto the matching node. Returns an error dict, or None."""
    nodes = list(pipeline.graph_nodes_json) if pipeline.graph_nodes_json else []
    target = None
    for node in nodes:
        if uuid.UUID(node["id"]) == nid:
            target = node
            break
    if target is None:
        return {"error": "node_not_found", "detail": f"Node {node_id} not found in pipeline graph"}

    target["connector_binding"] = {
        "type": connector_type,
        "instance_id": connector_instance_id,
    }
    pipeline.graph_nodes_json = nodes
    return None


@mcp.tool(
    description="Bind a connector instance to a pipeline node. "
    "Updates the node's connector_binding in the pipeline graph. "
    "The connector must already exist in the organisation."
)
@_RETRY_DB
async def bind_connector_to_node(
    pipeline_id: str,
    node_id: str,
    connector_type: str,
    connector_instance_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("bind_connector_to_node")

        from modulo.db.crud.connector_instance import get_connector_instance

        org_id = _ctx_org_id_val()
        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        nid, nid_err = _parse_uuid_param(node_id, "node_id")
        if nid_err:
            return nid_err
        cid, cid_err = _parse_uuid_param(connector_instance_id, "connector_instance_id")
        if cid_err:
            return cid_err
        if pid is None or nid is None or cid is None:
            return {"error": "invalid_id", "detail": _MSG_UUID_PARSE_FAILED}

        async with _session(org_id) as s:
            # Verify connector exists in org
            connector = await get_connector_instance(s, cid)
            if connector is None or connector.organisation_id != org_id:
                return {"error": "connector_not_found", "detail": "Connector not found in this organisation"}

            # Get pipeline and update node
            from sqlalchemy import select

            from modulo.db.models.pipeline import Pipeline

            pipeline = (
                await s.execute(select(Pipeline).where(Pipeline.id == pid).with_for_update())
            ).scalar_one_or_none()
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

            if _team_scoped_key_mismatch(pipeline.owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)

            from modulo.core.team_visibility import (
                CONNECTOR_TEAM_MISMATCH,
                connector_team_mismatch,
            )

            if connector_team_mismatch(connector.visibility, connector.owner_team_id, pipeline.owner_team_id):
                return {
                    "error": CONNECTOR_TEAM_MISMATCH,
                    "detail": (
                        f"connector_team_mismatch: connector '{connector.name}' (id={cid}) is team-private "
                        f"(owner team {connector.owner_team_id}) but pipeline is owned by team "
                        f"{pipeline.owner_team_id}"
                    ),
                }

            bind_error = _apply_node_connector_binding(pipeline, nid, node_id, connector_type, connector_instance_id)
            if bind_error is not None:
                return bind_error
            await s.flush()

        return {
            "pipeline_id": pipeline_id,
            "node_id": node_id,
            "connector_type": connector_type,
            "connector_instance_id": connector_instance_id,
            "status": "bound",
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("bind_connector_to_node failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("bind_connector_to_node failed")
        return _tool_error("Failed to bind connector to node")


def _trigger_pipeline_validate_id(pipeline_id: str) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    return _parse_uuid_param(pipeline_id, "pipeline_id")


async def _create_manual_run(
    s: AsyncSession,
    org_id: uuid.UUID,
    pid: uuid.UUID,
    pipeline_id: str,
    payload: dict[str, Any],
) -> tuple[uuid.UUID | None, str | None, dict[str, Any] | None]:
    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
    from modulo.db.crud.run import create_run

    uid = _ctx_user_id_val()
    pipeline = await get_pipeline(s, pid)
    if pipeline is None:
        return None, None, {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
    if _team_scoped_key_mismatch(pipeline.owner_team_id):
        return None, None, _team_scope_error("pipeline", pipeline_id)
    snapshot = await create_snapshot_from_live_graph(s, pipeline_id=pid, account_id=uid)
    if snapshot is None:
        return None, None, {"error": "snapshot_failed", "pipeline_id": pipeline_id}
    if not snapshot.graph_json or not snapshot.graph_json.get("nodes"):
        return (
            None,
            None,
            {
                "error": "validation_failed",
                "detail": "Pipeline graph has no nodes — cannot trigger run",
            },
        )
    run = await create_run(
        s,
        org_id=org_id,
        pipeline_id=pid,
        snapshot_id=snapshot.id,
        trigger_type="manual",
        input_payload=payload,
    )
    return run.id, run.langgraph_thread_id, None


async def _trigger_pipeline_impl(
    pipeline_id: str,
    input_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("trigger_pipeline")
    if not await _trigger_pipeline_rate_allowed():
        _log.warning(
            "ratelimit.trigger_pipeline_exceeded",
            extra={"client_key": _trigger_pipeline_client_key()},
        )
        return {"error": "rate_limited", "detail": "Rate limit exceeded for trigger_pipeline (60/min)"}
    org_id = _ctx_org_id_val()
    pid, id_err = _trigger_pipeline_validate_id(pipeline_id)
    if id_err:
        return id_err
    if pid is None:
        raise RuntimeError("_trigger_pipeline_impl: validate returned no error and no pipeline id")
    payload = input_payload or {}

    async with _session(org_id) as s:
        run_id, thread_id, run_err = await _create_manual_run(s, org_id, pid, pipeline_id, payload)
    if run_err:
        return run_err
    if run_id is None:
        raise RuntimeError("_trigger_pipeline_impl: manual run creation returned no error and no run id")

    await dispatch_run(str(run_id), str(org_id), queue="runs")

    return {
        "run_id": str(run_id),
        "status": "pending",
        "langgraph_thread_id": thread_id,
    }


@mcp.tool(description="Fire a pipeline run and return immediately with run_id. Poll get_run_status to track progress.")
@_RETRY_DB
async def trigger_pipeline(
    pipeline_id: str,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return await _trigger_pipeline_impl(pipeline_id, input_payload)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except SnapshotLockNotAvailableError:
        _log.info("trigger_pipeline queued — snapshot lock not available for pipeline %s", pipeline_id)
        return {"pipeline_id": pipeline_id, "status": "queued", "detail": "Pipeline busy — queued for retry"}
    except OrgDeletedError as exc:
        _log.exception("trigger_pipeline failed — organisation deleted or missing")
        if exc.deleted:
            return {"error": "org_deleted", "detail": f"Organisation {exc.org_id} is deleted"}
        return {"error": "org_not_found", "detail": f"Organisation {exc.org_id} not found"}
    except StorageExhaustedError as exc:
        _log.warning("trigger_pipeline refused — storage exhausted (FAR-426)")
        return {"error": "storage_exhausted", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("trigger_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("trigger_pipeline failed")
        return _tool_error("Failed to trigger pipeline")


async def _load_run_for_status(s: AsyncSession, rid: uuid.UUID) -> Any | None:
    run = await get_run(s, rid)
    if run is None:
        return None
    # The run carries its own owner_team_id (snapshot at creation) — that
    # is the source of truth, not the pipeline's current team assignment.
    # Legacy runs with a NULL stamp fall back to the pipeline owner.
    run_owner_team_id = await _run_owner_team_id(s, run)
    if _team_scoped_key_mismatch(run_owner_team_id):
        return _TEAM_SCOPE_ERROR
    return run


def _run_status_base(run: Run) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "status": run.status,
        "trigger_type": run.trigger_type,
        "created_at": run.created_at.isoformat(),
    }
    if run.started_at:
        result["started_at"] = run.started_at.isoformat()
    if run.completed_at:
        result["completed_at"] = run.completed_at.isoformat()
    if run.error_code:
        result["error_code"] = map_legacy_code(run.error_code)
    if run.error_detail is not None:
        _, error_detail = present_error(run.error_code, run.error_detail, limit=5000)
        result["error_detail"] = error_detail
    return result


def _run_status_detail(run: Run) -> dict[str, Any]:
    from modulo.api.routes.runs import _clamp_node_token_usage_union

    token_usage = _clamp_node_token_usage_union(run.node_token_usage or {})
    outputs_json = run.outputs_json or {}
    telemetry_json = run.node_telemetry_json
    if not isinstance(telemetry_json, dict):
        telemetry_json = {}
    node_ids: set[str] = set()
    node_ids.update(token_usage.keys())
    node_ids.update(outputs_json.keys())
    node_ids.update(telemetry_json.keys())
    nodes = [_run_status_node(nid, token_usage, outputs_json, telemetry_json) for nid in sorted(node_ids)]
    result: dict[str, Any] = {"nodes": nodes}
    if run.cost_breakdown is not None:
        result["cost_breakdown"] = _sanitize_cost_breakdown(run.cost_breakdown)
    return result


def _run_status_node(
    nid: str,
    token_usage: dict[str, Any],
    outputs_json: dict[str, Any],
    telemetry_json: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate a single node's usage/status into its run-status dict entry."""
    usage = token_usage.get(nid, {})
    if not isinstance(usage, dict):
        usage = {}
    t_in = usage.get("input_tokens") or 0
    t_out = usage.get("output_tokens") or 0
    if nid in outputs_json:
        status = "completed"
    elif nid in telemetry_json:
        tel_entry = telemetry_json[nid]
        tel_status = tel_entry.get("status") if isinstance(tel_entry, dict) else None
        status = "failed" if tel_status == "failed" else "processed"
    else:
        status = "processed"
    node: dict[str, Any] = {
        "node_id": nid,
        "status": status,
        "input_tokens": t_in,
        "output_tokens": t_out,
        "total_tokens": usage.get("total_tokens") or (t_in + t_out),
        "cost_usd": usage.get("cost_usd", 0),
        "has_output": nid in outputs_json or nid in telemetry_json,
    }
    if usage.get("model_cost_display_usd") is not None:
        node["model_cost_display_usd"] = usage["model_cost_display_usd"]
    return node


async def _get_run_status_impl(run_id: str, detail: bool) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        run = await _load_run_for_status(s, rid)
        if run is _TEAM_SCOPE_ERROR:
            return _team_scope_error("run", run_id)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
    result = _run_status_base(run)
    if detail:
        result.update(_run_status_detail(run))
    return result


@mcp.tool(description="Get current run status. Pass detail=true for per-node breakdown.")
@_RETRY_DB
async def get_run_status(run_id: str, detail: bool = False) -> dict[str, Any]:
    try:
        return await _get_run_status_impl(run_id, detail)
    except ProgrammingError:
        _log.exception("get_run_status failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_run_status failed")
        return _tool_error("Failed to get run status")


def _resolve_run_node_output(outputs: dict[str, Any], telemetry: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """Resolve the node output dict, falling back to telemetry status/summary."""
    from modulo.core.node_output_split import node_return, node_telemetry

    node_output = node_return(outputs, telemetry, node_id)
    if node_output is None:
        node_meta = node_telemetry(telemetry, outputs, node_id)
        if isinstance(node_meta, dict):
            node_output = {key: node_meta[key] for key in ("status", "summary") if key in node_meta}
    return cast("dict[str, Any] | None", node_output)


def _detect_masked_fields(masked: Any) -> list[str]:
    """Keys whose masked value contains the bullet mask character."""
    if not isinstance(masked, dict):
        return []
    return [k for k, v in masked.items() if isinstance(v, str) and "\u2022" in v]


@mcp.tool(
    description=(
        "Get a specific node's output from a completed pipeline run. "
        "Sensitive fields (tokens, secrets, API keys, passwords, credentials) "
        "are masked in the response."
    ),
)
@_RETRY_DB
async def get_run_output(run_id: str, node_id: str) -> dict[str, Any]:
    try:
        return await _get_run_output_impl(run_id, node_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_output failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_run_output failed")
        return _tool_error("Failed to get node output")


async def _get_run_output_impl(run_id: str, node_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("get_run_output")
    from modulo.api.routes.runs import _mask_output_value

    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        run = await get_run(s, rid)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        run_owner_team_id = await _run_owner_team_id(s, run)
    if _team_scoped_key_mismatch(run_owner_team_id):
        return _team_scope_error("run", run_id)
    outputs = run.outputs_json or {}
    telemetry = run.node_telemetry_json
    if not isinstance(telemetry, dict):
        telemetry = {}
    node_output = _resolve_run_node_output(outputs, telemetry, node_id)
    if node_output is None:
        return {"error": "node_output_not_found", "run_id": run_id, "node_id": node_id}
    masked = _mask_output_value(node_output)
    masked_fields = _detect_masked_fields(masked)

    return {
        "node_id": node_id,
        "output": masked,
        "masked_fields": masked_fields,
    }


@mcp.tool(
    description="Get eval results for a given run. Returns structured eval outcomes "
    "including pass/fail status, scores, and detailed feedback.",
)
@_RETRY_DB
async def get_run_evals(run_id: str) -> dict[str, Any]:
    try:
        return await _get_run_evals_impl(run_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_evals failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_run_evals failed")
        return _tool_error("Failed to get run evals")


async def _get_run_evals_impl(run_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("get_run_evals")
    from modulo.db.crud.eval_run import get_run_evals as db_get_run_evals

    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    async with _session(org_id) as s:
        run = await get_run(s, rid)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
            return _team_scope_error("run", run_id)
        evals = await db_get_run_evals(s, rid)

    return {
        "run_id": run_id,
        "status": run.status,
        "evals": _serialize_run_evals(evals),
        "eval_count": len(evals),
    }


@mcp.tool(
    description="List eval definitions with cursor-based pagination. Optionally filter by pipeline_id.",
)
@_RETRY_DB
async def list_eval_definitions(
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_eval_definitions")
        from modulo.db.crud.eval_definition import list_eval_definitions as db_list_eval_definitions

        org_id = _ctx_org_id_val()
        pid = uuid.UUID(pipeline_id) if pipeline_id else None
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_eval_definitions(s, org_id, pipeline_id=pid, cursor=cursor, limit=lim)

        return {
            "data": [
                {
                    "id": str(d.id),
                    "name": d.name,
                    "type": d.eval_type,
                    "pipeline_id": str(d.pipeline_id),
                    "failure_behaviour": d.failure_behaviour,
                    "pass_threshold": d.pass_threshold,
                    "suite_id": d.suite_id,
                }
                for d in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_eval_definitions failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_eval_definitions failed")
        return _tool_error("Failed to list eval definitions")


_EVAL_FAILURE_BEHAVIOURS = ("warn", "block")

# Mirrors the REST ``max_length=255`` on the eval-definition name field so an
# oversized MCP-supplied name surfaces as ``invalid_name`` rather than a generic
# constraint conflict.
_EVAL_NAME_MAX_LENGTH = 255


def _assert_eval_type(eval_type: str) -> dict[str, Any] | None:
    """Validate an eval_type value, returning an error dict or None."""
    if not re.fullmatch(_EVAL_TYPE_PATTERN, eval_type):
        return {
            "error": "invalid_eval_type",
            "detail": "eval_type must be one of: llm_judge|regex|json_schema|custom_function|guardrail|human_set",
        }
    return None


def _assert_failure_behaviour(failure_behaviour: str) -> dict[str, Any] | None:
    """Validate a failure_behaviour value, returning an error dict or None."""
    if failure_behaviour not in _EVAL_FAILURE_BEHAVIOURS:
        return {
            "error": "invalid_failure_behaviour",
            "detail": "failure_behaviour must be 'warn' or 'block'",
        }
    return None


def _assert_pass_threshold(pass_threshold: float | None) -> dict[str, Any] | None:
    """Validate a pass_threshold value, returning an error dict or None."""
    if pass_threshold is not None and not (0.0 <= pass_threshold <= 1.0):
        return {
            "error": "invalid_pass_threshold",
            "detail": "pass_threshold must be between 0.0 and 1.0",
        }
    return None


def _assert_admin_scope(action: str) -> None:
    """Raise MCPAuthorizationError when the caller is not an org admin.

    Eval-definition management mirrors the REST routes: admin-only. A
    non-admin (operator/runner/viewer) caller must receive ``insufficient_scope``
    rather than a silent success.
    """
    if ORG_ROLE_HIERARCHY.get(_ctx_role_val() or "", -1) < ORG_ROLE_HIERARCHY["admin"]:
        raise MCPAuthorizationError(f"Only admins can {action} eval definitions")


@mcp.tool(
    description="Create a new eval definition (admin only). Persists an "
    "EvalDefinition row scoped to the caller's org and returns its details. "
    "Requires an admin caller; non-admins receive an insufficient_scope error.",
)
@_RETRY_DB
async def create_eval_definition(
    pipeline_id: str,
    node_id: str | None = None,
    name: str = "",
    eval_type: str = "",
    config_json: dict[str, Any] | None = None,
    failure_behaviour: str = "warn",
    pass_threshold: float | None = None,
    suite_id: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_eval_definition")

        from modulo.api.routes.evals import (
            _MSG_PIPELINE_NOT_FOUND,
            _eval_def_to_dict,
            _validate_guardrail_request,
        )

        if not name or not name.strip():
            return {"error": "invalid_name", "detail": "name must be a non-empty string"}
        if len(name) > _EVAL_NAME_MAX_LENGTH:
            return {
                "error": "invalid_name",
                "detail": f"name must be at most {_EVAL_NAME_MAX_LENGTH} characters",
            }
        if (err := _assert_eval_type(eval_type)) is not None:
            return err
        if (err := _assert_failure_behaviour(failure_behaviour)) is not None:
            return err
        if (err := _assert_pass_threshold(pass_threshold)) is not None:
            return err

        _assert_admin_scope("create")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        nid: uuid.UUID | None = None
        if node_id is not None:
            nid, nid_err = _parse_uuid_param(node_id, "node_id")
            if nid_err:
                return nid_err

        cfg = config_json if config_json is not None else {}

        try:
            _validate_guardrail_request(
                eval_type=eval_type,
                failure_behaviour=failure_behaviour,
                config_json=cfg,
            )
        except StarletteHTTPException as exc:
            return {"error": "validation_failed", "detail": str(exc.detail)}

        from modulo.db.models.eval_definition import EvalDefinition
        from modulo.db.models.pipeline import Pipeline

        async with _session(org_id) as s:
            pipeline = (
                await s.execute(
                    select(Pipeline).where(
                        Pipeline.id == pid,
                        Pipeline.organisation_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if pipeline is None:
                return {"error": "pipeline_not_found", "detail": _MSG_PIPELINE_NOT_FOUND}

            eval_def = EvalDefinition(
                organisation_id=org_id,
                pipeline_id=pid,
                node_id=nid,
                name=name,
                eval_type=eval_type,
                config_json=cfg,
                failure_behaviour=failure_behaviour,
                pass_threshold=pass_threshold,
                suite_id=suite_id,
                account_id=account_id,
                version=1,
            )
            s.add(eval_def)
            await s.flush()
            return _eval_def_to_dict(eval_def)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except StarletteHTTPException as exc:
        return {"error": "validation_failed", "detail": str(exc.detail)}
    except IntegrityError as exc:
        _log.exception("create_eval_definition failed")
        return {
            "error": "conflict",
            "detail": f"Eval definition references a resource that does not exist: {exc.orig}",
        }
    except ProgrammingError:
        _log.exception("create_eval_definition failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError:
        _log.exception("create_eval_definition failed")
        return {"error": "database_unavailable", "detail": "Database operation failed. Please try again."}
    except Exception:
        _log.exception("create_eval_definition failed")
        return _tool_error("Failed to create eval definition")


@mcp.tool(
    description="Update an eval definition (admin only). Bumps the definition "
    "version and snapshots the pre-edit config, mirroring the REST semantics. "
    "Requires an admin caller; non-admins receive an insufficient_scope error. "
    "NOTE: because None means 'not provided' in the tool signature, nullable "
    "fields (node_id, pass_threshold, suite_id) cannot be cleared to NULL via "
    "this tool - the REST PUT route must be used to unset them.",
)
@_RETRY_DB
async def update_eval_definition(
    eval_id: str,
    node_id: str | None = None,
    name: str | None = None,
    eval_type: str | None = None,
    config_json: dict[str, Any] | None = None,
    failure_behaviour: str | None = None,
    pass_threshold: float | None = None,
    suite_id: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_eval_definition")

        from modulo.api.routes.evals import (
            _MSG_EVAL_DEFINITION_NOT_FOUND,
            _eval_def_to_dict,
            _stamp_eval_definition_version,
            _validate_guardrail_request,
        )

        if eval_type is not None and (err := _assert_eval_type(eval_type)) is not None:
            return err
        if failure_behaviour is not None and (err := _assert_failure_behaviour(failure_behaviour)) is not None:
            return err
        if (err := _assert_pass_threshold(pass_threshold)) is not None:
            return err
        if name is not None and (not name or not name.strip()):
            return {"error": "invalid_name", "detail": "name must be a non-empty string when provided"}
        if name is not None and len(name) > _EVAL_NAME_MAX_LENGTH:
            return {
                "error": "invalid_name",
                "detail": f"name must be at most {_EVAL_NAME_MAX_LENGTH} characters",
            }

        _assert_admin_scope("update")

        org_id = _ctx_org_id_val()

        eid, eid_err = _parse_uuid_param(eval_id, "eval_id")
        if eid_err:
            return eid_err
        nid: uuid.UUID | None = None
        if node_id is not None:
            nid, nid_err = _parse_uuid_param(node_id, "node_id")
            if nid_err:
                return nid_err

        updates: dict[str, Any] = {}
        if node_id is not None:
            updates["node_id"] = nid
        if name is not None:
            updates["name"] = name
        if eval_type is not None:
            updates["eval_type"] = eval_type
        if config_json is not None:
            updates["config_json"] = config_json
        if failure_behaviour is not None:
            updates["failure_behaviour"] = failure_behaviour
        if pass_threshold is not None:
            updates["pass_threshold"] = pass_threshold
        if suite_id is not None:
            updates["suite_id"] = suite_id

        from modulo.db.models.eval_definition import EvalDefinition

        async with _session(org_id) as s:
            eval_def = (
                await s.execute(
                    select(EvalDefinition).where(
                        EvalDefinition.id == eid,
                        EvalDefinition.organisation_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if eval_def is None:
                return {"error": "eval_definition_not_found", "detail": _MSG_EVAL_DEFINITION_NOT_FOUND}

            new_type = updates.get("eval_type", eval_def.eval_type)
            new_behaviour = updates.get("failure_behaviour", eval_def.failure_behaviour)
            new_config = updates.get("config_json", eval_def.config_json)
            try:
                _validate_guardrail_request(
                    eval_type=new_type,
                    failure_behaviour=new_behaviour,
                    config_json=new_config,
                )
            except StarletteHTTPException as exc:
                return {"error": "validation_failed", "detail": str(exc.detail)}

            # FAR-382: snapshot the pre-edit config, then bump the version so a
            # rubric/config change is an explicitly version-scoped event.
            _stamp_eval_definition_version(eval_def)
            for key, value in updates.items():
                setattr(eval_def, key, value)
            await s.flush()
            return _eval_def_to_dict(eval_def)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except StarletteHTTPException as exc:
        return {"error": "validation_failed", "detail": str(exc.detail)}
    except IntegrityError as exc:
        _log.exception("update_eval_definition failed")
        return {
            "error": "conflict",
            "detail": f"Update would violate a constraint. Check referenced pipeline/suite: {exc.orig}",
        }
    except ProgrammingError:
        _log.exception("update_eval_definition failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError:
        _log.exception("update_eval_definition failed")
        return {"error": "database_unavailable", "detail": "Database operation failed. Please try again."}
    except Exception:
        _log.exception("update_eval_definition failed")
        return _tool_error("Failed to update eval definition")


@mcp.tool(
    description="Delete an eval definition (admin only). Guardrail eval "
    "definitions are SOFT-deleted (deleted_at stamped) by default; a second, "
    "admin-only hard purge (hard=True) removes the row outright. Non-admins "
    "receive an insufficient_scope error.",
)
@_RETRY_DB
async def delete_eval_definition(
    eval_id: str,
    hard: bool = False,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_eval_definition")

        from modulo.api.routes.evals import _MSG_EVAL_DEFINITION_NOT_FOUND
        from modulo.core.audit_logger import append_audit_event

        _assert_admin_scope("delete")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        eid, eid_err = _parse_uuid_param(eval_id, "eval_id")
        if eid_err:
            return eid_err

        from modulo.db.models.eval_definition import EvalDefinition

        async with _session(org_id) as s:
            eval_def = (
                await s.execute(
                    select(EvalDefinition).where(
                        EvalDefinition.id == eid,
                        EvalDefinition.organisation_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if eval_def is None:
                return {"error": "eval_definition_not_found", "detail": _MSG_EVAL_DEFINITION_NOT_FOUND}

            is_guardrail = eval_def.eval_type == "guardrail"
            soft = is_guardrail and not hard
            eval_name = eval_def.name
            if soft:
                eval_def.deleted_at = datetime.now(UTC)
                eval_def.deleted_by = account_id
            else:
                await s.delete(eval_def)
            if is_guardrail:
                try:
                    await append_audit_event(
                        s,
                        org_id=org_id,
                        event_type="eval_definition.soft_deleted" if soft else "eval_definition.purged",
                        actor_user_id=account_id,
                        resource_type="eval_definition",
                        resource_id=eid,
                        payload_json={"eval_id": str(eid), "name": eval_name, "purge": hard},
                    )
                except Exception:
                    _log.exception(
                        "delete_eval_definition_audit_failed",
                        extra={"org_id": str(org_id), "eval_id": str(eid)},
                    )
        return {"id": str(eid), "soft_deleted": soft, "hard_deleted": not soft}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError as exc:
        _log.exception("delete_eval_definition failed")
        return {
            "error": "conflict",
            "detail": f"Delete would violate a constraint: {exc.orig}",
        }
    except ProgrammingError:
        _log.exception("delete_eval_definition failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError:
        _log.exception("delete_eval_definition failed")
        return {"error": "database_unavailable", "detail": "Database operation failed. Please try again."}
    except Exception:
        _log.exception("delete_eval_definition failed")
        return _tool_error("Failed to delete eval definition")


@mcp.tool(description="Cancel a running pipeline run.")
@_RETRY_DB
async def cancel_run(run_id: str) -> dict[str, Any]:
    try:
        return await _cancel_run_impl(run_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("cancel_run failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("cancel_run failed")
        return _tool_error("Failed to cancel run")


async def _cancel_run_impl(run_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("cancel_run")
    from modulo.db.crud.run import get_run, request_cancellation

    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        run = await get_run(s, rid)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
            return _team_scope_error("run", run_id)
        if run.status in TERMINAL_STATUSES:
            detail = f"Run is already in terminal status: {run.status}"
            return {"error": "cannot_cancel", "run_id": str(run_id), "detail": detail}
        # PAUSED-then-cancelled class (awaiting_human/claimed) runs NO
        # finalize (§4.2). A STREAMED running run cancelled cross-process is
        # routed through finalize_cost, re-reading the STORED cumulative
        # sets; a NEVER-PAUSED in-flight run has none and forfeits its
        # accrued cost (cost_components_partial_spend_lost log).
        was_paused = run.status in ("awaiting_human", "claimed")
        run = await request_cancellation(s, rid)
        if not was_paused:
            await finalize_cancelled_run(s, run_id=rid, org_id=org_id)
    if run is None:
        return {"error": "run_not_found", "run_id": run_id}
    return {"run_id": run_id, "cancellation_requested": True}


@mcp.tool(description="List all pending (undecided) HITL gates across all runs.")
@_RETRY_DB
async def list_pending_hitl(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    try:
        return await _list_pending_hitl_impl(page, page_size)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_pending_hitl failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_pending_hitl failed")
        return _tool_error("Failed to list pending HITL gates")


async def _list_pending_hitl_impl(page: int, page_size: int) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("list_pending_hitl")
    from sqlalchemy import func

    from modulo.db.models.pipeline import Pipeline

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        base_where: list[Any] = [
            HitlClaim.organisation_id == org_id,
            HitlClaim.decision.is_(None),
            Run.status.not_in(TERMINAL_STATUSES),
        ]
        key_team_id = _ctx_team_id_val()
        if key_team_id is not None:
            # A team-scoped key only sees pending gates for runs owned by its
            # own team (or org-level runs with no owner team) — the same
            # boundary the run tools enforce. The run's owner is the source
            # of truth; runs predating the create-time stamp (NULL) fall
            # back to the pipeline's owner so a NULL stamp can never widen
            # the boundary.
            from modulo.db.crud.team_scope import team_scope_clause

            effective_owner = func.coalesce(Run.owner_team_id, Pipeline.owner_team_id)
            base_where.append(team_scope_clause(effective_owner, key_team_id))
        gates, total = await _load_pending_hitl_gates(s, base_where, page, page_size)
    return {
        "gates": [
            {
                "run_id": str(g.run_id),
                "gate_id": g.gate_id,
                "pipeline_id": str(g.pipeline_id),
                "claimed_by": str(g.account_id) if g.account_id else None,
                "expires_at": _iso_or_none(g.expires_at),
                "required_team_id": str(g.required_team_id) if g.required_team_id else None,
            }
            for g in gates
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": (page * page_size) < total,
    }


async def _load_pending_hitl_gates(
    s: AsyncSession, base_where: list[Any], page: int, page_size: int
) -> tuple[list[HitlClaim], int]:
    """Load a page of pending HITL gates plus the total count (one transaction)."""
    from sqlalchemy import func, select

    from modulo.db.models.pipeline import Pipeline

    total_result = await s.execute(
        select(func.count())
        .select_from(HitlClaim)
        .join(Run, HitlClaim.run_id == Run.id)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(*base_where)
    )
    total = total_result.scalar_one()

    offset = (page - 1) * page_size
    result = await s.execute(
        select(HitlClaim)
        .join(Run, HitlClaim.run_id == Run.id)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(*base_where)
        .offset(offset)
        .limit(page_size)
    )
    return list(result.scalars()), total


_TEAM_SCOPE_ERROR = object()


def _parse_hitl_action(
    run_id: str,
    action: str,
    claim_token: str | None,
    output: dict[str, Any] | None,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse and validate the HITL action request.

    Returns ``(rid, None)`` on success, or ``(None, error_dict)`` on the first
    validation failure.
    """
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return None, rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    if action not in ("claim", "approve", "reject", "deliver_manual"):
        return None, {"error": "invalid_action", "detail": "action must be claim, approve, reject, or deliver_manual"}

    if action == "approve" and claim_token is None:
        return None, {"error": "claim_token_required", "detail": "approve requires claim_token"}
    if action == "reject" and claim_token is None:
        return None, {"error": "claim_token_required", "detail": "reject requires claim_token"}
    if action == "deliver_manual" and claim_token is None:
        return None, {"error": "claim_token_required", "detail": "deliver_manual requires claim_token"}
    if action == "deliver_manual" and output is None:
        return None, {"error": "output_required", "detail": "deliver_manual requires output dict"}

    return rid, None


async def _load_hitl_run(
    s: AsyncSession,
    rid: uuid.UUID,
) -> Any | None:
    """Load the HITL gate's run, enforcing the team boundary.

    Returns the run ORM object on success, ``_TEAM_SCOPE_ERROR`` when a
    team-scoped key must not act on the run, or ``None`` when the run is not
    found.
    """
    run = await get_run(s, rid)
    if run is None:
        return None
    if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
        return _TEAM_SCOPE_ERROR
    return run


async def _check_human_only_gate(
    s: AsyncSession,
    org_id: uuid.UUID,
    rid: uuid.UUID,
    gate_id: str,
) -> dict[str, Any] | None:
    """Return an error dict when the gate is human_only, else ``None``.

    Only called for the ``approve`` action: a ``human_only`` gate can only be
    approved by a browser-authenticated human, never by an API key.
    """
    from sqlalchemy import select

    gate_row = (
        await s.execute(
            select(HitlClaim).where(
                HitlClaim.run_id == rid,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if gate_row is not None:
        edge = (
            (
                await s.execute(
                    select(PipelineEdge).where(
                        PipelineEdge.pipeline_id == gate_row.pipeline_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if edge and edge.hitl_gate_config and edge.hitl_gate_config.get("human_only", False):
            return {"error": "human_only_gate", "detail": "human_only gate requires browser auth"}
    return None


async def _dispatch_hitl_action(
    mgr: HITLManager,
    s: AsyncSession,
    action: str,
    rid: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    claim_token: str | None,
    output: dict[str, Any] | None,
    reason: str | None,
) -> dict[str, Any]:
    """Dispatch a validated HITL action to the manager and return the success dict.

    Raises the domain exceptions (GateNotFoundError, NotTeamMemberError, etc.)
    which the caller maps to error responses.
    """
    if action == "claim":
        gate = await mgr.claim(s, run_id=rid, gate_id=gate_id, org_id=org_id, claimant_id=key_id)
        return {
            "status": "claimed",
            "claim_token": gate.claim_token,
            "expires_at": gate.expires_at.isoformat() if gate.expires_at else None,
        }
    if action == "approve":
        await mgr.approve(s, run_id=rid, gate_id=gate_id, org_id=org_id, claim_token=claim_token or "")
        return {"status": "approved", "gate_id": gate_id}
    if action == "deliver_manual":
        await mgr.deliver_manual(
            s,
            run_id=rid,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token or "",
            output=output or {},
            actor_id=key_id,
        )
        return {"status": "delivered_manual", "gate_id": gate_id}
    await mgr.reject(
        s,
        run_id=rid,
        gate_id=gate_id,
        org_id=org_id,
        claim_token=claim_token or "",
        actor_id=key_id,
        reason=reason,
    )
    return {"status": "rejected", "gate_id": gate_id}


def _hitl_error_response(exc: BaseException, run_id: str, gate_id: str) -> dict[str, Any]:
    if isinstance(exc, GateNotFoundError):
        return {"error": "gate_not_found", "run_id": run_id, "gate_id": gate_id}
    if isinstance(exc, NotTeamMemberError):
        return {"error": "not_team_member", "detail": "You are not a member of the team required by this gate"}
    if isinstance(exc, AlreadyClaimedError):
        return {"error": "already_claimed", "detail": "Gate is already held by another client"}
    if isinstance(exc, ClaimTokenInvalidError):
        return {"error": "claim_token_invalid"}
    if isinstance(exc, ClaimTokenExpiredError):
        return {"error": "claim_token_expired", "detail": "Re-claim the gate"}
    if isinstance(exc, GateAlreadyDecidedError):
        return {"error": "already_decided", "detail": "Gate already has a final decision"}
    if isinstance(exc, ProgrammingError):
        _log.exception("review_hitl failed")
        return {"error": "migration_required", "detail": "DB migration required. Run alembic upgrade head."}
    _log.exception("review_hitl failed")
    return _tool_error("Failed to process HITL action")


async def _review_hitl_impl(
    run_id: str,
    gate_id: str,
    action: str,
    claim_token: str | None,
    reason: str | None,
    output: dict[str, Any] | None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)

    org_id = _ctx_org_id_val()
    key_id = _ctx_key_id.get(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    mgr = HITLManager()

    rid, parse_err = _parse_hitl_action(run_id, action, claim_token, output)
    if parse_err:
        return parse_err
    if rid is None:
        raise RuntimeError("_review_hitl_impl: parse returned no error and no run id")

    try:
        _check_agent_tool_scope("review_hitl", action=action)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}

    async with _session(org_id) as s:
        run = await _load_hitl_run(s, rid)
        if run is _TEAM_SCOPE_ERROR:
            return _team_scope_error("run", run_id)
        if run is None:
            return {"error": "gate_not_found", "run_id": run_id, "gate_id": gate_id}

        if action == "approve":
            human_only_err = await _check_human_only_gate(s, org_id, rid, gate_id)
            if human_only_err:
                return human_only_err

        try:
            return await _dispatch_hitl_action(
                mgr, s, action, rid, gate_id, org_id, key_id, claim_token, output, reason
            )
        except (
            GateNotFoundError,
            NotTeamMemberError,
            AlreadyClaimedError,
            ClaimTokenInvalidError,
            ClaimTokenExpiredError,
            GateAlreadyDecidedError,
            ProgrammingError,
        ) as exc:
            return _hitl_error_response(exc, run_id, gate_id)
        except Exception as exc:
            return _hitl_error_response(exc, run_id, gate_id)


@mcp.tool(
    description=(
        "Unified HITL gate action: claim, approve, reject, or deliver_manual. "
        "Step 1: call with action='claim' to get a claim_token. "
        "Step 2: call with action='approve', 'reject', or 'deliver_manual' + your claim_token. "
        "'deliver_manual' requires 'output' (a dict) to supply the output directly. "
        "human_only gates return 403 on approve — only a browser-authenticated human can approve."
    ),
)
@_RETRY_DB
async def review_hitl(
    run_id: str,
    gate_id: str,
    action: str,
    claim_token: str | None = None,
    reason: str | None = None,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return await _review_hitl_impl(run_id, gate_id, action, claim_token, reason, output)
    except OperationalError:
        raise
    except Exception:
        _log.exception("review_hitl operation failed")
        return _tool_error("Failed to process HITL action")


@mcp.tool(
    description=(
        "Copy a library primitive to the org workspace. "
        "Community primitives can be copied — this creates an editable copy in your workspace. "
        "Note: community primitives are maintained by the Modulo team; your copy diverges from upstream on first edit."
    ),
)
@_RETRY_DB
async def copy_library_primitive(
    primitive_id: str,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    try:
        _check_agent_tool_scope("copy_library_primitive")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}

    org_id = _ctx_org_id_val()
    pid, pid_err = _parse_uuid_param(primitive_id, "primitive_id")
    if pid_err:
        return pid_err
    assert pid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    async with _session(org_id) as s:
        try:
            result = await library_copy_to_adapt(s, org_id, pid, via_mcp=False)
        except LookupError:
            return {"error": "not_found", "primitive_id": primitive_id}
        except ProgrammingError:
            _log.exception("copy_library_primitive failed")
            return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
        except Exception:
            _log.exception("copy_library_primitive failed")
            return _tool_error("Failed to copy library primitive")

    return {
        "status": "copied",
        "primitive_id": str(result.id),
        "name": result.name,
        "slug": result.slug,
    }


@mcp.tool(
    name="search_library",
    description=(
        "Search the library of primitives (schemas, agents, workflows, "
        "pipeline templates, test fixtures). Supports filtering by type, "
        "text search, and cursor-based pagination. "
        "For text output, see the modulo://library resource."
    ),
)
@_RETRY_DB
async def search_library(
    primitive_type: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            result = await list_primitives(
                s,
                org_id,
                primitive_type=primitive_type,
                search=search,
                page=1,
                page_size=limit,
                include_community=True,
                cursor=cursor,
            )
        return {
            "items": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "description": p.description,
                    "type": p.primitive_type,
                    "version": p.version,
                    "average_rating": p.average_rating,
                    "tags": list(p.tags) if p.tags else [],
                }
                for p in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except ProgrammingError:
        _log.exception("search_library failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("search_library failed")
        return _tool_error("Failed to search library")


async def _apply_trigger_event_trigger_filter(
    s: AsyncSession,
    q: Any,
    key_team_id: uuid.UUID | None,
    trigger_id: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    from sqlalchemy import select

    from modulo.db.models.trigger import Trigger
    from modulo.db.models.trigger_event import TriggerEvent

    if trigger_id is None:
        return q, None
    try:
        tid = uuid.UUID(trigger_id)
    except ValueError:
        return q, {
            "error": "invalid_id",
            "field": "trigger_id",
            "detail": f"Invalid UUID format: {trigger_id}",
        }
    q = q.where(TriggerEvent.trigger_id == tid)
    if key_team_id is not None:
        # A team-scoped key must not read events for another
        # team's trigger even when no pipeline filter is given.
        # Fail closed: a soft-deleted or otherwise-unresolvable
        # trigger is treated as out of the key's team boundary too
        # (matching ``list_triggers``, which filters
        # ``Trigger.deleted_at.is_(None)``), so a deleted cross-team
        # trigger cannot fall through to an unfiltered listing.
        trigger = (
            await s.execute(
                select(Trigger).where(
                    Trigger.id == tid,
                    Trigger.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if trigger is None:
            return q, _team_scope_error("trigger", str(tid))
        if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, trigger.pipeline_id)):
            return q, _team_scope_error("pipeline", str(trigger.pipeline_id))
    return q, None


async def _build_trigger_event_query(
    s: AsyncSession,
    org_id: uuid.UUID,
    key_team_id: uuid.UUID | None,
    trigger_id: str | None,
    pipeline_id: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    from sqlalchemy import select

    from modulo.db.models.pipeline import Pipeline as _Pipeline
    from modulo.db.models.trigger import Trigger
    from modulo.db.models.trigger_event import TriggerEvent

    q = select(TriggerEvent).where(TriggerEvent.organisation_id == org_id)
    joined = False

    q, trigger_filter_err = await _apply_trigger_event_trigger_filter(s, q, key_team_id, trigger_id)
    if trigger_filter_err:
        return None, trigger_filter_err

    if pipeline_id is not None:
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return None, {
                "error": "invalid_id",
                "field": "pipeline_id",
                "detail": f"Invalid UUID format: {pipeline_id}",
            }
        if key_team_id is not None:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return None, _team_scope_error("pipeline", str(pid))
        if not joined:
            q = q.join(Trigger, TriggerEvent.trigger_id == Trigger.id)
            q = q.join(_Pipeline, Trigger.pipeline_id == _Pipeline.id)
            joined = True
        q = q.where(
            Trigger.pipeline_id == pid,
            _Pipeline.deleted_at.is_(None),
        )

    if key_team_id is not None:
        # A team-scoped key only sees events whose trigger's pipeline
        # is org-level or owned by its own team — the same boundary
        # ``list_triggers`` applies.
        from modulo.db.crud.team_scope import team_scope_clause

        if not joined:
            q = q.join(Trigger, TriggerEvent.trigger_id == Trigger.id)
            q = q.join(_Pipeline, Trigger.pipeline_id == _Pipeline.id)
            joined = True
        q = q.where(team_scope_clause(_Pipeline.owner_team_id, key_team_id))

    return q, None


async def _paginate_trigger_events(
    s: AsyncSession,
    q: Any,
    cursor: str | None,
    lim: int,
) -> tuple[list[Any], str | None, bool]:
    from modulo.db.crud.pagination import CursorPaginator
    from modulo.db.models.trigger_event import TriggerEvent

    if cursor is not None:
        paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
        cp = await paginator.paginate(
            s,
            q,
            cursor=cursor,
            limit=lim,
            model=TriggerEvent,
            compute_total=False,
        )
        items = cp.items
        next_cursor = cp.next_cursor
        has_more = cp.has_more
    else:
        q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc())
        rows = list((await s.execute(q.limit(lim + 1))).scalars().all())
        has_more = len(rows) > lim
        items = rows[:lim]
        next_cursor = None
        if has_more:
            last = items[-1]
            next_cursor = CursorPaginator.encode_cursor(last.created_at, last.id)
    return items, next_cursor, has_more


def _trigger_event_payloads(items: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(e.id),
            "trigger_id": str(e.trigger_id),
            "trigger_type": e.trigger_type,
            "validation_result": e.validation_result,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "run_id": str(e.run_id) if e.run_id else None,
        }
        for e in items
    ]


async def _list_trigger_events_impl(
    trigger_id: str | None,
    pipeline_id: str | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("list_trigger_events")
    from sqlalchemy import func, select

    org_id = _ctx_org_id_val()
    lim = max(1, min(limit, 100))

    async with _session(org_id) as s:
        key_team_id = _ctx_team_id_val()
        q, q_err = await _build_trigger_event_query(s, org_id, key_team_id, trigger_id, pipeline_id)
        if q_err:
            return q_err
        total = int((await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one_or_none() or 0)
        items, next_cursor, has_more = await _paginate_trigger_events(s, q, cursor, lim)

    return {
        "data": _trigger_event_payloads(items),
        "total": total,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@mcp.tool(
    name="list_trigger_events",
    description=(
        "List recent trigger events with cursor-based pagination. "
        "Filter by trigger_id and/or pipeline_id. Returns events ordered "
        "by most recent first."
    ),
)
@_RETRY_DB
async def list_trigger_events(
    trigger_id: str | None = None,
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        return await _list_trigger_events_impl(trigger_id, pipeline_id, cursor, limit)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_trigger_events failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_trigger_events failed")
        return _tool_error("Failed to list trigger events")


@mcp.tool(
    description="List triggers configured for the organisation with cursor-based pagination. "
    "Optionally filter by pipeline_id. Returns trigger metadata "
    "including type, active status, and cron schedule.",
)
@_RETRY_DB
async def list_triggers(
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_triggers")
        from modulo.db.crud.trigger import list_triggers as db_list_triggers

        org_id = _ctx_org_id_val()
        pid = uuid.UUID(pipeline_id) if pipeline_id else None
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            if pid is not None:
                owner_team_id = await _pipeline_owner_team_id(s, pid)
                if _team_scoped_key_mismatch(owner_team_id):
                    return _team_scope_error("pipeline", str(pid))
            result = await db_list_triggers(
                s,
                org_id,
                pipeline_id=pid,
                cursor=cursor,
                limit=lim,
                team_id=_ctx_team_id_val(),
            )
            # FAR-251 — surface the SAME streak_status shape as the REST
            # trigger serializers (via the shared routes helper), computed
            # INSIDE the RLS transaction so a deactivated trigger's reason /
            # streak reads land in-org (mirrors the FAR-191 list fix).
            data = [
                {
                    "id": str(t.id),
                    "pipeline_id": str(t.pipeline_id),
                    "trigger_type": t.trigger_type,
                    "active": t.active,
                    "max_concurrent_runs": t.max_concurrent_runs,
                    "cron_expression": t.cron_expression,
                    "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "streak_status": await _streak_status_for(s, t),
                }
                for t in result.items
            ]

        return {
            "data": data,
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_triggers failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_triggers failed")
        return _tool_error("Failed to list triggers")


@mcp.tool(
    description="Create a new model backend (provider configuration). "
    "The API key is NOT sent through this tool — instead, a one-time setup URL is returned. "
    "Open the URL in your browser to provide the API key directly. "
    "This keeps the secret out of the LLM context and MCP transport logs. "
    "Common providers include: openai, anthropic, gemini, deepseek, groq, opencode.",
)
@_RETRY_DB
async def create_model_backend(
    name: str,
    display_name: str,
    provider: str,
    model_id: str,
    default_params: dict[str, Any] | None = None,
    visibility: str = "org",
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_model_backend")

        from modulo.core.mcp_setup_handoff import create_handoff

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            mb = await db_create_model_backend(
                s,
                org_id=org_id,
                name=name,
                display_name=display_name,
                provider=provider,
                model_id=model_id,
                credentials_ciphertext=b"",
                account_id=account_id,
                default_params=default_params or {},
                visibility=visibility,
                fallback_backend_ids=None,
            )
            handoff = await create_handoff(
                s,
                org_id=org_id,
                resource_type="model-backend",
                resource_id=mb.id,
                created_by=account_id,
            )

        return {
            "id": str(mb.id),
            "name": mb.name,
            "display_name": mb.display_name,
            "provider": mb.provider,
            "model_id": mb.model_id,
            "status": "pending_setup",
            "visibility": mb.visibility,
            **handoff,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_model_backend failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("create_model_backend failed")
        return _tool_error("Failed to create model backend")


@mcp.tool(
    description="Create a new connector instance (provider configuration). "
    "Credentials are encrypted at rest. Returns the created connector details."
)
@_RETRY_DB
async def create_connector(
    name: str,
    connector_type_id: str,
    credentials: str,
    config_json: dict[str, Any] | None = None,
    allowed_operations: list[str] | None = None,
    visibility: str = "org",
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_connector")

        from cryptography.fernet import Fernet

        from modulo.db.crud.connector_instance import create_connector_instance

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        settings = get_settings()
        credentials_ciphertext = Fernet(settings.fernet_key.encode()).encrypt(credentials.encode())

        async with _session(org_id) as s:
            ci = await create_connector_instance(
                s,
                org_id=org_id,
                name=name,
                connector_type_id=connector_type_id,
                account_id=account_id,
                credentials_ciphertext=credentials_ciphertext,
                config_json=config_json or {},
                allowed_operations=allowed_operations or [],
                visibility=visibility,
            )

        return {
            "id": str(ci.id),
            "name": ci.name,
            "connector_type_id": ci.connector_type_id,
            "visibility": ci.visibility,
            "status": "created",
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_connector failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("create_connector failed")
        return _tool_error("Failed to create connector")


def _validate_trigger_create_inputs(
    pipeline_id: str,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return None, {
            "error": "invalid_id",
            "field": "pipeline_id",
            "detail": f"Invalid UUID format: {pipeline_id}",
        }
    num_err = _validate_trigger_numbers(max_concurrent_runs, daily_spend_limit)
    if num_err:
        return None, num_err
    return pid, None


async def _validate_ongoing_trigger_create(
    s: AsyncSession,
    pid: uuid.UUID,
    trigger_type: str,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
) -> tuple[datetime | None, dict[str, Any] | None]:
    if trigger_type != "ongoing":
        return None, None
    # FAR-158: identical guards to the REST create surface.
    from fastapi import HTTPException

    from modulo.core.trigger_validation import validate_ongoing_config
    from modulo.db.models.pipeline import Pipeline

    pipeline = await s.get(Pipeline, pid)
    pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
    try:
        validate_ongoing_config(
            trigger_type,
            max_concurrent_runs=max_concurrent_runs,
            daily_spend_limit=Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else None,
            config_json=config_json,
            pipeline_max_concurrent_runs=pipeline_cap,
        )
    except HTTPException as exc:
        return None, {"error": "validation", "detail": str(exc.detail)}
    return datetime.now(UTC), None


def _build_trigger_record(
    org_id: uuid.UUID,
    pid: uuid.UUID,
    trigger_type: str,
    active: bool,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
    account_id: uuid.UUID,
    next_fire_at: datetime | None,
    cron_expression: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    from modulo.db.models.trigger import Trigger

    trigger = Trigger(
        organisation_id=org_id,
        pipeline_id=pid,
        trigger_type=trigger_type,
        active=active,
        max_concurrent_runs=max_concurrent_runs,
        daily_spend_limit=Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else None,
        config_json=config_json or {},
        account_id=account_id,
        next_fire_at=next_fire_at,
        # FAR-190: creation anchors the no-delivery streak epoch (the
        # streak boundary) so pre-existing history can never count.
        streak_epoch=datetime.now(UTC),
    )
    if cron_expression:
        trigger.cron_expression = cron_expression
        error = validate_cron_expression(cron_expression)
        if error:
            return trigger, {"error": "invalid_cron", "detail": error}
        trigger.next_fire_at = compute_next_fire(cron_expression, timezone=trigger.cron_timezone or "UTC")
    return trigger, None


async def _create_trigger_impl(
    pipeline_id: str,
    trigger_type: str,
    active: bool,
    cron_expression: str | None,
    config_json: dict[str, Any] | None,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("create_trigger")

    org_id = _ctx_org_id_val()
    account_id = _ctx_user_id_val()
    pid, input_err = _validate_trigger_create_inputs(pipeline_id, max_concurrent_runs, daily_spend_limit)
    if input_err:
        return input_err
    if pid is None:
        raise RuntimeError("_create_trigger_impl: validate returned no error and no pipeline id")

    async with _session(org_id) as s:
        owner_team_id = await _pipeline_owner_team_id(s, pid)
        if _team_scoped_key_mismatch(owner_team_id):
            return _team_scope_error("pipeline", pipeline_id)
        next_fire_at, ongoing_err = await _validate_ongoing_trigger_create(
            s, pid, trigger_type, max_concurrent_runs, daily_spend_limit, config_json
        )
        if ongoing_err:
            return ongoing_err
        trigger, build_err = _build_trigger_record(
            org_id,
            pid,
            trigger_type,
            active,
            max_concurrent_runs,
            daily_spend_limit,
            config_json,
            account_id,
            next_fire_at,
            cron_expression,
        )
        if build_err:
            return build_err
        s.add(trigger)
        await s.flush()
        # FAR-251 — surface the created trigger's streak_status exactly as
        # the REST create serializer does (computed inside the RLS
        # transaction; for a fresh ongoing trigger this reads the anchored
        # streak=0 / state=ok baseline).
        created_streak_status = await _streak_status_for(s, trigger)

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": float(trigger.daily_spend_limit) if trigger.daily_spend_limit is not None else None,
        "cron_expression": trigger.cron_expression,
        "streak_status": created_streak_status,
    }


@mcp.tool(description="Create a new trigger for a pipeline.")
@_RETRY_DB
async def create_trigger(
    pipeline_id: str,
    trigger_type: str = "manual",
    active: bool = True,
    cron_expression: str | None = None,
    config_json: dict[str, Any] | None = None,
    max_concurrent_runs: int = 1,
    daily_spend_limit: float | None = None,
) -> dict[str, Any]:
    try:
        return await _create_trigger_impl(
            pipeline_id,
            trigger_type,
            active,
            cron_expression,
            config_json,
            max_concurrent_runs,
            daily_spend_limit,
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("create_trigger failed")
        return _tool_error("Failed to create trigger")


def _trigger_detail_dict(trigger: Any, in_flight: int, streak_status: Any) -> dict[str, Any]:
    """Serialize a trigger row to the MCP response shape."""
    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": float(trigger.daily_spend_limit) if trigger.daily_spend_limit is not None else None,
        "config_json": trigger.config_json or {},
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "input_template": (trigger.config_json or {}).get("input_template"),
        "in_flight": in_flight,
        "streak_status": streak_status,
    }


@mcp.tool(description="Get a single trigger by ID.")
@_RETRY_DB
async def get_trigger(trigger_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_trigger")

        org_id = _ctx_org_id_val()
        tid, tid_err = _parse_uuid_param(trigger_id, "trigger_id")
        if tid_err:
            return tid_err
        if tid is None:
            return {"error": "invalid_id", "field": "trigger_id", "detail": _MSG_UUID_PARSE_FAILED}

        from modulo.core.cron_helpers import _count_ongoing_runs

        async with _session(org_id) as s:
            trigger = await _load_trigger_row(s, org_id, tid)
            if trigger is not None:
                owner_team_id = await _pipeline_owner_team_id(s, trigger.pipeline_id)
                if _team_scoped_key_mismatch(owner_team_id):
                    return _team_scope_error("pipeline", str(trigger.pipeline_id))
            in_flight = (
                await _count_ongoing_runs(s, tid) if trigger is not None and trigger.trigger_type == "ongoing" else 0
            )
            # FAR-251 — surface the SAME streak_status shape as the REST
            # trigger detail serializer (shared ``_streak_status_for``), computed
            # INSIDE the RLS transaction (mirrors the FAR-191 fix — never read
            # streak status post-commit).
            streak_status = await _streak_status_for(s, trigger) if trigger is not None else None

        if trigger is None:
            return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}

        return _trigger_detail_dict(trigger, in_flight, streak_status)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_trigger failed")
        return _tool_error("Failed to get trigger")


def _validate_trigger_update_inputs(
    trigger_id: str,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse the trigger UUID and validate the numeric update inputs."""
    tid, tid_err = _parse_uuid_param(trigger_id, "trigger_id")
    if tid_err:
        return None, tid_err
    assert tid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    num_err = _validate_trigger_numbers(max_concurrent_runs, daily_spend_limit)
    if num_err:
        return None, num_err
    return tid, None


async def _load_trigger_for_update(s: AsyncSession, org_id: uuid.UUID, tid: uuid.UUID) -> Any | None:
    """Load the trigger row for update; None if not found, _TEAM_SCOPE_ERROR if team-scope mismatch."""
    trigger = await _load_trigger_row(s, org_id, tid)
    if trigger is None:
        return None
    if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, trigger.pipeline_id)):
        return _TEAM_SCOPE_ERROR
    return trigger


async def _validate_ongoing_config_change(
    s: AsyncSession,
    trigger: Any,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
    active: bool | None,
) -> dict[str, Any] | None:
    """Validate an ongoing trigger's config change; returns None if valid or an error dict."""
    from fastapi import HTTPException

    from modulo.core.trigger_validation import validate_ongoing_config
    from modulo.db.models.pipeline import Pipeline

    ongoing_fields_changing = any(x is not None for x in [max_concurrent_runs, config_json, active]) or (
        daily_spend_limit is not None
    )
    if not ongoing_fields_changing:
        return None
    pipeline = await s.get(Pipeline, trigger.pipeline_id)
    pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
    resolved_daily_spend_limit = (
        Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else trigger.daily_spend_limit
    )
    try:
        validate_ongoing_config(
            trigger.trigger_type,
            max_concurrent_runs=(
                max_concurrent_runs if max_concurrent_runs is not None else trigger.max_concurrent_runs
            ),
            daily_spend_limit=resolved_daily_spend_limit,
            config_json=(config_json if config_json is not None else trigger.config_json),
            pipeline_max_concurrent_runs=pipeline_cap,
        )
    except HTTPException as exc:
        return {"error": "validation", "detail": str(exc.detail)}
    return None


async def _validate_ongoing_trigger_update(
    s: AsyncSession,
    trigger: Any,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
    active: bool | None,
    clear_daily_spend_limit: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """FAR-158 ongoing guards (identical to REST PUT); returns (scan_interval_changed, error)."""
    ongoing_scan_interval_changed = False
    if trigger.trigger_type == "ongoing":
        if clear_daily_spend_limit:
            return False, {
                "error": "validation",
                "detail": "ongoing triggers require daily_spend_limit; clearing it is not allowed",
            }
        cfg_err = await _validate_ongoing_config_change(
            s, trigger, max_concurrent_runs, daily_spend_limit, config_json, active
        )
        if cfg_err:
            return False, cfg_err
        if config_json is not None:
            old_scan = int((trigger.config_json or {}).get("scan_interval_seconds") or 60)
            new_scan = int(config_json.get("scan_interval_seconds") or 60)
            ongoing_scan_interval_changed = new_scan != old_scan
    return ongoing_scan_interval_changed, None


def _validate_cron_update(
    trigger: Any,
    cron_expression: str | None,
    cron_timezone: str | None,
) -> tuple[datetime | None, dict[str, Any] | None]:
    """Validate cron config; returns (next_fire_at, error)."""
    next_fire_at = None
    if cron_expression is not None or cron_timezone is not None:
        expr = cron_expression if cron_expression is not None else trigger.cron_expression
        if expr is None:
            return None, {"error": "invalid_cron", "detail": "Cron expression is required"}
        tz = cron_timezone if cron_timezone is not None else trigger.cron_timezone or "UTC"
        error = validate_cron_expression(expr, tz)
        if error:
            return None, {"error": "invalid_cron", "detail": error}
        next_fire_at = compute_next_fire(expr, timezone=tz)
    return next_fire_at, None


def _merge_trigger_config_json(trigger: Any, config_json: dict[str, Any] | None) -> None:
    if config_json is None:
        return
    # MERGE into the existing blob — never wholesale replace.
    current_cfg = trigger.config_json or {}
    merged_cfg = dict(current_cfg)
    for k, v in config_json.items():
        if isinstance(v, str) and v == SENSITIVE_VALUE_MASK:
            # A masked placeholder must never clobber the stored secret
            # (read-modify-write round-trip guard). Keep the existing value.
            continue
        if v is None:
            # Explicit null clears the key; a missing key leaves it intact.
            merged_cfg.pop(k, None)
        else:
            merged_cfg[k] = v
    trigger.config_json = merged_cfg


async def _apply_trigger_field_updates(
    s: AsyncSession,
    trigger: Any,
    active: bool | None,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
    clear_daily_spend_limit: bool,
    config_json: dict[str, Any] | None,
    cron_expression: str | None,
    cron_timezone: str | None,
    next_fire_at: datetime | None,
    prev_active: bool | None,
) -> None:
    """Apply the field updates to the trigger row in place."""
    if active is not None:
        trigger.active = active
        # FAR-190: re-anchor the no-delivery streak epoch on any
        # active=True transition (no un-epoch'd re-enable path).
        if trigger.active and not prev_active:
            await anchor_trigger_streak_epoch(s, trigger_id=trigger.id)
    if max_concurrent_runs is not None:
        trigger.max_concurrent_runs = max_concurrent_runs
    if clear_daily_spend_limit:
        trigger.daily_spend_limit = None
    elif daily_spend_limit is not None:
        trigger.daily_spend_limit = Decimal(str(daily_spend_limit))
    _merge_trigger_config_json(trigger, config_json)
    if cron_expression is not None:
        trigger.cron_expression = cron_expression
    if cron_timezone is not None:
        trigger.cron_timezone = cron_timezone
    if next_fire_at is not None:
        trigger.next_fire_at = next_fire_at


def _recompute_ongoing_next_fire(
    trigger: Any,
    max_concurrent_runs: int | None,
    active: bool | None,
    prev_max: int | None,
    prev_active: bool | None,
    ongoing_scan_interval_changed: bool,
) -> None:
    # Ongoing triggers recompute next_fire_at when the pool / cadence /
    # active actually changes so the new config takes effect promptly.
    if trigger.trigger_type == "ongoing":
        from datetime import UTC

        target_changed = max_concurrent_runs is not None and max_concurrent_runs != prev_max
        activated = active is not None and trigger.active and not prev_active
        if target_changed or ongoing_scan_interval_changed or activated:
            trigger.next_fire_at = datetime.now(UTC)


@mcp.tool(
    description="Update an existing trigger's configuration. "
    "Mirrors PUT /api/v1/triggers/{id}. Setting cron_expression or "
    "cron_timezone is only valid for cron triggers.",
)
@_RETRY_DB
async def update_trigger(
    trigger_id: str,
    active: bool | None = None,
    max_concurrent_runs: int | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    daily_spend_limit: float | None = None,
    clear_daily_spend_limit: bool = False,
    config_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_trigger")

        org_id = _ctx_org_id_val()
        tid, input_err = _validate_trigger_update_inputs(trigger_id, max_concurrent_runs, daily_spend_limit)
        if input_err:
            return input_err
        if tid is None:
            raise RuntimeError("_validate_trigger_update_inputs returned an error dict but no parsed trigger id")

        async with _session(org_id) as s:
            trigger = await _load_trigger_for_update(s, org_id, tid)
            if trigger is _TEAM_SCOPE_ERROR:
                return _team_scope_error("pipeline", str(tid))
            if trigger is None:
                return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}

            cron_config_requested = cron_expression is not None or cron_timezone is not None
            if cron_config_requested and trigger.trigger_type != "cron":
                return {"error": "validation", "detail": "Only cron triggers can have cron configuration"}

            ongoing_scan_interval_changed, ongoing_err = await _validate_ongoing_trigger_update(
                s, trigger, max_concurrent_runs, daily_spend_limit, config_json, active, clear_daily_spend_limit
            )
            if ongoing_err:
                return ongoing_err

            next_fire_at, cron_err = _validate_cron_update(trigger, cron_expression, cron_timezone)
            if cron_err:
                return cron_err

            prev_max = trigger.max_concurrent_runs
            prev_active = trigger.active
            await _apply_trigger_field_updates(
                s,
                trigger,
                active,
                max_concurrent_runs,
                daily_spend_limit,
                clear_daily_spend_limit,
                config_json,
                cron_expression,
                cron_timezone,
                next_fire_at,
                prev_active,
            )

            _recompute_ongoing_next_fire(
                trigger, max_concurrent_runs, active, prev_max, prev_active, ongoing_scan_interval_changed
            )
            await s.flush()
            from modulo.core.cron_helpers import _count_ongoing_runs

            in_flight = await _count_ongoing_runs(s, trigger.id) if trigger.trigger_type == "ongoing" else 0
            # FAR-251 — surface the updated trigger's streak_status exactly as
            # the REST update serializer does (computed inside the RLS
            # transaction so a re-enabled trigger reflects its reset streak).
            updated_streak_status = await _streak_status_for(s, trigger)

        # FAR-190: clear the config-failure Redis counter only AFTER the commit
        # (the _session context commits on exit); best-effort.
        if active is True and not prev_active:
            await clear_trigger_streak_after_reenable(trigger.id)

        return _trigger_detail_dict(trigger, in_flight, updated_streak_status)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("update_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("update_trigger failed")
        return _tool_error("Failed to update trigger")


@mcp.tool(description="Soft-delete a trigger by ID.")
@_RETRY_DB
async def delete_trigger(trigger_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_trigger")

        org_id = _ctx_org_id_val()
        try:
            tid = uuid.UUID(trigger_id)
        except ValueError:
            return {"error": "invalid_id", "field": "trigger_id", "detail": f"Invalid UUID format: {trigger_id}"}

        from sqlalchemy import select

        from modulo.db.crud.trigger import soft_delete_trigger
        from modulo.db.models.trigger import Trigger

        async with _session(org_id) as s:
            trigger = (
                await s.execute(
                    select(Trigger).where(
                        Trigger.id == tid,
                        Trigger.organisation_id == org_id,
                        Trigger.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if trigger is None:
                return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}
            if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, trigger.pipeline_id)):
                return _team_scope_error("pipeline", str(trigger.pipeline_id))
            deleted = await soft_delete_trigger(s, tid)

        if deleted is None:
            return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}

        return {"id": str(tid), "deleted": True}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("delete_trigger failed")
        return _tool_error("Failed to delete trigger")


@mcp.tool(
    description="Pause or resume all pipeline triggers for the current organisation. "
    "When paused, new trigger-initiated runs (webhook, cron, polling, agent_signal, "
    "replay) are blocked at the run-creation gate; manual runs, MCP trigger_pipeline, "
    "and scheduled reports are not paused. Idempotent: setting the state it is "
    "already in is a no-op that returns the current state without writing an audit event."
)
@_RETRY_DB
async def set_org_triggers_paused(paused: bool) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("set_org_triggers_paused")

        org_id = _ctx_org_id_val()

        from datetime import UTC

        from modulo.core.audit_logger import append_audit_event
        from modulo.db.crud.organisation import get_organisation

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)
            if org is None:
                return {"error": "not_found", "detail": "Organisation not found"}

            # Idempotency: toggling to the current state is a no-op (no audit write).
            if org.triggers_paused == paused:
                paused_at = org.triggers_paused_at.isoformat() if org.triggers_paused_at else None
                return {"paused": org.triggers_paused, "paused_at": paused_at}

            org.triggers_paused = paused
            org.triggers_paused_at = datetime.now(UTC) if paused else None
            await s.flush()

            # Audit is fail-open-with-alert: the toggle ALWAYS commits; a failed
            # audit write is loudly logged and never rolls back the toggle.
            try:
                await append_audit_event(
                    s,
                    org_id=org_id,
                    event_type="triggers_paused",
                    actor_user_id=_ctx_user_id_val(),
                    payload_json={"paused": paused},
                )
            except SQLAlchemyError:
                _log.exception("set_org_triggers_paused audit write failed")
            except Exception:
                _log.exception("set_org_triggers_paused audit write failed (non-DB)")

            return {
                "paused": org.triggers_paused,
                "paused_at": org.triggers_paused_at.isoformat() if org.triggers_paused_at else None,
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("set_org_triggers_paused failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except StarletteHTTPException:
        return {"error": "not_found", "detail": "Organisation not found"}
    except Exception:
        _log.exception("set_org_triggers_paused failed")
        return _tool_error("Failed to update org trigger pause state")


@mcp.tool(description="Delete a pipeline by ID.")
@_RETRY_DB
async def delete_pipeline(
    pipeline_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_pipeline")

        org_id = _ctx_org_id_val()
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

        from modulo.db.crud.pipeline import soft_delete_pipeline

        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            deleted = await soft_delete_pipeline(s, pid)

        if not deleted:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

        return {"status": "deleted", "pipeline_id": pipeline_id}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("delete_pipeline failed")
        return _tool_error("Failed to delete pipeline")


@mcp.tool(description="Delete a connector instance by ID.")
@_RETRY_DB
async def delete_connector(
    connector_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_connector")

        org_id = _ctx_org_id_val()
        try:
            cid = uuid.UUID(connector_id)
        except ValueError:
            return {
                "error": "invalid_id",
                "field": "connector_id",
                "detail": f"Invalid UUID format: {connector_id}",
            }

        from modulo.db.crud.connector_instance import delete_connector_instance as db_delete_connector

        async with _session(org_id) as s:
            deleted = await db_delete_connector(s, cid)

        if not deleted:
            return {"error": "connector_not_found", "connector_id": connector_id}
        return {"status": "deleted", "connector_id": connector_id}

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_connector failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception:
        _log.exception("delete_connector failed")
        return _tool_error("Failed to delete connector")


@mcp.tool(
    description="Create or update a secret in the organisation vault. "
    "Secrets are encrypted at rest and scoped to the organisation. "
    "Returns the created secret details."
)
@_RETRY_DB
async def create_secret(
    key: str,
    value: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_secret")

        if not key or not key.strip():
            return {"error": "validation_failed", "field": "key", "detail": "Secret key is required"}
        if len(key) > 255:
            return {"error": "validation_failed", "field": "key", "detail": "Secret key exceeds 255 characters"}
        if not value:
            return {"error": "validation_failed", "field": "value", "detail": "Secret value is required"}

        org_id = _ctx_org_id_val()
        from modulo.settings import get_settings

        settings = get_settings()
        from modulo.core.secrets_backend import create_secrets_backend

        async with _session(org_id) as s:
            secrets_backend = create_secrets_backend(
                fernet_key=settings.fernet_key,
                session=s,
            )
            await secrets_backend.set_secret(key, value)

        return {"status": "created", "key": key}

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_secret failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception:
        _log.exception("create_secret failed")
        return _tool_error("Failed to create secret")


@mcp.tool(
    description="List all secret keys in the organisation vault. "
    "Returns secret keys and metadata — never exposes secret values."
)
@_RETRY_DB
async def list_secrets(
    limit: int = 100,
    search: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_secrets")

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            from sqlalchemy import func, select

            from modulo.db.models.secret import Secret

            query = select(Secret).where(Secret.organisation_id == org_id)
            if search:
                query = query.where(Secret.key.ilike(f"%{search}%"))
            query = query.order_by(Secret.key).limit(limit)

            result = await s.execute(query)
            secrets = result.scalars().all()

            count_query = select(func.count()).select_from(Secret).where(Secret.organisation_id == org_id)
            if search:
                count_query = count_query.where(Secret.key.ilike(f"%{search}%"))
            total = (await s.execute(count_query)).scalar() or 0

        return {
            "secrets": [
                {
                    "key": sec.key,
                    "created_at": sec.created_at.isoformat() if sec.created_at else None,
                    "updated_at": sec.updated_at.isoformat() if sec.updated_at else None,
                }
                for sec in secrets
            ],
            "total": total,
        }

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_secrets failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception:
        _log.exception("list_secrets failed")
        return _tool_error("Failed to list secrets")


@mcp.tool(description="Delete a secret from the organisation vault by key.")
@_RETRY_DB
async def delete_secret(
    key: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_secret")

        if not key or not key.strip():
            return {"error": "validation_failed", "field": "key", "detail": "Secret key is required"}

        org_id = _ctx_org_id_val()
        from modulo.settings import get_settings

        settings = get_settings()
        from modulo.core.secrets_backend import create_secrets_backend

        async with _session(org_id) as s:
            secrets_backend = create_secrets_backend(
                fernet_key=settings.fernet_key,
                session=s,
            )
            await secrets_backend.delete_secret(key)

        return {"status": "deleted", "key": key}

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_secret failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception:
        _log.exception("delete_secret failed")
        return _tool_error("Failed to delete secret")


# ---------------------------------------------------------------------------
# Organisation API-key management (REST parity with /api/v1/api-keys)
# ---------------------------------------------------------------------------


def _parse_api_key_expires_at(value: str) -> datetime:
    """Parse an ISO datetime, normalising naive values to UTC (REST parity)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _deny_break_glass_mint(session: AsyncSession, account_id: uuid.UUID) -> None:
    """REST-parity deny_break_glass_mint for MCP credential-minting tools.

    Break-glass accounts can never mint or revoke credentials (plan v17,
    API-key + long-lived deny). Mirrors the FastAPI dependency of the same
    name: load the account by primary key and deny outright when
    ``account.is_break_glass`` is True — for a break-glass account the shared
    ``is_break_glass_denied`` / ``is_break_glass_live`` decisions are ALWAYS
    true (live OR denied), so the predicate call is redundant. A missing
    account or a DB read failure raises (fail-closed) rather than silently
    allowing a mint.
    """
    from modulo.db.models.account import Account

    account = await session.get(Account, account_id)
    # The guard below means `account.is_break_glass is True`: the union of the
    # shared `is_break_glass_denied` / `is_break_glass_live` decisions is then
    # ALWAYS true (plan v17 — break-glass can never mint, live OR denied), so
    # any break-glass account is denied outright.
    if account is not None and account.is_break_glass is True:
        _log.warning(
            "permission.break_glass_mint_denied",
            extra={"account_id": str(account_id)},
        )
        raise MCPAuthorizationError("Break-glass accounts cannot create or modify secrets/credentials")


async def _enforce_api_key_mint_cap(
    session: AsyncSession,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
    requested_role: str,
) -> None:
    """Enforce the API-key role-cap: never mint above the caller's LIVE role.

    Mirrors ``_enforce_mint_cap`` in ``api/routes/api_keys.py`` — the live
    membership role is the authoritative source, so a runner cannot mint an
    operator key, an operator can mint operator/runner, and a removed or
    deactivated member's live role is None, denying the mint outright.
    """
    live_role = await resolve_role_from_membership(
        session,
        str(account_id),
        str(org_id),
    )
    if live_role is None:
        _log.warning(
            _CODE_PERMISSION_API_KEY_ROLE_CAP,
            extra={"requested_role": requested_role, "live_role": None},
        )
        raise MCPAuthorizationError("Active organisation membership required to manage API keys")
    if org_role_level(requested_role) > org_role_level(live_role):
        _log.warning(
            _CODE_PERMISSION_API_KEY_ROLE_CAP,
            extra={"requested_role": requested_role, "live_role": live_role},
        )
        raise MCPAuthorizationError(
            f"Cannot use role '{requested_role}' for an API key while your live role is '{live_role}'"
        )


async def _require_admin_for_team_key(org_id: uuid.UUID) -> None:
    """REST parity for team-scoped keys: feature + admin guard before setting team_id."""
    async with _session(org_id) as s:
        ctx = await resolve_plan_context(get_settings(), s)
        if not ctx.feature_enabled("team_rbac"):
            raise MCPAuthorizationError("Team-scoped API keys require an upgraded plan")
    if ORG_ROLE_HIERARCHY.get(_ctx_role_val() or "", -1) < ORG_ROLE_HIERARCHY["admin"]:
        raise MCPAuthorizationError("Only admin users can perform this action")


def _validate_api_key_role_and_name(name: str, role: str) -> dict[str, Any] | None:
    """Return a validation error dict for invalid role/name, or None."""
    if role not in ("operator", "runner"):
        return {
            "error": "validation_failed",
            "field": "role",
            "detail": "role must be 'operator' or 'runner'. admin keys are prohibited.",
        }
    if not name.strip():
        return {
            "error": "validation_failed",
            "field": "name",
            "detail": "API key name must not be blank",
        }
    return None


def _parse_api_key_expires(expires_at: str | None) -> tuple[datetime | None, dict[str, Any] | None]:
    """Parse and validate the expiry. Returns (datetime, error_or_None)."""
    if not expires_at:
        return (None, None)
    try:
        parsed = _parse_api_key_expires_at(expires_at)
    except ValueError:
        return (
            None,
            {
                "error": "validation_failed",
                "field": "expires_at",
                "detail": "expires_at must be a valid ISO-8601 datetime",
            },
        )
    if parsed <= datetime.now(UTC):
        return (
            None,
            {
                "error": "validation_failed",
                "field": "expires_at",
                "detail": "expires_at must be in the future",
            },
        )
    return (parsed, None)


async def _parse_api_key_team_id(
    team_id: str | None, org_id: uuid.UUID
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse and validate the team_id. Returns (team_uuid, error_or_None)."""
    if team_id is None:
        return (None, None)
    try:
        team_uuid = uuid.UUID(team_id)
    except ValueError:
        return (
            None,
            {
                "error": "invalid_id",
                "field": "team_id",
                "detail": f"Invalid UUID format: {team_id}",
            },
        )
    await _require_admin_for_team_key(org_id)
    return (team_uuid, None)


@mcp.tool(
    description=(
        "Create a new organisation API key. Returns the full mk_... key value "
        "ONLY at creation — store it immediately, it is never returned again. "
        "Mirrors POST /api/v1/api-keys. Roles: 'operator' or 'runner'. A key "
        "cannot be minted above the caller's live org role."
    ),
)
@_RETRY_DB
async def create_api_key(
    name: str,
    role: str = "operator",
    expires_at: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_api_key")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        validation_error = _validate_api_key_role_and_name(name, role)
        if validation_error is not None:
            return validation_error

        name = name.strip()

        parsed_expires_at, expires_error = _parse_api_key_expires(expires_at)
        if expires_error is not None:
            return expires_error

        team_uuid, team_error = await _parse_api_key_team_id(team_id, org_id)
        if team_error is not None:
            return team_error

        async with _session(org_id) as s:
            await _deny_break_glass_mint(s, account_id)
            await _enforce_api_key_mint_cap(s, account_id, org_id, role)
            key, full_key = await auth_create_api_key(
                s,
                org_id=org_id,
                name=name,
                role=role,
                account_id=account_id,
                team_id=team_uuid,
                expires_at=parsed_expires_at,
            )

        return {
            "id": str(key.id),
            "name": key.name,
            "role": key.role,
            "key_value": full_key,
            "lookup_prefix": f"mk_{key.lookup_prefix}****",
            "created_at": key.created_at.isoformat() if key.created_at else None,
            "team_id": str(key.team_id) if key.team_id else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return {"error": "conflict", "detail": "A resource with this value already exists"}
    except ProgrammingError:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except SQLAlchemyError:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)
    except Exception:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return _tool_error("Failed to create API key")


@mcp.tool(
    description=(
        "List API keys in the organisation. Returns id/name/role/lookup_prefix/"
        "created_at/team_id — never full key values. Mirrors GET /api/v1/api-keys."
    ),
)
@_RETRY_DB
async def list_api_keys() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_api_keys")

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            keys = await auth_list_api_keys(s, org_id)

        return {"api_keys": keys, "total": len(keys)}
    except ProgrammingError:
        _log.exception(_MSG_LIST_API_KEYS_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except SQLAlchemyError:
        _log.exception(_MSG_LIST_API_KEYS_FAILED)
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)
    except Exception:
        _log.exception(_MSG_LIST_API_KEYS_FAILED)
        return _tool_error("Failed to list API keys")


@mcp.tool(
    description=(
        "Revoke an API key by ID. The key is immediately invalidated and can "
        "no longer authenticate. Mirrors DELETE /api/v1/api-keys/{key_id}."
    ),
)
@_RETRY_DB
async def revoke_api_key(key_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("revoke_api_key")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        try:
            kid = uuid.UUID(key_id)
        except ValueError:
            return {
                "error": "invalid_id",
                "field": "key_id",
                "detail": f"Invalid UUID format: {key_id}",
            }

        async with _session(org_id) as s:
            await _deny_break_glass_mint(s, account_id)
            revoked = await auth_revoke_api_key(s, kid, org_id)

        if not revoked:
            return {"error": "not_found", "detail": "API key not found"}
        return {"id": str(kid), "revoked": True}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return {"error": "conflict", "detail": "A resource with this value already exists"}
    except ProgrammingError:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except SQLAlchemyError:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)
    except Exception:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return _tool_error("Failed to revoke API key")


@mcp.tool(description="Create a new agent. Returns the created agent details.")
@_RETRY_DB
async def create_agent(
    name: str,
    prompt_template: str,
    description: str | None = None,
    model_backend_id: str | None = None,
    input_schema_id: str | None = None,
    output_schema_id: str | None = None,
    connector_type_refs: list[dict[str, Any]] | None = None,
    required_environment_capabilities: list[str] | None = None,
    is_executable: bool = True,
    template_id: str | None = None,
    agent_command: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_agent")

        from modulo.db.crud.agent import create_agent as db_create_agent

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        parsed_model_backend_id = uuid.UUID(model_backend_id) if model_backend_id else None
        parsed_input_schema_id = uuid.UUID(input_schema_id) if input_schema_id else None
        parsed_output_schema_id = uuid.UUID(output_schema_id) if output_schema_id else None

        async with _session(org_id) as s:
            agent = await db_create_agent(
                s,
                org_id=org_id,
                name=name,
                account_id=account_id,
                is_executable=is_executable,
                input_schema_id=parsed_input_schema_id,
                input_schema_version="latest",
                output_schema_id=parsed_output_schema_id,
                output_schema_version="latest",
                prompt_template=prompt_template,
                model_backend_id=parsed_model_backend_id,
                description=description,
                connector_type_refs=connector_type_refs or [],
                template_id=template_id,
                agent_command=agent_command,
                required_environment_capabilities=required_environment_capabilities,
            )

        return {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "is_executable": agent.is_executable,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_agent failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as e:
        _log.exception("create_agent failed")
        return {"error": "internal_error", "detail": f"Failed to create agent: {e}"}

    # ---------------------------------------------------------------------------
    # Context retrieval tools
    # ---------------------------------------------------------------------------


_doc_index: DocumentationIndex | None = None
_doc_index_ts: float = 0.0
_doc_index_ttl: float = 300.0  # 5 minutes


def _get_doc_index() -> DocumentationIndex:
    global _doc_index, _doc_index_ts
    import time as _time

    now = _time.time()
    if _doc_index is None or (now - _doc_index_ts) > _doc_index_ttl:
        _doc_index = DocumentationIndex.build()
        _doc_index_ts = now
    return _doc_index


SENSITIVE_CONFIG_KEYS: set[str] = {
    "fernet_key",
    "secret_key",
    "database_url",
    "db_url",
    "postgres_url",
    "redis_url",
    "api_key",
    "api_keys",
    "modulo_license_key",
    "modulo_secret_key",
    "modulo_fernet_key",
}


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(lower.startswith(prefix) for prefix in SENSITIVE_CONFIG_KEYS)


@mcp.tool(
    name="search_documentation",
    description=(
        "Search the product surface and navigation for relevant sections. Supports "
        "free-text keyword search against routes and pages in the product manifest. Returns Markdown-formatted results."
    ),
)
async def search_documentation(query: str, section: str | None = None) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        index = _get_doc_index()
        results = index.search(query, section=section)
        if not results:
            return {"results": "No documentation found for query.", "count": 0}
        formatted = index.format_results(results)
        return {"results": formatted, "count": len(results)}
    except Exception:
        _log.exception("search_documentation failed")
        return _tool_error("Failed to search documentation")


@mcp.tool(
    description=(
        "Get current health status of all connectors, model backends, and triggers. "
        "Returns a Markdown table plus structured JSON fields. "
        "For individual connector/model-backend details, see modulo://connectors "
        "and modulo://model-backends resources."
    ),
)
async def get_integration_status() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        from sqlalchemy import func, select

        from modulo.db.models.connector_instance import ConnectorInstance
        from modulo.db.models.model_backend import ModelBackend
        from modulo.db.models.trigger import Trigger

        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            connector_rows = (
                (await s.execute(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id)))
                .scalars()
                .all()
            )
            backend_rows = (
                (await s.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id))).scalars().all()
            )
            trigger_count_result = await s.execute(
                select(func.count()).select_from(Trigger).where(Trigger.organisation_id == org_id)
            )
            trigger_count = trigger_count_result.scalar_one()

        connector_list: list[dict[str, Any]] = []
        connector_lines = [
            "| Name | Type | Status | Last Check | Error |",
            "|------|------|--------|------------|-------|",
        ]
        for c in connector_rows:
            last_check = c.last_health_check_at.isoformat() if c.last_health_check_at else "never"
            error = c.last_health_check_error or ""
            connector_lines.append(f"| {c.name} | {c.connector_type_id} | {c.status} | {last_check} | {error} |")
            connector_list.append(
                {
                    "name": c.name,
                    "type": c.connector_type_id,
                    "status": c.status,
                    "last_check": last_check,
                    "error": error,
                }
            )

        backend_list: list[dict[str, Any]] = []
        backend_lines = [
            "| Name | Provider | Model | Has Credentials | Status |",
            "|------|----------|-------|-----------------|--------|",
        ]
        for b in backend_rows:
            has_creds = "yes" if b.credentials_ciphertext else "no"
            backend_lines.append(f"| {b.name} | {b.provider} | {b.model_id} | {has_creds} | {b.status} |")
            backend_list.append(
                {
                    "name": b.name,
                    "provider": b.provider,
                    "model": b.model_id,
                    "has_credentials": bool(b.credentials_ciphertext),
                    "status": b.status,
                }
            )

        parts = [
            f"## Connectors ({len(connector_rows)})",
            "\n".join(connector_lines) if connector_rows else "No connectors configured.",
            "",
            f"## Model Backends ({len(backend_rows)})",
            "\n".join(backend_lines) if backend_rows else "No model backends configured.",
            "",
            f"## Triggers\n\nTotal triggers: {trigger_count}",
        ]
        return {
            "results": "\n".join(parts),
            "connectors": connector_list,
            "model_backends": backend_list,
            "trigger_count": trigger_count,
        }
    except ProgrammingError:
        _log.exception("get_integration_status failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_integration_status failed")
        return _tool_error("Failed to get integration status")


_VALID_CONFIG_SECTIONS = {"remy", "plan", "rate_limits"}


def _config_key_prefixes(section: str | None) -> list[str] | None:
    """Key prefixes matching a config section, or None to match all sections."""
    org_ctx = f"{_ctx_org_id_val()}"
    if section == "remy":
        return [f"remy_config:{org_ctx}", "remy_config"]
    if section in {"plan", "rate_limits"}:
        return ["feature_flags", "default_plan", "rate_limits"]
    return None


def _config_matches(cfg: Any, key_prefixes: list[str] | None) -> bool:
    """True when *cfg* falls within *key_prefixes* and is not a sensitive key."""
    if key_prefixes is not None and not any(cfg.key.startswith(p) for p in key_prefixes):
        return False
    return not _is_sensitive_key(cfg.key)


def _config_table(filtered: list[Any]) -> str:
    """Render config rows as a markdown table with long values truncated."""
    lines = ["| Key | Value |", "|-----|-------|"]
    for cfg in filtered:
        val = cfg.value
        val_str = json.dumps(val, default=str) if isinstance(val, dict) else str(val)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        lines.append(f"| {cfg.key} | {val_str} |")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Get org-level configuration. Optionally filter to a specific section "
        "(remy, plan, rate_limits). Never exposes secrets."
    ),
)
async def get_org_config(section: str | None = None) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        if section is not None and section not in _VALID_CONFIG_SECTIONS:
            return {
                "error": "invalid_section",
                "detail": f"section must be one of: {', '.join(sorted(_VALID_CONFIG_SECTIONS))}",
            }
        from modulo.db.crud.system_config import list_config

        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            configs = await list_config(s)

        key_prefixes = _config_key_prefixes(section)
        filtered = [cfg for cfg in configs if _config_matches(cfg, key_prefixes)]

        if not filtered:
            section_label = section or "org"
            return {"results": f"No configuration found for section '{section_label}'.", "count": 0}

        return {"results": _config_table(filtered), "count": len(filtered)}
    except ProgrammingError:
        _log.exception("get_org_config failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_org_config failed")
        return _tool_error("Failed to get org configuration")


@mcp.tool(
    description="List product features enabled on the current plan tier.",
)
async def get_available_features() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        from modulo.core.feature_flags import resolve_plan_context

        org_id = _ctx_org_id_val()
        settings = get_settings()

        from modulo.db.crud.organisation import get_organisation

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)

        async with _session(org_id) as s:
            plan_ctx = await resolve_plan_context(settings, s, org)

        current_tier = plan_ctx.tier()
        all_flags = plan_ctx.list_enabled_features()

        lines = ["| Feature | Required Tier | Available |", "|---------|---------------|-----------|"]
        for flag in all_flags:
            available = "yes" if flag.currently_active else "no"
            lines.append(f"| {flag.name} | {flag.tier} | {available} |")

        return {"results": "\n".join(lines), "tier": current_tier, "feature_count": len(all_flags)}
    except ProgrammingError:
        _log.exception("get_available_features failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_available_features failed")
        return _tool_error("Failed to get available features")


@mcp.tool(
    description="Create a new schema. Creates the schema record plus a "
    "'latest' version placeholder so agents can reference the schema "
    "immediately. Returns the created schema details.",
)
@_RETRY_DB
async def create_schema(
    name: str,
    description: str | None = None,
    abstract_name: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_schema")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            schema = await db_create_schema(
                s,
                org_id=org_id,
                name=name,
                account_id=account_id,
                description=description,
                abstract_name=abstract_name,
            )

            from modulo.db.models.schema import SchemaVersion

            s.add(
                SchemaVersion(
                    organisation_id=org_id,
                    schema_id=schema.id,
                    version="latest",
                    version_number=0,
                    definition_json={"type": "object", "properties": {}, "additionalProperties": True},
                    account_id=account_id,
                )
            )

        return {
            "id": str(schema.id),
            "name": schema.name,
            "description": schema.description,
            "abstract_name": schema.abstract_name,
            "created_at": schema.created_at.isoformat() if schema.created_at else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError as exc:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return {"error": "conflict", "detail": f"A schema with this name already exists: {exc.orig}"}
    except ProgrammingError:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return {"error": "database_unavailable", "detail": "Database operation failed. Please try again."}
    except Exception:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return _tool_error("Failed to create schema")


@mcp.tool(
    description="List registered schemas with cursor-based pagination. Returns schema metadata.",
)
@_RETRY_DB
async def list_schemas(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_schemas(s, cursor=cursor, limit=lim)

        return {
            "data": [
                {
                    "id": str(sc.id),
                    "name": sc.name,
                    "description": sc.description,
                    "version": sc.abstract_name,
                    "created_at": sc.created_at.isoformat() if sc.created_at else None,
                }
                for sc in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except ProgrammingError:
        _log.exception("list_schemas failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_schemas failed")
        return _tool_error("Failed to list schemas")


@mcp.tool(
    description="AI-assisted schema inference. Takes a sample JSON payload and returns an inferred "
    "JSON Schema definition.",
)
@_RETRY_DB
async def infer_schema(
    input_sample: dict[str, Any],
    pipeline_id: str | None = None,
) -> dict[str, Any]:
    del pipeline_id  # retained for backward-compatible MCP input schema; unused by design
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("infer_schema")

        # Preview feature - requires dev mode
        from modulo.settings import get_settings

        settings = get_settings()
        if not settings.modulo_dev_mode:
            return _tool_error(
                "Schema inference requires developer mode. "
                "Set MODULO_DEV_MODE=true or toggle Developer Mode in Admin > Feature Flags."
            )
        from modulo.core.schema_registry import SchemaInferenceError, SchemaInferenceService

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            from modulo.db.crud.model_backend import list_model_backends

            mbs = await list_model_backends(s, org_id=org_id, page_size=1)
            if not mbs.items:
                return {"error": "no_backend", "detail": "No model backends configured; cannot perform inference"}

            from modulo.core.model_backend_hub import ModelBackendHub
            from modulo.core.secrets_backend import create_secrets_backend

            secrets_backend = create_secrets_backend(fernet_key=get_settings().fernet_key)
            async with ModelBackendHub() as mh:
                await mh.initialise(mbs.items, secrets_backend=secrets_backend)
                backend = await mh.get(mbs.items[0].id)

                samples = [input_sample]
                service = SchemaInferenceService(backend)
                definition = await service.infer(samples)

        return {
            "definition": definition,
            "sample_count": 1,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except SchemaInferenceError as exc:
        return {"error": "inference_failed", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("infer_schema failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("infer_schema failed")
        return _tool_error("Failed to infer schema")


@mcp.tool(
    description="Validate a payload against a registered schema by schema_id. Returns validation errors or success.",
)
@_RETRY_DB
async def validate_payload(
    schema_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        from jsonschema import Draft202012Validator, ValidationError
        from jsonschema.exceptions import SchemaError as JsSchemaError

        org_id = _ctx_org_id_val()
        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "field": "schema_id", "detail": f"Invalid UUID format: {schema_id}"}

        async with _session(org_id) as s:
            schema = await get_schema(s, sid)
            if schema is None:
                return {"error": "not_found", "detail": f"Schema {schema_id} not found"}

            from sqlalchemy import select

            from modulo.db.models.schema import SchemaVersion

            result = await s.execute(
                select(SchemaVersion)
                .where(SchemaVersion.schema_id == sid)
                .order_by(SchemaVersion.version_number.desc())
                .limit(1)
            )
            sv = result.scalar_one_or_none()
            if sv is None:
                return {"error": "no_version", "detail": f"Schema {schema_id} has no versions"}

        definition = sv.definition_json
        try:
            Draft202012Validator.check_schema(definition)
            validator = Draft202012Validator(definition)
            errors = list(validator.iter_errors(payload))
            if not errors:
                return {"valid": True, "errors": []}
            return {
                "valid": False,
                "errors": [
                    {
                        "path": ".".join(str(p) for p in e.path),
                        "message": e.message,
                    }
                    for e in errors
                ],
            }
        except (ValidationError, JsSchemaError) as exc:
            return {
                "valid": False,
                "errors": [{"path": "(schema)", "message": f"Invalid schema definition: {exc.message}"}],
            }
    except ProgrammingError:
        _log.exception("validate_payload failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("validate_payload failed")
        return _tool_error("Failed to validate payload")


@mcp.tool(
    description="List housekeeping cleanup candidates for the organisation. "
    "Returns categories of potential cleanup items such as orphan secrets, "
    "unbound connectors, stale pipelines, and other candidates.",
)
@_RETRY_DB
async def list_housekeeping(limit: int = 100) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_housekeeping")
        from modulo.core.housekeeping import scan_all as hk_scan_all

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 500))
        async with _session(org_id) as s:
            results = await hk_scan_all(s, org_id)
        return {
            "categories": [
                {
                    "category": r.category,
                    "label": r.label,
                    "description": r.description,
                    "candidates": [c.to_dict() for c in r.candidates[:lim]],
                    "count": len(r.candidates),
                }
                for r in results
            ],
            "total_count": sum(len(r.candidates) for r in results),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_housekeeping failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_housekeeping failed")
        return _tool_error("Failed to list housekeeping candidates")


def _group_housekeeping_items(items: list[dict[str, str]], errors: list[dict[str, str]]) -> dict[str, list[str]]:
    """Group items by entity_type, collecting malformed items into *errors*."""
    grouped: dict[str, list[str]] = {}
    for item in items:
        et = item.get("entity_type", "")
        eid = item.get("id", "")
        if not et or not eid:
            errors.append({"error": "item missing entity_type or id", "item": str(item)})
            continue
        grouped.setdefault(et, []).append(eid)
    return grouped


async def _delete_housekeeping_entity(s: AsyncSession, model_cls: Any, eid: str, org_id: uuid.UUID) -> bool:
    """Delete a single entity by id under a savepoint. Returns True if deleted."""
    from sqlalchemy import select as _sa_select

    stmt = _sa_select(model_cls).where(
        model_cls.id == eid,
        model_cls.organisation_id == org_id,
    )
    obj = (await s.execute(stmt)).scalar_one_or_none()
    if obj is not None:
        await s.delete(obj)
        return True
    return False


async def _delete_housekeeping_group(
    s: AsyncSession,
    entity_type: str,
    ids: list[str],
    model_cls: Any,
    org_id: uuid.UUID,
    errors: list[dict[str, str]],
) -> int:
    """Delete each id in *ids* under a savepoint. Returns the number deleted."""
    deleted_count = 0
    for eid in ids:
        try:
            async with s.begin_nested():
                if await _delete_housekeeping_entity(s, model_cls, eid, org_id):
                    deleted_count += 1
        except IntegrityError:
            _log.warning("IntegrityError cleaning up %s %s", entity_type, eid)
            errors.append({"id": eid, "entity_type": entity_type, "error": "Foreign key constraint violation"})
    return deleted_count


async def _delete_housekeeping_groups(
    s: AsyncSession,
    grouped: dict[str, list[str]],
    hk_entity_map: Mapping[str, Any],
    org_id: uuid.UUID,
    errors: list[dict[str, str]],
) -> int:
    """Delete all grouped housekeeping items. Returns the total deleted count."""
    from modulo.core.housekeeping import NON_DELETABLE_ENTITY_TYPES

    deleted_count = 0
    for entity_type, ids in grouped.items():
        if entity_type in NON_DELETABLE_ENTITY_TYPES:
            errors.append({"entity_type": entity_type, "error": "Surfaced for triage only — not auto-deleted."})
            continue
        model_cls = hk_entity_map.get(entity_type)
        if model_cls is None:
            errors.append({"entity_type": entity_type, "error": f"Unknown entity type: {entity_type}"})
            continue
        deleted_count += await _delete_housekeeping_group(s, entity_type, ids, model_cls, org_id, errors)
    return deleted_count


@mcp.tool(
    description="Delete housekeeping cleanup candidates. "
    "Accepts a list of items with id and entity_type. "
    "Valid entity types: secret, connector, model_backend, pipeline, "
    "pipeline_snapshot, trigger, webhook_dedup, environment_profile, "
    "org_api_key, sso_provider, team, parameter_schema, schema, lifecycle_map. "
    "Deletions are grouped by entity type with per-group savepoints.",
)
async def perform_housekeeping(items: list[dict[str, str]]) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("perform_housekeeping")
        from modulo.core.housekeeping import ENTITY_MODEL_MAP as HK_ENTITY_MAP

        org_id = _ctx_org_id_val()
        errors: list[dict[str, str]] = []

        grouped = _group_housekeeping_items(items, errors)

        async with _session(org_id) as s:
            deleted_count = await _delete_housekeeping_groups(s, grouped, HK_ENTITY_MAP, org_id, errors)

        return {"deleted_count": deleted_count, "errors": errors}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("perform_housekeeping failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("perform_housekeeping failed")
        return _tool_error("Failed to perform housekeeping")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("modulo://pipelines")
async def resource_pipelines() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from modulo.db.crud.pipeline import list_pipelines

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await list_pipelines(s, page=1, page_size=50, team_id=_ctx_team_id_val())
    lines = [f"- {p.name} (id={p.id}, visibility={p.visibility})" for p in result.items]
    return f"Pipelines ({result.total} total):\n" + "\n".join(lines)


def _format_run_line(r: Any, child_rollups: dict[Any, tuple[Any, int]]) -> str:
    """Render a single run row as a text line for MCP resources."""
    child_cost, child_count = child_rollups.get(r.id, (_MCP_COST_ROLLUP_ZERO, 0))
    own_cost = Decimal(str(r.total_cost_usd)) if r.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
    aggregate_cost = _quantize_mcp_cost_rollup(own_cost + child_cost)
    line = (
        f"- Run {r.id} | status={r.status} | trigger={r.trigger_type} | "
        f"created={r.created_at.isoformat()} | "
        f"tokens={r.total_tokens or 0} | cost=${r.total_cost_usd or 0} | "
        f"child_count={child_count} | child_cost=${child_cost} | aggregate_cost=${aggregate_cost}"
    )
    if r.cost_breakdown is not None:
        breakdown = _sanitize_cost_breakdown(r.cost_breakdown)
        if breakdown:
            line += " | breakdown={" + ", ".join(_format_breakdown_line(e) for e in breakdown) + "}"
    return line


@mcp.resource("modulo://pipelines/{pipeline_id}/runs")
async def resource_pipeline_runs(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from modulo.db.crud.run import list_runs

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return f"error: Invalid UUID format: {pipeline_id}"
    async with _session(org_id) as s:
        pipeline = await get_pipeline(s, pid)
        if pipeline is None:
            return f"Pipeline {pipeline_id} not found."
        if _team_scoped_key_mismatch(pipeline.owner_team_id):
            return _team_scope_error_str("pipeline", pipeline_id)
        result = await list_runs(s, pipeline_id=pid, page=1, page_size=50, team_id=_ctx_team_id_val())
        # Child-run cost+count rollup: ONE GROUP BY query for the whole page,
        # joined in Python — never a per-row aggregate (avoids N+1).
        run_ids = [r.id for r in result.items]
        from modulo.db.crud.run import get_child_run_rollup

        child_rollups = await get_child_run_rollup(s, run_ids) if run_ids else {}

    if not result.items:
        return f"Pipeline '{pipeline.name}' has no runs."

    lines = [_format_run_line(r, child_rollups) for r in result.items]
    return f"Runs for pipeline {pipeline.name} ({result.total} total):\n" + "\n".join(lines)


@mcp.resource("modulo://pipelines/{pipeline_id}")
async def resource_pipeline_detail(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from sqlalchemy import func, select

    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.run import Run

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return f"error: Invalid UUID format: {pipeline_id}"
    async with _session(org_id) as s:
        pipeline = await get_pipeline(s, pid)
        if pipeline is None:
            return f"Pipeline {pipeline_id} not found."
        if _team_scoped_key_mismatch(pipeline.owner_team_id):
            return _team_scope_error_str("pipeline", pipeline_id)

        edge_result = await s.execute(
            select(func.count()).select_from(PipelineEdge).where(PipelineEdge.pipeline_id == pid)
        )
        edge_count = edge_result.scalar_one()

        snap_result = await s.execute(
            select(func.count()).select_from(PipelineSnapshot).where(PipelineSnapshot.pipeline_id == pid)
        )
        snapshot_count = snap_result.scalar_one()

        run_result = await s.execute(
            select(Run.created_at).where(Run.pipeline_id == pid).order_by(Run.created_at.desc()).limit(1)
        )
        last_run_at = run_result.scalar_one_or_none()

    parts = [
        f"Pipeline: {pipeline.name}",
        f"ID: {pipeline.id}",
        f"Description: {pipeline.description or '(none)'}",
        f"Status: {'active' if pipeline.graph_nodes_json else 'inactive'}",
        f"Visibility: {pipeline.visibility}",
        f"Created: {pipeline.created_at.isoformat()}",
        f"Node count: {len(pipeline.graph_nodes_json)}",
        f"Edge count: {edge_count}",
        f"Snapshot count: {snapshot_count}",
    ]
    if last_run_at:
        parts.append(f"Last run: {last_run_at.isoformat()}")
    return "\n".join(parts)


@mcp.resource("modulo://pipelines/{pipeline_id}/snapshots")
async def resource_pipeline_snapshots(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from modulo.db.crud.pipeline_snapshot_versioning import list_snapshots

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return f"error: Invalid pipeline_id UUID: {pipeline_id}"

    async with _session(org_id) as s:
        if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, pid)):
            return _team_scope_error_str("pipeline", pipeline_id)
        snapshots, _ = await list_snapshots(s, pid, page=1, page_size=50)

    lines = [
        f"- snapshot {s.snapshot_version} (id={s.id}, tag={s.tag or ''}, created={s.created_at.isoformat()})"
        for s in snapshots
    ]
    return f"Snapshots for pipeline {pipeline_id} ({len(snapshots)}):\n" + "\n".join(lines)


def _render_snapshot_node_line(n: dict[str, Any]) -> str:
    """Render a single node summary line for MCP resource output."""
    nid = n.get("id", "?")
    ntype = n.get("node_type", "?")
    agent_id = n.get("agent_id", "")
    agent_cmd = n.get("agent_command", "(required)")
    prompt_preview = (n.get("prompt_template", "") or "")[:80].replace("\n", " ")
    line = f"  - {nid} (type={ntype}, agent={agent_id}, command={agent_cmd})\n"
    if prompt_preview:
        line += f"    prompt: {prompt_preview}...\n"
    return line


def _render_snapshot_node_details(nodes: list[dict[str, Any]]) -> str:
    """Render the full node JSON plus prompt/command previews."""
    result = ""
    for n in nodes:
        safe = {k: v for k, v in n.items() if k not in ("agent_prompt", "agent_command")}
        result += json.dumps(safe, indent=2, default=str)[:2000] + "\n"
        ap = n.get("agent_prompt")
        if ap is None:
            ap = ""
        if ap:
            result += f"    agent_prompt: {ap[:200].replace(chr(10), ' ')}...\n"
        ac = n.get("agent_command", "") or ""
        if ac:
            result += f"    agent_command: {ac[:200].replace(chr(10), ' ')}...\n"
        cf = n.get("context_files", {}) or {}
        for cfp, cfc in cf.items():
            result += f"    context_file {cfp}: {len(str(cfc))} bytes\n"
        tid = n.get("template_id", "")
        if tid:
            result += f"    template_id: {tid}\n"
    return result


@mcp.resource("modulo://pipelines/{pipeline_id}/snapshots/{snapshot_id}")
async def resource_pipeline_snapshot_detail(pipeline_id: str, snapshot_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from modulo.db.crud.pipeline_snapshot_versioning import get_snapshot_detail

    org_id = _ctx_org_id_val()
    try:
        uuid.UUID(pipeline_id)
        sid = uuid.UUID(snapshot_id)
    except ValueError:
        return "error: Invalid UUID format"

    async with _session(org_id) as s:
        if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, uuid.UUID(pipeline_id))):
            return _team_scope_error_str("pipeline", pipeline_id)
        snap = await get_snapshot_detail(s, sid, organisation_id=org_id, pipeline_id=uuid.UUID(pipeline_id))

    if snap is None:
        return f"error: Snapshot {snapshot_id} not found"

    nodes = snap.graph_json.get("nodes", [])
    edges = snap.graph_json.get("edges", [])
    result = f"Snapshot {snapshot_id} (v{snap.snapshot_version}) for pipeline {pipeline_id}\n"
    result += f"Nodes ({len(nodes)}):\n"
    result += "".join(_render_snapshot_node_line(n) for n in nodes)
    result += f"Edges ({len(edges)}):\n"
    for e in edges:
        result += f"  - {e.get('id', '?')}: {e.get('source', '?')} -> {e.get('target', '?')} ({e.get('type', '?')})\n"
    result += "  Full node JSON:\n"
    result += _render_snapshot_node_details(nodes)
    result += f"Connector bindings: {json.dumps(snap.connector_bindings_json, indent=2)}\n"
    return result


@mcp.resource("modulo://runs/{run_id}")
async def resource_run(run_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    org_id = _ctx_org_id_val()
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return f"error: Invalid UUID format: {run_id}"
    async with _session(org_id) as s:
        run = await get_run(s, rid)
        if run is None:
            return f"Run {run_id} not found."
        if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
            return _team_scope_error_str("run", run_id)
        from modulo.db.crud.run import get_child_run_rollup

        child_rollups = await get_child_run_rollup(s, [rid])
        child_cost, child_count = child_rollups.get(rid, (_MCP_COST_ROLLUP_ZERO, 0))
        own_cost = Decimal(str(run.total_cost_usd)) if run.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
        aggregate_cost = _quantize_mcp_cost_rollup(own_cost + child_cost)
    parts = [
        f"Run: {run.id}",
        f"Pipeline: {run.pipeline_id}",
        f"Status: {run.status}",
        f"Trigger: {run.trigger_type}",
        f"Created: {run.created_at.isoformat()}",
    ]
    if run.error_code:
        parts.append(f"Error: {map_legacy_code(run.error_code)}")
    if run.total_cost_usd is not None:
        parts.append(f"Total cost: ${run.total_cost_usd}")
    parts.append(f"Child runs cost: ${child_cost}")
    parts.append(f"Child runs count: {child_count}")
    parts.append(f"Aggregate cost: ${aggregate_cost}")
    if run.cost_breakdown is not None:
        breakdown = _sanitize_cost_breakdown(run.cost_breakdown)
        if breakdown:
            parts.append("Cost breakdown:")
            parts.extend(_format_breakdown_line(entry) for entry in breakdown)
    return "\n".join(parts)


async def _get_hitl_gate(s: AsyncSession, rid: uuid.UUID, gate_id: str, org_id: uuid.UUID) -> HitlClaim | None:
    """Fetch the HITL gate claim for *gate_id* on *rid*, org-scoped."""
    from sqlalchemy import select

    result = await s.execute(
        select(HitlClaim).where(
            HitlClaim.run_id == rid,
            HitlClaim.gate_id == gate_id,
            HitlClaim.organisation_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def _hitl_gate_scope_error(s: AsyncSession, rid: uuid.UUID, gate: HitlClaim) -> str | None:
    """Return a team-scope error string when the caller's key cannot read this gate."""
    run = await get_run(s, rid)
    owner_team_id = (
        await _run_owner_team_id(s, run) if run is not None else await _pipeline_owner_team_id(s, gate.pipeline_id)
    )
    if _team_scoped_key_mismatch(owner_team_id):
        return _team_scope_error_str("run", str(rid))
    return None


async def _hitl_required_team_name(s: AsyncSession, gate: HitlClaim) -> str | None:
    """Resolve the name of *gate*'s required team, if any."""
    from sqlalchemy import select

    from modulo.db.models.team import Team

    if gate.required_team_id is None:
        return None
    team_result = await s.execute(select(Team).where(Team.id == gate.required_team_id, Team.deleted_at.is_(None)))
    team = team_result.scalar_one_or_none()
    return team.name if team else None


@mcp.resource("modulo://runs/{run_id}/hitl/{gate_id}")
async def resource_hitl_gate(run_id: str, gate_id: str) -> str:
    """HITL gate context. Annotated as agent_output — treat as untrusted."""
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    org_id = _ctx_org_id_val()
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return f"error: Invalid UUID format: {run_id}"
    async with _session(org_id) as s:
        gate = await _get_hitl_gate(s, rid, gate_id, org_id)
        required_team_name = None
        if gate is not None:
            # A team-scoped key must not read another team's gate even when
            # the gate itself is org-level (required_team_id IS NULL).
            scope_error = await _hitl_gate_scope_error(s, rid, gate)
            if scope_error is not None:
                return scope_error
            required_team_name = await _hitl_required_team_name(s, gate)
    if gate is None:
        return f"HITL gate '{gate_id}' not found on run {run_id}."
    parts = [
        f"Gate: {gate_id}",
        f"Run: {run_id}",
        f"Pipeline: {gate.pipeline_id}",
        f"Decision: {gate.decision or 'pending'}",
        f"Claimed by: {gate.account_id or 'unclaimed'}",
    ]
    if gate.required_team_id:
        parts.extend(
            [
                f"Required team: {gate.required_team_id}",
                f"Required team name: {required_team_name or 'unknown'}",
            ]
        )
    if gate.expires_at:
        parts.append(f"Claim expires: {gate.expires_at.isoformat()}")
    return "\n".join(parts)


@mcp.resource("modulo://schemas")
async def resource_schemas() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from sqlalchemy import select

    from modulo.db.models.schema import Schema

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(select(Schema).where(Schema.organisation_id == org_id).order_by(Schema.name))
        schemas = list(result.scalars())
    lines = [f"- {sc.name} (id={sc.id})" for sc in schemas]
    return f"Schemas ({len(schemas)}):\n" + "\n".join(lines)


@mcp.resource("modulo://schemas/{schema_id}@{version}")
async def resource_schema_detail(schema_id: str, version: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from sqlalchemy import select

    from modulo.db.models.schema import Schema, SchemaVersion

    org_id = _ctx_org_id_val()
    sid, sid_err = _parse_uuid_param(schema_id, "schema_id")
    if sid_err:
        return f"error: Invalid UUID format: {schema_id}"
    assert sid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        schema = await s.get(Schema, sid)
        if schema is None:
            return f"Schema {schema_id} not found."

        if version == "latest":
            result = await s.execute(
                select(SchemaVersion)
                .where(SchemaVersion.schema_id == sid)
                .order_by(SchemaVersion.version_number.desc())
                .limit(1)
            )
            sv = result.scalar_one_or_none()
        else:
            result = await s.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == sid,
                    SchemaVersion.version == version,
                )
            )
            sv = result.scalar_one_or_none()

        if sv is None:
            return f"Schema version '{version}' not found for schema {schema_id}."

    defn = sv.definition_json or {}
    schema_type = defn.get("type", "object")

    fields: list[dict[str, Any]] = []
    if "properties" in defn:
        required_set = set(defn.get("required", []))
        fields = [
            {
                "name": name,
                "type": prop.get("type", "unknown"),
                "required": name in required_set,
            }
            for name, prop in defn["properties"].items()
        ]
    elif "fields" in defn:
        fields = [
            {
                "name": f.get("name", "?"),
                "type": f.get("type", "unknown"),
                "required": f.get("required", False),
            }
            for f in defn["fields"]
        ]

    lines = [
        f"Schema: {schema.name}",
        f"ID: {schema.id}",
        f"Type: {schema_type}",
        f"Version: {sv.version}",
        f"Created: {sv.created_at.isoformat()}",
        f"Fields ({len(fields)}):",
    ]
    for f in fields:
        req = "required" if f["required"] else "optional"
        lines.append(f"  - {f['name']}: {f['type']} ({req})")

    return "\n".join(lines)


@mcp.resource("modulo://connectors")
async def resource_connectors() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from sqlalchemy import select

    from modulo.db.models.connector_instance import ConnectorInstance

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(
            select(ConnectorInstance)
            .where(ConnectorInstance.organisation_id == org_id)
            .order_by(ConnectorInstance.name)
        )
        connectors = list(result.scalars())
    lines = [f"- {c.name} (id={c.id}, type={c.connector_type_id})" for c in connectors]
    return f"Connectors ({len(connectors)}):\n" + "\n".join(lines)


@mcp.resource("modulo://model-backends")
async def resource_model_backends() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from sqlalchemy import select

    from modulo.db.models.model_backend import ModelBackend

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(
            select(ModelBackend).where(ModelBackend.organisation_id == org_id).order_by(ModelBackend.name)
        )
        backends = list(result.scalars())
    lines = [f"- {b.name} (id={b.id}, {b.provider}/{b.model_id})" for b in backends]
    return f"Model Backends ({len(backends)}):\n" + "\n".join(lines)


@mcp.resource("modulo://library")
async def resource_library() -> str:
    """List library primitives — schemas, agents, workflows, pipeline templates, test fixtures.

    For filtered browsing, use the ``search_library`` tool instead.
    """
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    try:
        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            result = await list_primitives(
                s,
                org_id,
                page=1,
                page_size=50,
                include_community=True,
            )
        if not result.items:
            return "Library is empty."
        lines: list[str] = []
        for p in result.items:
            tags_str = ", ".join(p.tags) if p.tags else ""
            rating_str = f"{p.average_rating:.1f}" if p.average_rating is not None else "N/A"
            desc = f" — {p.description}" if p.description else ""
            lines.append(
                f"- {p.name} (id={p.id}, type={p.primitive_type}, "
                f"v{p.version}, tags=[{tags_str}], rating={rating_str}){desc}"
            )
        header = f"Library ({result.total} primitives):"
        return header + "\n" + "\n".join(lines)
    except Exception:
        _log.exception("resource_library failed")
        return "error: Failed to browse library"


@mcp.resource("modulo://library/{primitive_type}/{slug}")
async def resource_library_detail(primitive_type: str, slug: str) -> str:
    """Get details of a single library primitive by type and slug."""
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    try:
        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            p = await get_primitive_by_slug(s, org_id, primitive_type, slug)
        if p is None:
            return f"Library primitive '{slug}' of type '{primitive_type}' not found."

        tags_str = ", ".join(p.tags) if p.tags else ""
        rating_str = f"{p.average_rating:.2f}" if p.average_rating is not None else "N/A"
        downloads_str = str(p.download_count) if p.download_count is not None else "0"
        desc = p.description or "(no description)"
        content_summary_str = json.dumps(p.content_json, indent=2)

        parts = [
            f"Name: {p.name}",
            f"ID: {p.id}",
            f"Type: {p.primitive_type}",
            f"Version: {p.version}",
            f"Author: {p.author}",
            f"Tags: [{tags_str}]",
            f"Average Rating: {rating_str}",
            f"Download Count: {downloads_str}",
            f"Description: {desc}",
            f"\nContent Summary:\n{content_summary_str}",
        ]
        return "\n".join(parts)
    except Exception:
        _log.exception("resource_library_detail failed")
        return "error: Failed to get library primitive detail"


# ---------------------------------------------------------------------------
# Health check (mounted inside the MCP sub-app, before auth middleware)
# ---------------------------------------------------------------------------


def _mcp_healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# OAuth 2.0 protocol endpoints (mounted inside the MCP sub-app, before auth)
# ---------------------------------------------------------------------------


def _frontend_url(settings: Any) -> str:
    """Derive the SPA base URL from CORS_ORIGINS (first origin)."""
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    return origins[0] if origins else "http://localhost:5173"


def _oauth_authorize_param_errors(params: Mapping[str, str]) -> JSONResponse | None:
    """First validation error for the authorize query, or None. Runs in wire order."""
    if params.get("response_type", "") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if not params.get("client_id", "") or not params.get("redirect_uri", ""):
        return JSONResponse(
            {"error": "invalid_request", "detail": "client_id and redirect_uri required"},
            status_code=400,
        )
    if not params.get("state", ""):
        return JSONResponse(
            {"error": "invalid_request", "detail": "state parameter required"},
            status_code=400,
        )
    # S256-only (RFC 7636) — the challenge is verified at token exchange, so
    # rejecting plain/empty here keeps every stored challenge verifiable.
    code_challenge_method = params.get("code_challenge_method", "")
    try:
        from modulo.auth.oauth import InvalidGrantError, validate_pkce_method

        validate_pkce_method(code_challenge_method)
    except InvalidGrantError as exc:
        return JSONResponse(
            {"error": "invalid_request", "detail": str(exc)},
            status_code=400,
        )
    if not params.get("code_challenge", "") or not params.get("code_challenge", "").strip():
        return JSONResponse(
            {"error": "invalid_request", "detail": "code_challenge parameter required"},
            status_code=400,
        )
    return None


def _oauth_authorize_settings_error(settings: Any) -> JSONResponse | None:
    """Return an error response when the public URL is unconfigured."""
    if not settings.modulo_public_url or settings.modulo_public_url == "http://localhost:8000":
        return JSONResponse(
            {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
            status_code=500,
        )
    return None


async def _oauth_authorize(request: Request) -> JSONResponse | RedirectResponse:
    """GET /mcp/oauth/authorize — thin 302 to the SPA consent route.

    Anonymous (the browser is not yet authenticated against the SPA). Validates
    the request (client exists, exact-match redirect_uri, S256-only PKCE), then
    persists an ``oauth_consent_states`` row (account_id NULL until approve)
    and redirects the browser to ``/oauth/authorize?...`` on the SPA. The
    ``Referrer-Policy: no-referrer`` header keeps the client's query params
    from leaking to any third-party referer. The old POST handler that minted
    codes directly (anonymous, unbound) is DELETED — codes are only minted by
    the authenticated consent approve endpoint (ADR 017 DECISION 1).
    """
    params = request.query_params
    param_error = _oauth_authorize_param_errors(params)
    if param_error is not None:
        return param_error

    settings = get_settings()
    settings_error = _oauth_authorize_settings_error(settings)
    if settings_error is not None:
        return settings_error

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    scope = params.get("scope", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")

    from modulo.auth.oauth import (
        create_consent_state,
        get_oauth_client_by_client_id,
        normalize_scopes,
        validate_client_scopes,
    )

    try:
        session_factory = _get_session_factory()
        async with session_factory() as s, s.begin():
            # Look up client by globally unique client_id.
            client = await get_oauth_client_by_client_id(s, client_id)
            if client is None:
                return JSONResponse(
                    {"error": "invalid_client", "detail": "Unknown client_id"},
                    status_code=400,
                )

            allowed_uris = client.redirect_uris.split()
            if redirect_uri not in allowed_uris:
                return JSONResponse(
                    {"error": "invalid_client", "detail": "redirect_uri not allowed"},
                    status_code=400,
                )

            try:
                requested_scopes = normalize_scopes(scope)
                valid_scopes = validate_client_scopes(client, requested_scopes)
            except Exception as exc:
                return JSONResponse(
                    {"error": "invalid_scope", "detail": str(exc)},
                    status_code=400,
                )

            # Set RLS context for the client's org before creating records.
            await set_rls_org(s, client.organisation_id)
            await create_consent_state(
                s,
                state=state,
                client_id=client_id,
                redirect_uri=redirect_uri,
                scopes=valid_scopes,
                code_challenge=code_challenge,
                org_id=client.organisation_id,
            )
    except ProgrammingError:
        _log.warning("mcp_oauth.authorize.programming_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning("mcp_oauth.authorize.sqlalchemy_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.authorize.unexpected_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )

    consent_url = (
        f"{_frontend_url(settings)}/oauth/authorize"
        f"?client_id={quote(client_id)}"
        f"&redirect_uri={quote(redirect_uri)}"
        f"&state={quote(state)}"
        f"&code_challenge={quote(code_challenge)}"
    )
    redirect = RedirectResponse(consent_url, status_code=302)
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


async def _parse_oauth_form(request: Request) -> tuple[dict[str, str] | None, JSONResponse | None]:
    """Parse an RFC 6749 request body into a string dict.

    Accepts form-urlencoded (``request.form()``) and JSON bodies for backwards
    compatibility; anything else is ``invalid_request``. Returns
    ``(params, error)`` — exactly one is non-None.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        form = await request.form()
        params: dict[str, str] = {k: (str(v) if v is not None else "") for k, v in form.items()}
        return params, None
    if content_type == _CT_APPLICATION_JSON:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return None, JSONResponse(
                {"error": "invalid_request", "detail": "Request body must be JSON"},
                status_code=400,
            )
        params = {k: str(v) if v is not None else "" for k, v in body.items()}
        return params, None
    return None, JSONResponse(
        {"error": "invalid_request", "detail": "Content-Type must be application/x-www-form-urlencoded"},
        status_code=400,
    )


def _extract_oauth_client_credentials(
    request: Request, params: dict[str, str]
) -> tuple[dict[str, str], JSONResponse | None]:
    """Extract and validate the client credentials for an authorization-code grant.

    Validates ``grant_type`` is ``authorization_code`` and reads
    ``code``/``redirect_uri``/``client_id``/``code_verifier``/``client_secret``
    from the body, falling back to an HTTP Basic Authorization header for
    ``client_secret``/``client_id`` (RFC 6749 §2.3.1). Returns
    ``(creds, error)`` where ``creds`` has the six keys — exactly one of the
    tuple is non-None only on error.
    """
    grant_type = params.get("grant_type", "")
    if grant_type != "authorization_code":
        return {}, JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    code = params.get("code", "")
    redirect_uri = params.get("redirect_uri", "")
    client_id = params.get("client_id", "")
    code_verifier = params.get("code_verifier", "")

    # client_secret may come from the body (RFC 6749) OR Basic auth.
    client_secret = params.get("client_secret", "")
    creds_delta, auth_err = _parse_basic_auth_header(request, params)
    if auth_err:
        return {}, auth_err
    client_id = creds_delta.get("client_id", client_id)
    client_secret = creds_delta.get("client_secret", client_secret)

    if not code or not redirect_uri or not client_id or not client_secret:
        return {}, JSONResponse(
            {"error": "invalid_request", "detail": "Missing required parameters"},
            status_code=400,
        )

    return (
        {
            "grant_type": grant_type,
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "client_secret": client_secret,
        },
        None,
    )


async def _exchange_authorization_code(
    creds: dict[str, str], settings: Any
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Exchange an authorization code for an access/refresh token pair.

    Runs the OAuth token exchange steps inside a DB transaction: validate the
    client secret, set RLS org context, consume the authorization code (PKCE
    verified inside), re-verify the consenting account's LIVE role against the
    granted scopes (ADR 017), create a token family, and mint the token pair.
    Returns ``(response_dict, error)`` — ``response_dict`` is the success body
    on success; OAuth/DB exceptions propagate to the caller's ``try/except``.
    """
    from modulo.auth.oauth import (
        consume_authorization_code,
        create_oauth_access_token,
        create_oauth_refresh_token,
        create_oauth_token_family,
        validate_client_secret,
        verify_live_role_covers_scopes,
    )

    session_factory = _get_session_factory()
    async with session_factory() as s, s.begin():
        # Step 1: Validate client credentials to discover org_id.
        client = await validate_client_secret(s, creds["client_id"], creds["client_secret"])

        # Step 2: Set RLS context for the client's org.
        await set_rls_org(s, client.organisation_id)

        # Step 3: Consume the authorization code (PKCE verified inside).
        auth_code = await consume_authorization_code(
            s,
            code=creds["code"],
            client_id=creds["client_id"],
            redirect_uri=creds["redirect_uri"],
            client_secret=creds["client_secret"],
            code_verifier=creds["code_verifier"],
        )

        # Step 4: The consenting account's LIVE role must still cover the
        # granted scopes — a demoted/removed account is denied (ADR 017).
        await verify_live_role_covers_scopes(
            s,
            account_id=auth_code.account_id,
            org_id=client.organisation_id,
            scopes=auth_code.scopes.split(),
        )

        # Step 5: Create a new token family.
        family_id, sequence = await create_oauth_token_family(
            s,
            client_id=creds["client_id"],
            org_id=client.organisation_id,
        )

        scopes_list = auth_code.scopes.split()
        access_token = create_oauth_access_token(
            creds["client_id"],
            settings.secret_key,
            organisation_id=str(client.organisation_id),
            account_id=str(auth_code.account_id),
            scopes=scopes_list,
            token_family=family_id,
            token_sequence=sequence,
        )
        refresh_token = create_oauth_refresh_token(
            creds["client_id"],
            settings.secret_key,
            organisation_id=str(client.organisation_id),
            account_id=str(auth_code.account_id),
            scopes=scopes_list,
            token_family=family_id,
            token_sequence=sequence,
        )

    return (
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",  # nosec B105 - RFC 6750 token_type label, not a credential
            "expires_in": 3600,
            "scope": " ".join(scopes_list),
        },
        None,
    )


async def _oauth_token_impl(request: Request) -> JSONResponse:
    params, parse_err = await _parse_oauth_form(request)
    if parse_err:
        return parse_err
    if params is None:
        raise RuntimeError("_oauth_token_impl: form parse returned no error and no params")

    creds, cred_err = _extract_oauth_client_credentials(request, params)
    if cred_err:
        return cred_err

    settings = get_settings()
    if not settings.modulo_public_url or settings.modulo_public_url == "http://localhost:8000":
        return JSONResponse(
            {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
            status_code=500,
        )

    token_resp, token_err = await _exchange_authorization_code(creds, settings)
    if token_err:
        return token_err
    if token_resp is None:
        raise RuntimeError("_oauth_token_impl: token exchange returned no error and no response")
    return JSONResponse(token_resp)


async def _oauth_token(request: Request) -> JSONResponse:
    """POST /mcp/oauth/token — exchange code for access token.

    RFC 6749 wire format: form-urlencoded bodies (``request.form()``) with JSON
    bodies accepted for backwards compatibility; anything else is
    ``invalid_request``. The PKCE ``code_verifier`` is required and verified
    against the stored S256 challenge (RFC 7636 §4.5/§4.6). ``client_secret``
    may arrive in the form body OR an HTTP Basic Authorization header. The
    consenting account's LIVE org role is re-verified against the granted
    scopes — a demoted account is denied a token (ADR 017).
    """
    from modulo.auth.oauth import (
        InvalidClientError,
        InvalidGrantError,
    )

    try:
        return await _oauth_token_impl(request)
    except (InvalidGrantError, InvalidClientError):
        return JSONResponse(
            {"error": "invalid_grant", "detail": "Authorization code exchange failed"},
            status_code=400,
        )
    except StarletteHTTPException as e:
        return JSONResponse(
            {"error": "server_error" if e.status_code >= 500 else "invalid_request", "detail": e.detail},
            status_code=e.status_code,
        )
    except ProgrammingError:
        _log.warning("mcp_oauth.token.programming_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning("mcp_oauth.token.sqlalchemy_error")
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.token.unexpected_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )


def _extract_oauth_refresh_credentials(
    request: Request, params: dict[str, str]
) -> tuple[dict[str, str], JSONResponse | None]:
    """Extract and validate the client credentials for a refresh-token grant.

    Validates ``grant_type`` is ``refresh_token`` and reads
    ``refresh_token``/``client_id``/``client_secret`` from the body, falling
    back to an HTTP Basic Authorization header for the client secret/ID
    (RFC 6749 §2.3.1). Returns ``(creds, error)`` — exactly one is non-None on
    error.
    """
    grant_type = params.get("grant_type", "")
    if grant_type != "refresh_token":
        return {}, JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    refresh_token_value = params.get("refresh_token", "")
    client_id = params.get("client_id", "")
    client_secret = params.get("client_secret", "")

    creds_delta, auth_err = _parse_basic_auth_header(request, params)
    if auth_err:
        return {}, auth_err
    client_id = creds_delta.get("client_id", client_id)
    client_secret = creds_delta.get("client_secret", client_secret)

    if not refresh_token_value or not client_id or not client_secret:
        return {}, JSONResponse(
            {"error": "invalid_request", "detail": "refresh_token, client_id and client_secret are required"},
            status_code=400,
        )

    return (
        {
            "grant_type": grant_type,
            "refresh_token": refresh_token_value,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        None,
    )


async def _exchange_refresh_token(
    creds: dict[str, str], settings: Any
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Rotate a refresh token into a new access/refresh token pair.

    Validates the client secret, sets RLS org context, decodes the refresh
    token, re-verifies the consenting account's LIVE role still covers the
    token's scopes (ADR 017), then issues a new pair with an incremented
    sequence — invalidating the old refresh token. Returns
    ``(response_dict, error)``; OAuth/DB exceptions propagate to the caller.
    """
    from modulo.auth.oauth import (
        create_oauth_access_token,
        create_oauth_refresh_token,
        decode_oauth_refresh_token,
        validate_client_secret,
        verify_live_role_covers_scopes,
    )

    session_factory = _get_session_factory()
    async with session_factory() as s, s.begin():
        client = await validate_client_secret(s, creds["client_id"], creds["client_secret"])
        await set_rls_org(s, client.organisation_id)

        claims = decode_oauth_refresh_token(creds["refresh_token"], settings.secret_key)

        # ADR 017: the consenting account's LIVE role must still cover the
        # scopes — a demoted/removed account is denied a fresh token.
        await verify_live_role_covers_scopes(
            s,
            account_id=claims.account_id,
            org_id=claims.organisation_id,
            scopes=claims.scopes,
        )

        new_sequence = claims.token_sequence + 1
        new_access_token = create_oauth_access_token(
            claims.client_id,
            settings.secret_key,
            organisation_id=str(claims.organisation_id),
            account_id=str(claims.account_id),
            scopes=claims.scopes,
            token_family=claims.token_family,
            token_sequence=new_sequence,
        )
        new_refresh_token = create_oauth_refresh_token(
            claims.client_id,
            settings.secret_key,
            organisation_id=str(claims.organisation_id),
            account_id=str(claims.account_id),
            scopes=claims.scopes,
            token_family=claims.token_family,
            token_sequence=new_sequence,
        )

    return (
        {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",  # nosec B105 - RFC 6750 token_type label, not a credential
            "expires_in": 3600,
            "scope": " ".join(claims.scopes),
        },
        None,
    )


async def _oauth_refresh_impl(request: Request) -> JSONResponse:
    params, parse_err = await _parse_oauth_form(request)
    if parse_err:
        return parse_err
    if params is None:
        raise RuntimeError("_oauth_refresh_impl: form parse returned no error and no params")

    creds, cred_err = _extract_oauth_refresh_credentials(request, params)
    if cred_err:
        return cred_err

    settings = get_settings()
    token_resp, token_err = await _exchange_refresh_token(creds, settings)
    if token_err:
        return token_err
    if token_resp is None:
        raise RuntimeError("_oauth_refresh_impl: token exchange returned no error and no response")
    return JSONResponse(token_resp)


async def _oauth_refresh(request: Request) -> JSONResponse:
    """POST /mcp/oauth/refresh — exchange refresh token for new access token.

    Form-urlencoded per RFC 6749 with JSON compat, mirroring ``_oauth_token``.
    Re-verifies the client secret (body or Basic auth) and the consenting
    account's LIVE org role against the token's scopes — if the account was
    demoted (or removed) since the token was issued, the refresh is DENIED
    (ADR 017 demote-then-refresh). The refresh token is rotated: a new pair is
    issued with an incremented sequence, invalidating the old refresh token.
    """
    from modulo.auth.oauth import (
        InvalidClientError,
        InvalidGrantError,
    )

    try:
        return await _oauth_refresh_impl(request)
    except (InvalidGrantError, InvalidClientError):
        return JSONResponse(
            {"error": "invalid_grant", "detail": "Refresh token exchange failed"},
            status_code=400,
        )
    except (ValueError, JWTError) as exc:
        return JSONResponse(
            {"error": "invalid_grant", "detail": str(exc)},
            status_code=400,
        )
    except StarletteHTTPException as e:
        return JSONResponse(
            {"error": "server_error" if e.status_code >= 500 else "invalid_request", "detail": e.detail},
            status_code=e.status_code,
        )
    except ProgrammingError:
        _log.warning("mcp_oauth.refresh.programming_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning("mcp_oauth.refresh.sqlalchemy_error")
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.refresh.unexpected_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Build the mounted ASGI app (called from main.py)
# ---------------------------------------------------------------------------


def _mcp_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unhandled MCP exceptions and return a structured JSON error.

    Starlette's ``ServerErrorMiddleware`` (outermost in the middleware stack)
    catches unhandled exceptions and calls this handler instead of the default
    ``PlainTextResponse("Internal Server Error")`` — making errors observable
    in production logs for the first time.
    """
    _log.exception(
        "mcp.unhandled_exception",
        extra={
            "method": request.method,
            "path": str(request.url.path),
            "exc_type": type(exc).__name__,
            "exc_repr": str(exc),
            "traceback": _traceback.format_exc(),
        },
    )
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": _MSG_UNEXPECTED_ERROR},
    )


def build_mcp_asgi_app() -> Starlette:
    """Return the MCP Starlette app wrapped with auth middleware."""
    inner = mcp.streamable_http_app()

    # Mount an in-sub-app health check for orchestrators / load balancers.
    health_route = Route("/healthz", _mcp_healthz, methods=["GET"])

    # OAuth protocol endpoints — placed before auth middleware so they
    # don't require a Bearer token (authorize is an anonymous browser 302;
    # token/refresh authenticate via client_id + client_secret).
    oauth_authorize_route = Route("/oauth/authorize", _oauth_authorize, methods=["GET"])
    oauth_token_route = Route("/oauth/token", _oauth_token, methods=["POST"])
    oauth_refresh_route = Route("/oauth/refresh", _oauth_refresh, methods=["POST"])

    all_routes = [
        health_route,
        oauth_authorize_route,
        oauth_token_route,
        oauth_refresh_route,
        *list(inner.routes),
    ]
    return Starlette(
        routes=all_routes,
        middleware=[
            Middleware(McpAuthMiddleware),
            Middleware(RateLimiterMiddleware),  # type: ignore[arg-type]
        ],
        exception_handlers={Exception: _mcp_exception_handler},
        # Note: lifespan is managed by the parent FastAPI app's _lifespan
        # to ensure it is called — Starlette does not invoke sub-app lifespans.
    )
