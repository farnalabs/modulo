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
import threading
import time
import traceback as _traceback
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from jwt import InvalidTokenError as JWTError
from mcp.server.fastmcp import FastMCP
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
from modulo.core.mcp.scope_validator import MCPAuthorizationError, check_tool_scope
from modulo.core.pipeline_engine.error_codes import map_legacy_code, present_error
from modulo.core.rate_limiter import TokenBucketRegistry
from modulo.core.trigger_streak import (
    anchor_trigger_streak_epoch,
    clear_trigger_streak_after_reenable,
)
from modulo.db.crud.hitl_gate_guard import HitlGateWeakeningDenied
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
_MSG_TOKEN_REVOKED = "Token revoked or expired - re-authenticate"
_MSG_ERROR_TOKEN_REVOKED = "error: Token revoked or expired - re-authenticate"
_MSG_DB_MIGRATION_REQUIRED = "Database migration required. Run `alembic upgrade head`."
_MSG_DB_MIGRATION_REQUIRED_HEADS = "Database migration required. Run alembic upgrade heads."
_MSG_TRIGGER_NOT_FOUND = "Trigger not found"
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


def _sanitize_mcp_mapping(value: dict) -> dict:
    return {k: _sanitize_mcp_basis_value(v) for k, v in value.items()}


def _sanitize_mcp_sequence(value: list) -> list:
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
        sanitized.append(out)
    return sanitized


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
        # Allow unauthenticated access to the health check endpoint.
        clean = request.url.path.rstrip("/")
        if clean in ("/mcp/healthz", "/healthz"):
            resp: Response = await call_next(request)
            return resp

        # Allow unauthenticated access to the OAuth protocol endpoints.
        # These endpoints manage their own auth via client_id + client_secret.
        if clean in ("/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/refresh"):
            resp2: Response = await call_next(request)
            return resp2

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return Response(
                '{"error":"unauthorized","detail":"Bearer token required"}',
                status_code=401,
                media_type=_CT_APPLICATION_JSON,
            )
        token = auth_header[len("Bearer ") :].strip()

        # Try API key first (backwards compatible).
        if token.startswith("mk_"):
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
                    raise ApiKeyInvalidError()

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
                        raise ApiKeyInvalidError()
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
                request.scope["auth_principal"] = {
                    "type": "api_key",
                    "org_id": str(org_id),
                    "prefix": token[3:11],
                }
            except ApiKeyInvalidError:
                return Response(
                    '{"error":"unauthorized","detail":"Invalid or revoked API key"}',
                    status_code=401,
                    media_type=_CT_APPLICATION_JSON,
                )
            except (SQLAlchemyError, OperationalError, TimeoutError):
                _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
                return Response(
                    _JSON_AUTH_DB_UNAVAILABLE,
                    status_code=503,
                    media_type=_CT_APPLICATION_JSON,
                )
            await _set_authz_enforce(org_id)
            resp3: Response = await call_next(request)
            return resp3

        # Try OAuth access token (JWT).
        settings = get_settings()
        try:
            claims = decode_oauth_access_token(token, settings.secret_key)
        except JWTError:
            # Fall back to regular JWT access token (used by Remy MCP tool calls).
            try:
                from modulo.auth.jwt import decode_principal

                principal = decode_principal(token, settings.secret_key)
            except JWTError:
                return Response(
                    '{"error":"unauthorized","detail":"Invalid or expired access token"}',
                    status_code=401,
                    media_type=_CT_APPLICATION_JSON,
                )
            if principal.organisation_id is None:
                return Response(
                    _JSON_FORBIDDEN_ORG_MEMBERSHIP,
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                )
            # ADR 017: no claim-less default-up. A None role claim fails closed,
            # and the LIVE role is re-read from org_memberships so a demoted or
            # removed member loses access on the very next request.
            if principal.org_role is None:
                return Response(
                    '{"error":"forbidden","detail":"No org role claim on token"}',
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                )
            try:
                async with _session(principal.organisation_id) as s:
                    live_role = await resolve_role_from_membership(
                        s,
                        str(principal.account_id),
                        str(principal.organisation_id),
                    )
            except (SQLAlchemyError, OperationalError, TimeoutError):
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
            await _set_authz_enforce(principal.organisation_id)
            resp4: Response = await call_next(request)
            return resp4

        # Verify token family is not blacklisted.
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
        except (SQLAlchemyError, OperationalError, TimeoutError):
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

        # Resolve role from scopes (highest scope wins) — ADR 017: the scope
        # grant is then CLAMPED to the account's LIVE org role so a demoted
        # operator loses scope on the very next call. Fail-closed: a DB read
        # failure or missing/deactivated membership denies.
        scope_role = scopes_required_role(claims.scopes)
        try:
            async with _session(claims.organisation_id) as s:
                live_role = await resolve_role_from_membership(
                    s,
                    str(claims.account_id),
                    str(claims.organisation_id),
                )
        except (SQLAlchemyError, OperationalError, TimeoutError):
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
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Modulo",
    instructions=(
        "Modulo is governed orchestration for your agentic SDLC. "
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
        check_tool_scope(_ctx_role_val(), "create_pipeline")
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
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "list_runs")
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
        items = []
        for r in result.items:
            child_cost, child_count = child_rollup.get(r.id, (_MCP_COST_ROLLUP_ZERO, 0))
            child_cost = _quantize_mcp_cost_rollup(child_cost)
            own_cost = r.total_cost_usd if r.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
            _error_code, error_detail = present_error(r.error_code, r.error_detail, limit=200)
            items.append(
                {
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
            )
        return {
            "items": items,
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_runs failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_runs failed")
        return _tool_error("Failed to list runs")


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
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "query_analytics")

        org_id = _ctx_org_id_val()
        settings = get_settings()

        # analytics_page feature gate — mirror the REST route's require_feature.
        from modulo.core.feature_flags import resolve_plan_context
        from modulo.db.crud.organisation import get_organisation

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)
        async with _session(org_id) as s:
            plan_ctx = await resolve_plan_context(settings, s, org)
        if not plan_ctx.feature_enabled("analytics_page"):
            return {"error": "feature_required", "detail": "analytics_page is not available on your plan"}

        try:
            dim = AnalyticsDimension(dimension) if dimension is not None else None
            grp = AnalyticsGroupBy(group_by)
            tt = AnalyticsTriggerType(trigger_type) if trigger_type is not None else None
            st = AnalyticsStatus(status) if status is not None else None
        except ValueError:
            return {
                "error": "invalid_params",
                "detail": f"invalid enum value (dimension={dimension!r} group_by={group_by!r})",
            }

        pids: tuple[uuid.UUID, ...] = ()
        if pipeline_id:
            try:
                pids = tuple(uuid.UUID(p) for p in pipeline_id)
            except ValueError:
                return {"error": "invalid_params", "detail": "pipeline_id entries must be valid UUIDs"}

        fid: uuid.UUID | None = None
        if folder_id is not None:
            try:
                fid = uuid.UUID(folder_id)
            except ValueError:
                return {"error": "invalid_params", "detail": f"Invalid folder_id UUID: {folder_id}"}

        params = AnalyticsParams(
            group_by=grp,
            auto_granularity=auto_granularity,
            dimension=dim,
            trigger_type=tt,
            status=st,
            pipeline_ids=pids,
            team_id=_ctx_team_id_val(),
            error_code=error_code,
            folder_id=fid,
            date_from=_parse_mcp_datetime(date_from, "date_from") if date_from is not None else None,
            date_to=_parse_mcp_datetime(date_to, "date_to") if date_to is not None else None,
            limit=max(1, min(limit, 1000)),
        )
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
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "query_analytics_concurrency")

        org_id = _ctx_org_id_val()
        settings = get_settings()

        # analytics_page feature gate — mirror the REST route's require_feature.
        from modulo.core.feature_flags import resolve_plan_context
        from modulo.db.crud.organisation import get_organisation

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)
        async with _session(org_id) as s:
            plan_ctx = await resolve_plan_context(settings, s, org)
        if not plan_ctx.feature_enabled("analytics_page"):
            return {"error": "feature_required", "detail": "analytics_page is not available on your plan"}

        try:
            grp = AnalyticsGroupBy(group_by)
            tt = AnalyticsTriggerType(trigger_type) if trigger_type is not None else None
            st = AnalyticsStatus(status) if status is not None else None
        except ValueError:
            return {
                "error": "invalid_params",
                "detail": f"invalid enum value (group_by={group_by!r})",
            }

        pids: tuple[uuid.UUID, ...] = ()
        if pipeline_id:
            try:
                pids = tuple(uuid.UUID(p) for p in pipeline_id)
            except ValueError:
                return {"error": "invalid_params", "detail": "pipeline_id entries must be valid UUIDs"}

        fid: uuid.UUID | None = None
        if folder_id is not None:
            try:
                fid = uuid.UUID(folder_id)
            except ValueError:
                return {"error": "invalid_params", "detail": f"Invalid folder_id UUID: {folder_id}"}

        params = AnalyticsParams(
            group_by=grp,
            auto_granularity=auto_granularity,
            dimension=None,
            trigger_type=tt,
            status=st,
            pipeline_ids=pids,
            team_id=_ctx_team_id_val(),
            error_code=None,
            folder_id=fid,
            date_from=_parse_mcp_datetime(date_from, "date_from") if date_from is not None else None,
            date_to=_parse_mcp_datetime(date_to, "date_to") if date_to is not None else None,
            limit=max(1, min(limit, 1000)),
        )
        return await run_concurrency_query(
            org_id=org_id,
            params=params,
            factory=_get_session_factory(),
            settings=settings,
            account_id=_ctx_user_id_val(),
            org_role=_ctx_role_val() or "",
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
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            result = await get_pipeline_graph(s, pid)

        if result is None:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

        nodes, edges = result
        edge_dicts = [
            {
                "id": str(e.id),
                "source_node_id": str(e.source_node_id),
                "target_node_id": str(e.target_node_id),
                "edge_type": e.edge_type,
            }
            for e in edges
        ]

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
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "update_pipeline_graph")
        from modulo.core.team_visibility import (
            CONNECTOR_TEAM_MISMATCH,
            connector_team_mismatch_detail,
            extract_connector_bindings,
            find_connector_team_mismatches,
        )
        from modulo.db.crud.pipeline import replace_pipeline_graph

        org_id = _ctx_org_id_val()
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

        # ADR 017 service-layer backstop + hitl-gate-removal-guard-plan.md v19:
        # the MCP surface is structurally excluded from gate weakening. The
        # guarded function hardcodes is_privileged=False when
        # caller_type=="mcp" (no DB query); the literal below is enforced by a
        # .semgrep/ rule (mcp call site must pass the literal, not a variable).
        from modulo.api.routes.pipelines import PipelineGraphUpdate, _is_privileged

        is_privileged = _is_privileged(_ctx_role_val())

        # Validate graph structure using Pydantic models (same as REST endpoint)
        from pydantic import ValidationError as _PydanticValidationError

        try:
            PipelineGraphUpdate.model_validate({"nodes": nodes, "edges": edges})
        except _PydanticValidationError as exc:
            return {
                "error": "validation_failed",
                "detail": f"Graph validation failed: {exc.errors(include_url=False)}",
            }

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
                result = await replace_pipeline_graph(
                    s,
                    pipeline_id=pid,
                    org_id=org_id,
                    nodes=nodes,
                    edges=edges,
                    is_privileged=is_privileged,
                    caller_type="mcp",
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

        return {
            "pipeline_id": pipeline_id,
            "nodes": updated_nodes,
            "edges": [
                {
                    "id": str(e.id),
                    "source_node_id": str(e.source_node_id),
                    "target_node_id": str(e.target_node_id),
                    "edge_type": e.edge_type,
                }
                for e in updated_edges
            ],
            "node_count": len(updated_nodes),
            "edge_count": len(updated_edges),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("update_pipeline_graph failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("update_pipeline_graph failed")
        return _tool_error("Failed to update pipeline graph")


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
        check_tool_scope(_ctx_role_val(), "bind_connector_to_node")

        from modulo.db.crud.connector_instance import get_connector_instance

        org_id = _ctx_org_id_val()
        try:
            pid = uuid.UUID(pipeline_id)
            nid = uuid.UUID(node_id)
            cid = uuid.UUID(connector_instance_id)
        except ValueError:
            return {"error": "invalid_id", "detail": "One or more IDs have invalid UUID format"}

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


@mcp.tool(description="Fire a pipeline run and return immediately with run_id. Poll get_run_status to track progress.")
@_RETRY_DB
async def trigger_pipeline(
    pipeline_id: str,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "trigger_pipeline")
        if not await _trigger_pipeline_rate_allowed():
            _log.warning(
                "ratelimit.trigger_pipeline_exceeded",
                extra={"client_key": _trigger_pipeline_client_key()},
            )
            return {"error": "rate_limited", "detail": "Rate limit exceeded for trigger_pipeline (60/min)"}
        from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
        from modulo.db.crud.run import create_run

        org_id = _ctx_org_id_val()
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}
        payload = input_payload or {}

        async with _session(org_id) as s:
            pipeline = await get_pipeline(s, pid)
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            if _team_scoped_key_mismatch(pipeline.owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            uid = _ctx_user_id_val()
            snapshot = await create_snapshot_from_live_graph(s, pipeline_id=pid, account_id=uid)
            if snapshot is None:
                return {"error": "snapshot_failed", "pipeline_id": pipeline_id}
            if not snapshot.graph_json or not snapshot.graph_json.get("nodes"):
                return {
                    "error": "validation_failed",
                    "detail": "Pipeline graph has no nodes — cannot trigger run",
                }
            run = await create_run(
                s,
                org_id=org_id,
                pipeline_id=pid,
                snapshot_id=snapshot.id,
                trigger_type="manual",
                input_payload=payload,
            )
            run_id = run.id
            thread_id = run.langgraph_thread_id

        await dispatch_run(str(run_id), str(org_id), queue="runs")

        return {
            "run_id": str(run_id),
            "status": "pending",
            "langgraph_thread_id": thread_id,
        }
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
    except ProgrammingError:
        _log.exception("trigger_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("trigger_pipeline failed")
        return _tool_error("Failed to trigger pipeline")


@mcp.tool(description="Get current run status. Pass detail=true for per-node breakdown.")
@_RETRY_DB
async def get_run_status(run_id: str, detail: bool = False) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        org_id = _ctx_org_id_val()
        try:
            rid = uuid.UUID(run_id)
        except ValueError:
            return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}
        async with _session(org_id) as s:
            run = await get_run(s, rid)
            if run is None:
                return {"error": "run_not_found", "run_id": run_id}
            # The run carries its own owner_team_id (snapshot at creation) — that
            # is the source of truth, not the pipeline's current team assignment.
            # Legacy runs with a NULL stamp fall back to the pipeline owner.
            run_owner_team_id = await _run_owner_team_id(s, run)
        if _team_scoped_key_mismatch(run_owner_team_id):
            return _team_scope_error("run", run_id)
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
        if detail:
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
            nodes: list[dict[str, Any]] = []
            for nid in sorted(node_ids):
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
                nodes.append(node)
            result["nodes"] = nodes
            if run.cost_breakdown is not None:
                result["cost_breakdown"] = _sanitize_cost_breakdown(run.cost_breakdown)
        return result
    except ProgrammingError:
        _log.exception("get_run_status failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_run_status failed")
        return _tool_error("Failed to get run status")


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
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "get_run_output")
        from modulo.api.routes.runs import _mask_output_value
        from modulo.core.node_output_split import node_return, node_telemetry

        org_id = _ctx_org_id_val()
        try:
            rid = uuid.UUID(run_id)
        except ValueError:
            return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}
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
        node_output = node_return(outputs, telemetry, node_id)
        if node_output is None:
            node_meta = node_telemetry(telemetry, outputs, node_id)
            if isinstance(node_meta, dict):
                node_output = {key: node_meta[key] for key in ("status", "summary") if key in node_meta}
        if node_output is None:
            return {"error": "node_output_not_found", "run_id": run_id, "node_id": node_id}
        masked = _mask_output_value(node_output)

        # Detect masked fields by scanning for the bullet mask character.
        masked_fields: list[str] = []
        if isinstance(masked, dict):
            masked_fields = [k for k, v in masked.items() if isinstance(v, str) and "\u2022" in v]

        return {
            "node_id": node_id,
            "output": masked,
            "masked_fields": masked_fields,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_output failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_run_output failed")
        return _tool_error("Failed to get node output")


@mcp.tool(
    description="Get eval results for a given run. Returns structured eval outcomes "
    "including pass/fail status, scores, and detailed feedback.",
)
@_RETRY_DB
async def get_run_evals(run_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "get_run_evals")
        from modulo.db.crud.eval_run import get_run_evals as db_get_run_evals

        org_id = _ctx_org_id_val()
        try:
            rid = uuid.UUID(run_id)
        except ValueError:
            return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}

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
            "evals": [
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
            ],
            "eval_count": len(evals),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_evals failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_run_evals failed")
        return _tool_error("Failed to get run evals")


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
        check_tool_scope(_ctx_role_val(), "list_eval_definitions")
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


@mcp.tool(description="Cancel a running pipeline run.")
@_RETRY_DB
async def cancel_run(run_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "cancel_run")
        from modulo.db.crud.run import request_cancellation

        org_id = _ctx_org_id_val()
        try:
            rid = uuid.UUID(run_id)
        except ValueError:
            return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}
        async with _session(org_id) as s:
            from modulo.db.crud.run import get_run

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
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("cancel_run failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("cancel_run failed")
        return _tool_error("Failed to cancel run")


@mcp.tool(description="List all pending (undecided) HITL gates across all runs.")
@_RETRY_DB
async def list_pending_hitl(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "list_pending_hitl")
        from sqlalchemy import func, select

        from modulo.db.models.pipeline import Pipeline

        terminal_statuses = TERMINAL_STATUSES
        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            base_where: list[Any] = [
                HitlClaim.organisation_id == org_id,
                HitlClaim.decision.is_(None),
                Run.status.not_in(terminal_statuses),
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
            gates = list(result.scalars())
        return {
            "gates": [
                {
                    "run_id": str(g.run_id),
                    "gate_id": g.gate_id,
                    "pipeline_id": str(g.pipeline_id),
                    "claimed_by": str(g.account_id) if g.account_id else None,
                    "expires_at": g.expires_at.isoformat() if g.expires_at else None,
                    "required_team_id": str(g.required_team_id) if g.required_team_id else None,
                }
                for g in gates
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": (page * page_size) < total,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_pending_hitl failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("list_pending_hitl failed")
        return _tool_error("Failed to list pending HITL gates")


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
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)

    from sqlalchemy import select

    org_id = _ctx_org_id_val()
    key_id = _ctx_key_id.get(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}
    mgr = HITLManager()

    if action not in ("claim", "approve", "reject", "deliver_manual"):
        return {"error": "invalid_action", "detail": "action must be claim, approve, reject, or deliver_manual"}

    try:
        check_tool_scope(_ctx_role_val(), "review_hitl", action=action)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}

    if action == "approve" and claim_token is None:
        return {"error": "claim_token_required", "detail": "approve requires claim_token"}
    if action == "reject" and claim_token is None:
        return {"error": "claim_token_required", "detail": "reject requires claim_token"}
    if action == "deliver_manual" and claim_token is None:
        return {"error": "claim_token_required", "detail": "deliver_manual requires claim_token"}
    if action == "deliver_manual" and output is None:
        return {"error": "output_required", "detail": "deliver_manual requires output dict"}

    try:
        async with _session(org_id) as s:
            # Team boundary: a team-scoped key must not act on a run owned by
            # another team, even when the gate itself is org-level
            # (required_team_id IS NULL). The run's effective owner is the
            # source of truth with pipeline fallback for pre-stamp runs.
            run = await get_run(s, rid)
            if run is None:
                return {"error": "gate_not_found", "run_id": run_id, "gate_id": gate_id}
            if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
                return _team_scope_error("run", run_id)

            # Check human_only for approve action
            if action == "approve":
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

            try:
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
            except GateNotFoundError:
                return {"error": "gate_not_found", "run_id": run_id, "gate_id": gate_id}
            except NotTeamMemberError:
                return {"error": "not_team_member", "detail": "You are not a member of the team required by this gate"}
            except AlreadyClaimedError:
                return {"error": "already_claimed", "detail": "Gate is already held by another client"}
            except ClaimTokenInvalidError:
                return {"error": "claim_token_invalid"}
            except ClaimTokenExpiredError:
                return {"error": "claim_token_expired", "detail": "Re-claim the gate"}
            except GateAlreadyDecidedError:
                return {"error": "already_decided", "detail": "Gate already has a final decision"}
            except ProgrammingError:
                _log.exception("review_hitl failed")
                return {"error": "migration_required", "detail": "DB migration required. Run alembic upgrade head."}
            except Exception:
                _log.exception("review_hitl failed")
                return _tool_error("Failed to process HITL action")
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
        check_tool_scope(_ctx_role_val(), "copy_library_primitive")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(primitive_id)
    except ValueError:
        return {"error": "invalid_id", "field": "primitive_id", "detail": f"Invalid UUID format: {primitive_id}"}

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
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "list_trigger_events")
        from sqlalchemy import func, select

        from modulo.db.crud.pagination import CursorPaginator
        from modulo.db.models.pipeline import Pipeline as _Pipeline
        from modulo.db.models.trigger import Trigger
        from modulo.db.models.trigger_event import TriggerEvent

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            key_team_id = _ctx_team_id_val()
            q = select(TriggerEvent).where(TriggerEvent.organisation_id == org_id)
            joined = False

            if trigger_id is not None:
                try:
                    tid = uuid.UUID(trigger_id)
                except ValueError:
                    return {
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
                        return _team_scope_error("trigger", str(tid))
                    if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, trigger.pipeline_id)):
                        return _team_scope_error("pipeline", str(trigger.pipeline_id))

            if pipeline_id is not None:
                try:
                    pid = uuid.UUID(pipeline_id)
                except ValueError:
                    return {
                        "error": "invalid_id",
                        "field": "pipeline_id",
                        "detail": f"Invalid UUID format: {pipeline_id}",
                    }
                if key_team_id is not None:
                    owner_team_id = await _pipeline_owner_team_id(s, pid)
                    if _team_scoped_key_mismatch(owner_team_id):
                        return _team_scope_error("pipeline", str(pid))
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

            total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one_or_none() or 0

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

        return {
            "data": [
                {
                    "id": str(e.id),
                    "trigger_id": str(e.trigger_id),
                    "trigger_type": e.trigger_type,
                    "validation_result": e.validation_result,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                    "run_id": str(e.run_id) if e.run_id else None,
                }
                for e in items
            ],
            "total": total,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
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
        check_tool_scope(_ctx_role_val(), "list_triggers")
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
        check_tool_scope(_ctx_role_val(), "create_model_backend")

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
        check_tool_scope(_ctx_role_val(), "create_connector")

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
    from datetime import UTC

    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "create_trigger")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

        if max_concurrent_runs < 1:
            return {"error": "validation", "field": "max_concurrent_runs", "detail": "must be >= 1"}
        if daily_spend_limit is not None and daily_spend_limit < 0:
            return {"error": "validation", "field": "daily_spend_limit", "detail": "must be >= 0"}

        from modulo.core.trigger_validation import validate_ongoing_config
        from modulo.db.models.pipeline import Pipeline
        from modulo.db.models.trigger import Trigger

        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            next_fire_at = None
            if trigger_type == "ongoing":
                # FAR-158: identical guards to the REST create surface.
                from datetime import UTC

                from fastapi import HTTPException

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
                    return {"error": "validation", "detail": str(exc.detail)}
                next_fire_at = datetime.now(UTC)
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
                    return {"error": "invalid_cron", "detail": error}
                trigger.next_fire_at = compute_next_fire(cron_expression, timezone=trigger.cron_timezone or "UTC")
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
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("create_trigger failed")
        return _tool_error("Failed to create trigger")


@mcp.tool(description="Get a single trigger by ID.")
@_RETRY_DB
async def get_trigger(trigger_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "get_trigger")

        org_id = _ctx_org_id_val()
        try:
            tid = uuid.UUID(trigger_id)
        except ValueError:
            return {"error": "invalid_id", "field": "trigger_id", "detail": f"Invalid UUID format: {trigger_id}"}

        from sqlalchemy import select

        from modulo.core.cron_helpers import _count_ongoing_runs
        from modulo.db.models.trigger import Trigger

        async with _session(org_id) as s:
            q = select(Trigger).where(
                Trigger.id == tid,
                Trigger.organisation_id == org_id,
                Trigger.deleted_at.is_(None),
            )
            trigger = (await s.execute(q)).scalar_one_or_none()
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
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception:
        _log.exception("get_trigger failed")
        return _tool_error("Failed to get trigger")


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
        check_tool_scope(_ctx_role_val(), "update_trigger")

        org_id = _ctx_org_id_val()
        try:
            tid = uuid.UUID(trigger_id)
        except ValueError:
            return {"error": "invalid_id", "field": "trigger_id", "detail": f"Invalid UUID format: {trigger_id}"}

        if max_concurrent_runs is not None and max_concurrent_runs < 1:
            return {"error": "validation", "field": "max_concurrent_runs", "detail": "must be >= 1"}
        if daily_spend_limit is not None and daily_spend_limit < 0:
            return {"error": "validation", "field": "daily_spend_limit", "detail": "must be >= 0"}

        from sqlalchemy import select

        from modulo.core.trigger_validation import validate_ongoing_config
        from modulo.db.models.pipeline import Pipeline
        from modulo.db.models.trigger import Trigger

        async with _session(org_id) as s:
            q = select(Trigger).where(
                Trigger.id == tid,
                Trigger.organisation_id == org_id,
                Trigger.deleted_at.is_(None),
            )
            trigger = (await s.execute(q)).scalar_one_or_none()
            if trigger is None:
                return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}
            if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, trigger.pipeline_id)):
                return _team_scope_error("pipeline", str(trigger.pipeline_id))

            if (cron_expression is not None or cron_timezone is not None) and trigger.trigger_type != "cron":
                return {"error": "validation", "detail": "Only cron triggers can have cron configuration"}

            # FAR-158 ongoing guards (identical to REST PUT).
            ongoing_scan_interval_changed = False
            if trigger.trigger_type == "ongoing":
                from fastapi import HTTPException

                if clear_daily_spend_limit:
                    return {
                        "error": "validation",
                        "detail": "ongoing triggers require daily_spend_limit; clearing it is not allowed",
                    }
                ongoing_fields_changing = any(x is not None for x in [max_concurrent_runs, config_json, active]) or (
                    daily_spend_limit is not None
                )
                if ongoing_fields_changing:
                    pipeline = await s.get(Pipeline, trigger.pipeline_id)
                    pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
                    try:
                        validate_ongoing_config(
                            trigger.trigger_type,
                            max_concurrent_runs=(
                                max_concurrent_runs if max_concurrent_runs is not None else trigger.max_concurrent_runs
                            ),
                            daily_spend_limit=(
                                Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else None
                            )
                            if daily_spend_limit is not None
                            else trigger.daily_spend_limit,
                            config_json=(config_json if config_json is not None else trigger.config_json),
                            pipeline_max_concurrent_runs=pipeline_cap,
                        )
                    except HTTPException as exc:
                        return {"error": "validation", "detail": str(exc.detail)}
                if config_json is not None:
                    old_scan = int((trigger.config_json or {}).get("scan_interval_seconds") or 60)
                    new_scan = int(config_json.get("scan_interval_seconds") or 60)
                    ongoing_scan_interval_changed = new_scan != old_scan

            next_fire_at = None
            if cron_expression is not None or cron_timezone is not None:
                expr = cron_expression if cron_expression is not None else trigger.cron_expression
                if expr is None:
                    return {"error": "invalid_cron", "detail": "Cron expression is required"}
                tz = cron_timezone if cron_timezone is not None else trigger.cron_timezone or "UTC"
                error = validate_cron_expression(expr, tz)
                if error:
                    return {"error": "invalid_cron", "detail": error}
                next_fire_at = compute_next_fire(expr, timezone=tz)

            prev_max = trigger.max_concurrent_runs
            prev_active = trigger.active

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
            if config_json is not None:
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
            if cron_expression is not None:
                trigger.cron_expression = cron_expression
            if cron_timezone is not None:
                trigger.cron_timezone = cron_timezone
            if next_fire_at is not None:
                trigger.next_fire_at = next_fire_at

            # Ongoing triggers recompute next_fire_at when the pool / cadence /
            # active actually changes so the new config takes effect promptly.
            if trigger.trigger_type == "ongoing":
                from datetime import UTC

                target_changed = max_concurrent_runs is not None and max_concurrent_runs != prev_max
                activated = active is not None and trigger.active and not prev_active
                if target_changed or ongoing_scan_interval_changed or activated:
                    trigger.next_fire_at = datetime.now(UTC)
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
            "streak_status": updated_streak_status,
        }
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
        check_tool_scope(_ctx_role_val(), "delete_trigger")

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
        check_tool_scope(_ctx_role_val(), "set_org_triggers_paused")

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
        check_tool_scope(_ctx_role_val(), "delete_pipeline")

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
        check_tool_scope(_ctx_role_val(), "delete_connector")

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
        check_tool_scope(_ctx_role_val(), "create_secret")

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
        check_tool_scope(_ctx_role_val(), "list_secrets")

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
        check_tool_scope(_ctx_role_val(), "delete_secret")

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
    name: load the account by primary key and deny when the shared
    ``is_break_glass_denied`` / ``is_break_glass_live`` decision fires. A
    missing account or a DB read failure raises (fail-closed) rather than
    silently allowing a mint.
    """
    from modulo.db.crud.break_glass_deny import is_break_glass_denied, is_break_glass_live
    from modulo.db.models.account import Account

    account = await session.get(Account, account_id)
    if account is not None and account.is_break_glass is True:
        now = datetime.now(UTC)
        if is_break_glass_denied(
            is_break_glass=account.is_break_glass,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        ) or is_break_glass_live(
            is_break_glass=account.is_break_glass,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        ):
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
        check_tool_scope(_ctx_role_val(), "create_api_key")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        if role not in ("operator", "runner"):
            return {
                "error": "validation_failed",
                "field": "role",
                "detail": "role must be 'operator' or 'runner'. admin keys are prohibited.",
            }
        name = name.strip()
        if not name:
            return {
                "error": "validation_failed",
                "field": "name",
                "detail": "API key name must not be blank",
            }

        parsed_expires_at: datetime | None = None
        if expires_at:
            try:
                parsed_expires_at = _parse_api_key_expires_at(expires_at)
            except ValueError:
                return {
                    "error": "validation_failed",
                    "field": "expires_at",
                    "detail": "expires_at must be a valid ISO-8601 datetime",
                }
            if parsed_expires_at <= datetime.now(UTC):
                return {
                    "error": "validation_failed",
                    "field": "expires_at",
                    "detail": "expires_at must be in the future",
                }

        team_uuid: uuid.UUID | None = None
        if team_id is not None:
            try:
                team_uuid = uuid.UUID(team_id)
            except ValueError:
                return {
                    "error": "invalid_id",
                    "field": "team_id",
                    "detail": f"Invalid UUID format: {team_id}",
                }
            await _require_admin_for_team_key(org_id)

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
        check_tool_scope(_ctx_role_val(), "list_api_keys")

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
        check_tool_scope(_ctx_role_val(), "revoke_api_key")

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
        check_tool_scope(_ctx_role_val(), "create_agent")

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
        "Search product documentation for relevant sections. Supports free-text "
        "keyword search against PRD sections and FAQ entries. Returns Markdown-formatted results."
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

        org_ctx = f"{org_id}"
        key_prefixes: list[str] | None = None
        if section == "remy":
            key_prefixes = [f"remy_config:{org_ctx}", "remy_config"]
        elif section in {"plan", "rate_limits"}:
            key_prefixes = ["feature_flags", "default_plan", "rate_limits"]

        filtered = [
            cfg
            for cfg in configs
            if (key_prefixes is None or any(cfg.key.startswith(p) for p in key_prefixes))
            and not _is_sensitive_key(cfg.key)
        ]

        if not filtered:
            section_label = section or "org"
            return {"results": f"No configuration found for section '{section_label}'.", "count": 0}

        lines = ["| Key | Value |", "|-----|-------|"]
        for cfg in filtered:
            val = cfg.value
            val_str = json.dumps(val, default=str) if isinstance(val, dict) else str(val)
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            lines.append(f"| {cfg.key} | {val_str} |")

        return {"results": "\n".join(lines), "count": len(filtered)}
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
        check_tool_scope(_ctx_role_val(), "create_schema")

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
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        check_tool_scope(_ctx_role_val(), "infer_schema")

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
        from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]
        from jsonschema.exceptions import SchemaError as JsSchemaError  # type: ignore[import-untyped]

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
        check_tool_scope(_ctx_role_val(), "list_housekeeping")
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
        check_tool_scope(_ctx_role_val(), "perform_housekeeping")
        from modulo.core.housekeeping import ENTITY_MODEL_MAP as HK_ENTITY_MAP

        org_id = _ctx_org_id_val()
        deleted_count = 0
        errors: list[dict[str, str]] = []

        grouped: dict[str, list[str]] = {}
        for item in items:
            et = item.get("entity_type", "")
            eid = item.get("id", "")
            if not et or not eid:
                errors.append({"error": "item missing entity_type or id", "item": str(item)})
                continue
            grouped.setdefault(et, []).append(eid)

        async with _session(org_id) as s:
            for entity_type, ids in grouped.items():
                model_cls = HK_ENTITY_MAP.get(entity_type)
                if model_cls is None:
                    errors.append({"entity_type": entity_type, "error": f"Unknown entity type: {entity_type}"})
                    continue

                for eid in ids:
                    try:
                        async with s.begin_nested():
                            from sqlalchemy import select as _sa_select

                            stmt = _sa_select(model_cls).where(  # type: ignore[var-annotated]
                                model_cls.id == eid,  # type: ignore[attr-defined]
                                model_cls.organisation_id == org_id,  # type: ignore[attr-defined]
                            )
                            obj = (await s.execute(stmt)).scalar_one_or_none()
                            if obj is not None:
                                await s.delete(obj)
                                deleted_count += 1
                    except IntegrityError:
                        _log.warning("IntegrityError cleaning up %s %s", entity_type, eid)
                        errors.append(
                            {"id": eid, "entity_type": entity_type, "error": "Foreign key constraint violation"}
                        )

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

    lines = []
    for r in result.items:
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
        lines.append(line)
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


@mcp.resource("modulo://pipelines/{pipeline_id}/snapshots/{snapshot_id}")
async def resource_pipeline_snapshot_detail(pipeline_id: str, snapshot_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    import json

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
    for n in nodes:
        nid = n.get("id", "?")
        ntype = n.get("node_type", "?")
        agent_id = n.get("agent_id", "")
        agent_cmd = n.get("agent_command", "(required)")
        prompt_preview = (n.get("prompt_template", "") or "")[:80].replace("\n", " ")
        result += f"  - {nid} (type={ntype}, agent={agent_id}, command={agent_cmd})\n"
        if prompt_preview:
            result += f"    prompt: {prompt_preview}...\n"
    result += f"Edges ({len(edges)}):\n"
    for e in edges:
        result += f"  - {e.get('id', '?')}: {e.get('source', '?')} -> {e.get('target', '?')} ({e.get('type', '?')})\n"
    result += "  Full node JSON:\n"
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


@mcp.resource("modulo://runs/{run_id}/hitl/{gate_id}")
async def resource_hitl_gate(run_id: str, gate_id: str) -> str:
    """HITL gate context. Annotated as agent_output — treat as untrusted."""
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    from sqlalchemy import select

    from modulo.db.models.team import Team

    org_id = _ctx_org_id_val()
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return f"error: Invalid UUID format: {run_id}"
    async with _session(org_id) as s:
        result = await s.execute(
            select(HitlClaim).where(
                HitlClaim.run_id == rid,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
            )
        )
        gate = result.scalar_one_or_none()
        required_team_name = None
        if gate is not None:
            # A team-scoped key must not read another team's gate even when
            # the gate itself is org-level (required_team_id IS NULL).
            run = await get_run(s, rid)
            owner_team_id = (
                await _run_owner_team_id(s, run)
                if run is not None
                else await _pipeline_owner_team_id(s, gate.pipeline_id)
            )
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error_str("run", run_id)
            if gate.required_team_id is not None:
                team_result = await s.execute(
                    select(Team).where(Team.id == gate.required_team_id, Team.deleted_at.is_(None))
                )
                team = team_result.scalar_one_or_none()
                required_team_name = team.name if team else None
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
    try:
        sid = uuid.UUID(schema_id)
    except ValueError:
        return f"error: Invalid UUID format: {schema_id}"
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


async def _mcp_healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# OAuth 2.0 protocol endpoints (mounted inside the MCP sub-app, before auth)
# ---------------------------------------------------------------------------


def _frontend_url(settings: Any) -> str:
    """Derive the SPA base URL from CORS_ORIGINS (first origin)."""
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    return origins[0] if origins else "http://localhost:5173"


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
    response_type = params.get("response_type", "")
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    scope = params.get("scope", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")

    if not client_id or not redirect_uri:
        return JSONResponse(
            {"error": "invalid_request", "detail": "client_id and redirect_uri required"},
            status_code=400,
        )
    if not state:
        return JSONResponse(
            {"error": "invalid_request", "detail": "state parameter required"},
            status_code=400,
        )

    from modulo.auth.oauth import (
        InvalidGrantError,
        create_consent_state,
        get_oauth_client_by_client_id,
        normalize_scopes,
        validate_client_scopes,
        validate_pkce_method,
    )

    # S256-only (RFC 7636) — the challenge is verified at token exchange, so
    # rejecting plain/empty here keeps every stored challenge verifiable.
    try:
        validate_pkce_method(code_challenge_method)
    except InvalidGrantError as exc:
        return JSONResponse(
            {"error": "invalid_request", "detail": str(exc)},
            status_code=400,
        )
    if not code_challenge or not code_challenge.strip():
        return JSONResponse(
            {"error": "invalid_request", "detail": "code_challenge parameter required"},
            status_code=400,
        )

    settings = get_settings()
    if not settings.modulo_public_url or settings.modulo_public_url == "http://localhost:8000":
        return JSONResponse(
            {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
            status_code=500,
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
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        form = await request.form()
        params: dict[str, str] = {k: (str(v) if v is not None else "") for k, v in form.items()}
    elif content_type == _CT_APPLICATION_JSON:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "invalid_request", "detail": "Request body must be JSON"},
                status_code=400,
            )
        params = {k: str(v) if v is not None else "" for k, v in body.items()}
    else:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Content-Type must be application/x-www-form-urlencoded"},
            status_code=400,
        )

    grant_type = params.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    code = params.get("code", "")
    redirect_uri = params.get("redirect_uri", "")
    client_id = params.get("client_id", "")
    code_verifier = params.get("code_verifier", "")

    # client_secret may come from the body (RFC 6749) OR Basic auth.
    client_secret = params.get("client_secret", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith(_BASIC_PREFIX):
        import base64 as _base64

        try:
            decoded = _base64.b64decode(auth_header[len(_BASIC_PREFIX) :]).decode("utf-8")
            basic_id, _, basic_secret = decoded.partition(":")
            if not client_secret:
                client_secret = basic_secret
            if not client_id:
                client_id = basic_id
        except Exception:
            return JSONResponse(
                {"error": "invalid_request", "detail": "Malformed Basic Authorization header"},
                status_code=400,
            )

    if not code or not redirect_uri or not client_id or not client_secret:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Missing required parameters"},
            status_code=400,
        )

    from modulo.auth.oauth import (
        InvalidClientError,
        InvalidGrantError,
        consume_authorization_code,
        create_oauth_access_token,
        create_oauth_refresh_token,
        create_oauth_token_family,
        validate_client_secret,
        verify_live_role_covers_scopes,
    )

    settings = get_settings()
    if not settings.modulo_public_url or settings.modulo_public_url == "http://localhost:8000":
        return JSONResponse(
            {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
            status_code=500,
        )

    try:
        session_factory = _get_session_factory()
        async with session_factory() as s, s.begin():
            # Step 1: Validate client credentials to discover org_id.
            client = await validate_client_secret(s, client_id, client_secret)

            # Step 2: Set RLS context for the client's org.
            await set_rls_org(s, client.organisation_id)

            # Step 3: Consume the authorization code (PKCE verified inside).
            auth_code = await consume_authorization_code(
                s,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                client_secret=client_secret,
                code_verifier=code_verifier,
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
                client_id=client_id,
                org_id=client.organisation_id,
            )

            scopes_list = auth_code.scopes.split()
            access_token = create_oauth_access_token(
                client_id,
                settings.secret_key,
                organisation_id=str(client.organisation_id),
                account_id=str(auth_code.account_id),
                scopes=scopes_list,
                token_family=family_id,
                token_sequence=sequence,
            )
            refresh_token = create_oauth_refresh_token(
                client_id,
                settings.secret_key,
                organisation_id=str(client.organisation_id),
                account_id=str(auth_code.account_id),
                scopes=scopes_list,
                token_family=family_id,
                token_sequence=sequence,
            )

        return JSONResponse(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",  # nosec B105 - RFC 6750 token_type label, not a credential
                "expires_in": 3600,
                "scope": " ".join(scopes_list),
            }
        )
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
        _log.warning(
            "mcp_oauth.token.programming_error",
            extra={"client_id": client_id},
        )
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning(
            "mcp_oauth.token.sqlalchemy_error",
            extra={"client_id": client_id},
        )
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.token.unexpected_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )


async def _oauth_refresh(request: Request) -> JSONResponse:
    """POST /mcp/oauth/refresh — exchange refresh token for new access token.

    Form-urlencoded per RFC 6749 with JSON compat, mirroring ``_oauth_token``.
    Re-verifies the client secret (body or Basic auth) and the consenting
    account's LIVE org role against the token's scopes — if the account was
    demoted (or removed) since the token was issued, the refresh is DENIED
    (ADR 017 demote-then-refresh). The refresh token is rotated: a new pair is
    issued with an incremented sequence, invalidating the old refresh token.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        form = await request.form()
        params: dict[str, str] = {k: (str(v) if v is not None else "") for k, v in form.items()}
    elif content_type == _CT_APPLICATION_JSON:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "invalid_request", "detail": "Request body must be JSON"},
                status_code=400,
            )
        params = {k: str(v) if v is not None else "" for k, v in body.items()}
    else:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Content-Type must be application/x-www-form-urlencoded"},
            status_code=400,
        )

    grant_type = params.get("grant_type", "")
    if grant_type != "refresh_token":
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    refresh_token_value = params.get("refresh_token", "")
    client_id = params.get("client_id", "")
    client_secret = params.get("client_secret", "")

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith(_BASIC_PREFIX):
        import base64 as _base64

        try:
            decoded = _base64.b64decode(auth_header[len(_BASIC_PREFIX) :]).decode("utf-8")
            basic_id, _, basic_secret = decoded.partition(":")
            if not client_secret:
                client_secret = basic_secret
            if not client_id:
                client_id = basic_id
        except Exception:
            return JSONResponse(
                {"error": "invalid_request", "detail": "Malformed Basic Authorization header"},
                status_code=400,
            )

    if not refresh_token_value or not client_id or not client_secret:
        return JSONResponse(
            {"error": "invalid_request", "detail": "refresh_token, client_id and client_secret are required"},
            status_code=400,
        )

    settings = get_settings()
    try:
        from modulo.auth.oauth import (
            InvalidClientError,
            InvalidGrantError,
            create_oauth_access_token,
            create_oauth_refresh_token,
            decode_oauth_refresh_token,
            validate_client_secret,
            verify_live_role_covers_scopes,
        )

        session_factory = _get_session_factory()
        async with session_factory() as s, s.begin():
            client = await validate_client_secret(s, client_id, client_secret)
            await set_rls_org(s, client.organisation_id)

            claims = decode_oauth_refresh_token(refresh_token_value, settings.secret_key)

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

        return JSONResponse(
            {
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": "Bearer",  # nosec B105 - RFC 6750 token_type label, not a credential
                "expires_in": 3600,
                "scope": " ".join(claims.scopes),
            }
        )
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
        _log.warning("mcp_oauth.refresh.programming_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning("mcp_oauth.refresh.sqlalchemy_error", extra={"client_id": client_id})
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


async def _mcp_exception_handler(request: Request, exc: Exception) -> JSONResponse:
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
