"""Remote MCP server â€” thin adapter over the ViewModel API.

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
import traceback as _traceback
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from jwt import InvalidTokenError as JWTError
from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
from modulo.api.middleware.rate_limiter import RateLimitMiddleware as RateLimiterMiddleware
from modulo.auth.api_key import ApiKeyInvalidError, validate_api_key
from modulo.auth.oauth import (
    check_oauth_token_family_valid,
    decode_oauth_access_token,
)

# ContextVars populated by McpAuthMiddleware before each request.
# Propagation: this server runs FastMCP in stateless HTTP mode, where each request
# spawns a fresh per-request server task *from the already-authenticated request
# coroutine* (StreamableHTTPSessionManager._handle_stateless_request calls
# task_group.start(...) at request time). asyncio/anyio copy the caller's context
# at task-creation time, so values set here in the middleware propagate to tool
# handlers. If a handler ever runs without this context, tenant resolution FAILS
# CLOSED (auth error) â€” there must never be a process-global fallback, because
# under concurrent multi-tenant load a global would resolve to whichever org
# authenticated last, leaking cross-tenant data.
from modulo.core.background_pipeline_worker import BackgroundPipelineWorker
from modulo.core.cron_scheduler import compute_next_fire, validate_cron_expression
from modulo.core.documentation_indexer import DocumentationIndex
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
from modulo.db.crud.model_backend import create_model_backend as db_create_model_backend
from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.run import get_run
from modulo.db.crud.schema import get_schema
from modulo.db.crud.schema import list_schemas as db_list_schemas
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

_log = logging.getLogger(__name__)
_bg_worker: BackgroundPipelineWorker | None = None


def set_background_worker(worker: BackgroundPipelineWorker) -> None:
    global _bg_worker
    _bg_worker = worker


_ctx_org_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_org_id")
_ctx_role: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_role")
_ctx_key_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_key_id")
_ctx_auth_token: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_token")
_ctx_user_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_user_id")
_ctx_auth_type: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_type")


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
    """Get role from the request context (None if unset â€” scope checks then fail closed)."""
    return _ctx_role.get(None)


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


async def validate_current_auth() -> bool:
    """Re-validate the current auth credential for per-event SSE enforcement.

    Checks the stored credential against the DB/issuer to detect mid-session
    revocation, expiry, or OAuth token family blacklisting.
    Returns True if the credential is still valid, False otherwise.

    Fail closed: the credential and org come exclusively from the
    request-scoped ContextVars set by ``McpAuthMiddleware``. If any of them
    is missing, the request is treated as unauthenticated â€” there is no
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
            async with _session(org_id) as s:
                await validate_api_key(s, token, org_id)
            return True

        if auth_type == "oauth":
            settings = get_settings()
            try:
                claims = decode_oauth_access_token(token, settings.secret_key)
            except JWTError:
                # Regular JWT (used by Remy) â€” skip OAuth token family check
                try:
                    from modulo.auth.jwt import decode_principal

                    decode_principal(token, settings.secret_key)
                except JWTError:
                    return False
                return True
            async with _session(claims.organisation_id) as s:
                return await check_oauth_token_family_valid(
                    s,
                    family_id=claims.token_family,
                    client_id=claims.client_id,
                    org_id=claims.organisation_id,
                )

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
                media_type="application/json",
            )
        token = auth_header[len("Bearer ") :].strip()

        # Try API key first (backwards compatible).
        if token.startswith("mk_"):
            try:
                # Validate the key without RLS first (the key's org is unknown).
                from modulo.auth.api_key import _MK_PREFIX, _PREFIX_LEN

                prefix = token[len(_MK_PREFIX) :][:_PREFIX_LEN]
                from sqlalchemy import select

                from modulo.db.models.api_key import OrgApiKey

                factory = _get_session_factory()
                async with factory() as s:
                    async with s.begin():
                        from sqlalchemy import text

                        await s.execute(text("SET LOCAL row_security TO OFF"))
                        result = await s.execute(
                            select(OrgApiKey).where(
                                OrgApiKey.lookup_prefix == prefix,
                                OrgApiKey.revoked_at.is_(None),
                            )
                        )
                    key_record = result.scalar_one_or_none()
                    if key_record is None:
                        raise ApiKeyInvalidError()
                    import hmac

                    from modulo.auth.api_key import _hash_key

                    if not hmac.compare_digest(key_record.hashed_secret, _hash_key(token)):
                        raise ApiKeyInvalidError()

                # Now re-validate within the correct RLS context.
                async with _session(key_record.organisation_id) as s:
                    key = await validate_api_key(s, token, org_id=key_record.organisation_id)
                org_id = key.organisation_id
                _ctx_org_id.set(org_id)
                _ctx_role.set(key.role)
                _ctx_key_id.set(key.id)
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
                    media_type="application/json",
                )
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
                    media_type="application/json",
                )
            if principal.organisation_id is None:
                return Response(
                    '{"error":"forbidden","detail":"Organisation membership required"}',
                    status_code=403,
                    media_type="application/json",
                )
            _ctx_org_id.set(principal.organisation_id)
            _ctx_role.set(principal.org_role or "runner")
            _ctx_key_id.set(uuid.UUID(int=0))
            _ctx_user_id.set(principal.account_id)
            _ctx_auth_token.set(token)
            _ctx_auth_type.set("oauth")
            request.scope["auth_principal"] = {
                "type": "user",
                "org_id": str(principal.organisation_id) if principal.organisation_id else "",
                "user_id": str(principal.account_id) if principal.account_id else "",
            }
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
                        media_type="application/json",
                    )
        except Exception:
            _log.exception("OAuth token family check failed")
            return Response(
                '{"error":"unauthorized","detail":"Token validation failed"}',
                status_code=401,
                media_type="application/json",
            )

        # Resolve role from scopes (highest scope wins).
        scope_set = set(claims.scopes)
        if "hitl:review" in scope_set:
            role = "operator"
        elif "trigger:run" in scope_set or "library:browse" in scope_set:
            role = "runner"
        else:
            role = "runner"

        _ctx_org_id.set(claims.organisation_id)
        _ctx_role.set(role)
        _ctx_key_id.set(uuid.UUID(int=0))  # sentinel for OAuth clients
        oauth_actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"modulo-oauth-client:{claims.client_id}")
        _ctx_user_id.set(oauth_actor_id)
        _ctx_auth_token.set(token)
        _ctx_auth_type.set("oauth")
        request.scope["auth_principal"] = {
            "type": "user",
            "org_id": str(claims.organisation_id),
            "user_id": str(claims.client_id),
        }

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
async def list_pipelines_tool(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            org_id = _ctx_org_id_val()
            from modulo.db.crud.pipeline import list_pipelines

            lim = max(1, min(limit, 100))
            async with _session(org_id) as s:
                result = await list_pipelines(s, cursor=cursor, page_size=lim)
            return {
                "data": [{"id": str(p.id), "name": p.name, "visibility": p.visibility} for p in result.items],
                "total": result.total,
                "next_cursor": result.next_cursor,
                "has_more": result.has_more,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_pipelines_tool, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except ProgrammingError:
            _log.exception("list_pipelines_tool failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_pipelines_tool failed")
            return _tool_error("Failed to list pipelines")
        return None


@mcp.tool(description="Create a new pipeline in the organisation. Returns the created pipeline details.")
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

    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in create_pipeline, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("create_pipeline failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("create_pipeline failed")
            return _tool_error("Failed to create pipeline")
        return None


@mcp.tool(
    description="List pipeline runs with filtering and cursor-based pagination.",
)
async def list_runs(
    pipeline_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "list_runs")
            from modulo.db.crud.run import list_runs as db_list_runs

            org_id = _ctx_org_id_val()
            pid = uuid.UUID(pipeline_id) if pipeline_id else None
            async with _session(org_id) as s:
                result = await db_list_runs(
                    s,
                    pipeline_id=pid,
                    status=status,
                    page=1,
                    page_size=limit,
                    cursor=cursor,
                )
            return {
                "items": [
                    {
                        "id": str(r.id),
                        "pipeline_id": str(r.pipeline_id),
                        "status": r.status,
                        "trigger_type": r.trigger_type,
                        "run_number": r.run_number,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                        "error_code": r.error_code,
                    }
                    for r in result.items
                ],
                "total": result.total,
                "next_cursor": result.next_cursor,
                "has_more": result.has_more,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_runs, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("list_runs failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_runs failed")
            return _tool_error("Failed to list runs")
        return None


@mcp.tool(
    description="Set or replace the graph (nodes + edges) of an existing pipeline. "
    "Pass nodes as a list of dicts with id, node_type, agent_id, position (x, y), "
    "and edges as a list of dicts with id, source_node_id, target_node_id, edge_type. "
    "Returns the updated graph."
)
async def update_pipeline_graph(
    pipeline_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "update_pipeline_graph")
            from modulo.db.crud.pipeline import replace_pipeline_graph

            org_id = _ctx_org_id_val()
            try:
                pid = uuid.UUID(pipeline_id)
            except ValueError:
                return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

            async with _session(org_id) as s:
                result = await replace_pipeline_graph(
                    s,
                    pipeline_id=pid,
                    org_id=org_id,
                    nodes=nodes,
                    edges=edges,
                )
                if result is None:
                    return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
                updated_nodes, updated_edges = result

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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in update_pipeline_graph, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("update_pipeline_graph failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("update_pipeline_graph failed")
            return _tool_error("Failed to update pipeline graph")
        return None


@mcp.tool(
    description="Bind a connector instance to a pipeline node. "
    "Updates the node's connector_binding in the pipeline graph. "
    "The connector must already exist in the organisation."
)
async def bind_connector_to_node(
    pipeline_id: str,
    node_id: str,
    connector_type: str,
    connector_instance_id: str,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in bind_connector_to_node, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("bind_connector_to_node failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("bind_connector_to_node failed")
            return _tool_error("Failed to bind connector to node")
        return None


@mcp.tool(description="Fire a pipeline run and return immediately with run_id. Poll get_run_status to track progress.")
async def trigger_pipeline(
    pipeline_id: str,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "trigger_pipeline")
            from modulo.db.crud.pipeline import get_pipeline
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
                uid = _ctx_user_id_val()
                snapshot = await create_snapshot_from_live_graph(s, pipeline_id=pid, account_id=uid)
                if snapshot is None:
                    return {"error": "snapshot_failed", "pipeline_id": pipeline_id}
                if not snapshot.graph_json or not snapshot.graph_json.get("nodes"):
                    return {
                        "error": "validation_failed",
                        "detail": "Pipeline graph has no nodes â€” cannot trigger run",
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

            if _bg_worker is not None:
                _bg_worker.submit(run_id, org_id, payload)
            else:
                _log.warning("MCP: Background worker not initialized â€” run %s will not execute", run_id)

            return {
                "run_id": str(run_id),
                "status": "pending",
                "langgraph_thread_id": thread_id,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in trigger_pipeline, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("trigger_pipeline failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("trigger_pipeline failed")
            return _tool_error("Failed to trigger pipeline")
        return None


@mcp.tool(description="Get current run status. Pass detail=true for per-node breakdown.")
async def get_run_status(run_id: str, detail: bool = False) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            org_id = _ctx_org_id_val()
            try:
                rid = uuid.UUID(run_id)
            except ValueError:
                return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}
            async with _session(org_id) as s:
                run = await get_run(s, rid)
            if run is None:
                return {"error": "run_not_found", "run_id": run_id}
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
                result["error_code"] = run.error_code
            if detail:
                token_usage = run.node_token_usage or {}
                outputs_json = run.outputs_json or {}
                node_ids: set[str] = set()
                node_ids.update(token_usage.keys())
                node_ids.update(outputs_json.keys())
                nodes: list[dict[str, Any]] = []
                for nid in sorted(node_ids):
                    usage = token_usage.get(nid, {})
                    t_in = usage.get("tokens_in", 0) if usage else 0
                    t_out = usage.get("tokens_out", 0) if usage else 0
                    nodes.append(
                        {
                            "node_id": nid,
                            "status": "completed" if nid in outputs_json else "processed",
                            "tokens": t_in + t_out,
                            "has_output": nid in outputs_json,
                        }
                    )
                result["nodes"] = nodes
            return result
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in get_run_status, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except ProgrammingError:
            _log.exception("get_run_status failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("get_run_status failed")
            return _tool_error("Failed to get run status")
        return None


@mcp.tool(
    description=(
        "Get a specific node's output from a completed pipeline run. "
        "Sensitive fields (tokens, secrets, API keys, passwords, credentials) "
        "are masked in the response."
    ),
)
async def get_run_output(run_id: str, node_id: str) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "get_run_output")
            from modulo.api.routes.runs import _mask_output_value

            org_id = _ctx_org_id_val()
            try:
                rid = uuid.UUID(run_id)
            except ValueError:
                return {"error": "invalid_id", "field": "run_id", "detail": f"Invalid UUID format: {run_id}"}
            async with _session(org_id) as s:
                run = await get_run(s, rid)
            if run is None:
                return {"error": "run_not_found", "run_id": run_id}
            outputs = run.outputs_json or {}
            node_output = outputs.get(node_id)
            if node_output is None:
                return {"error": "node_output_not_found", "run_id": run_id, "node_id": node_id}
            masked = _mask_output_value(node_output)

            # Detect masked fields by scanning for the bullet mask character.
            masked_fields: list[str] = []
            if isinstance(masked, dict):
                for k, v in masked.items():
                    if isinstance(v, str) and "\u2022" in v:
                        masked_fields.append(k)

            return {
                "node_id": node_id,
                "output": masked,
                "masked_fields": masked_fields,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in get_run_output, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("get_run_output failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("get_run_output failed")
            return _tool_error("Failed to get node output")
        return None


@mcp.tool(
    description="Get eval results for a given run. Returns structured eval outcomes "
    "including pass/fail status, scores, and detailed feedback.",
)
async def get_run_evals(run_id: str) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in get_run_evals, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("get_run_evals failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("get_run_evals failed")
            return _tool_error("Failed to get run evals")
        return None


@mcp.tool(
    description="List eval definitions with cursor-based pagination. Optionally filter by pipeline_id.",
)
async def list_eval_definitions(
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_eval_definitions, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("list_eval_definitions failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_eval_definitions failed")
            return _tool_error("Failed to list eval definitions")
        return None


@mcp.tool(description="Cancel a running pipeline run.")
async def cancel_run(run_id: str) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
                _terminal_statuses = frozenset({"complete", "failed", "cancelled", "eval_failed"})
                if run.status in _terminal_statuses:
                    detail = f"Run is already in terminal status: {run.status}"
                    return {"error": "cannot_cancel", "run_id": str(run_id), "detail": detail}
                run = await request_cancellation(s, rid)
            if run is None:
                return {"error": "run_not_found", "run_id": run_id}
            return {"run_id": run_id, "cancellation_requested": True}
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in cancel_run, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("cancel_run failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("cancel_run failed")
            return _tool_error("Failed to cancel run")
        return None


@mcp.tool(description="List all pending (undecided) HITL gates across all runs.")
async def list_pending_hitl(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "list_pending_hitl")
            from sqlalchemy import func, select

            org_id = _ctx_org_id_val()
            async with _session(org_id) as s:
                total_result = await s.execute(
                    select(func.count())
                    .select_from(HitlClaim)
                    .where(
                        HitlClaim.organisation_id == org_id,
                        HitlClaim.decision.is_(None),
                    )
                )
                total = total_result.scalar_one()

                offset = (page - 1) * page_size
                result = await s.execute(
                    select(HitlClaim)
                    .where(
                        HitlClaim.organisation_id == org_id,
                        HitlClaim.decision.is_(None),
                    )
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_pending_hitl, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("list_pending_hitl failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_pending_hitl failed")
            return _tool_error("Failed to list pending HITL gates")
        return None


@mcp.tool(
    description=(
        "Unified HITL gate action: claim, approve, reject, or deliver_manual. "
        "Step 1: call with action='claim' to get a claim_token. "
        "Step 2: call with action='approve', 'reject', or 'deliver_manual' + your claim_token. "
        "'deliver_manual' requires 'output' (a dict) to supply the output directly. "
        "human_only gates return 403 on approve â€” only a browser-authenticated human can approve."
    ),
)
async def review_hitl(
    run_id: str,
    gate_id: str,
    action: str,
    claim_token: str | None = None,
    reason: str | None = None,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error("Token revoked or expired - re-authenticate")

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

    for _attempt in range(3):
        try:
            async with _session(org_id) as s:
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
                        # Check human_only from pipeline edge config
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
                            return {
                                "error": "human_only_gate",
                                "detail": (
                                    "This gate has human_only=true. Only a browser-authenticated human can approve it."
                                ),
                            }

                try:
                    if action == "claim":
                        gate = await mgr.claim(
                            s,
                            run_id=rid,
                            gate_id=gate_id,
                            org_id=org_id,
                            claimant_id=key_id,
                        )
                        return {
                            "status": "claimed",
                            "claim_token": gate.claim_token,
                            "expires_at": gate.expires_at.isoformat() if gate.expires_at else None,
                        }
                    if action == "approve":
                        gate = await mgr.approve(
                            s,
                            run_id=rid,
                            gate_id=gate_id,
                            org_id=org_id,
                            claim_token=claim_token or "",
                        )
                        return {"status": "approved", "gate_id": gate_id}
                    if action == "deliver_manual":
                        gate = await mgr.deliver_manual(
                            s,
                            run_id=rid,
                            gate_id=gate_id,
                            org_id=org_id,
                            claim_token=claim_token or "",
                            output=output or {},
                            actor_id=key_id,
                        )
                        return {"status": "delivered_manual", "gate_id": gate_id}
                    gate = await mgr.reject(
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
                    return {
                        "error": "not_team_member",
                        "detail": "You are not a member of the team required by this gate",
                    }
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
                    return {
                        "error": "migration_required",
                        "detail": "Database migration required. Run `alembic upgrade head`.",
                    }
                except Exception:
                    _log.exception("review_hitl failed")
                    return _tool_error("Failed to process HITL action")

        except Exception:
            _log.exception("review_hitl operation failed")
            return _tool_error("Failed to process HITL action")
        return None


@mcp.tool(
    description=(
        "Copy a library primitive to the org workspace. "
        "Community primitives can be copied â€” this creates an editable copy in your workspace. "
        "Note: community primitives are maintained by the Modulo team; your copy diverges from upstream on first edit."
    ),
)
async def copy_library_primitive(
    primitive_id: str,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error("Token revoked or expired - re-authenticate")
    try:
        check_tool_scope(_ctx_role_val(), "copy_library_primitive")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(primitive_id)
    except ValueError:
        return {"error": "invalid_id", "field": "primitive_id", "detail": f"Invalid UUID format: {primitive_id}"}

    for attempt in range(3):
        try:
            async with _session(org_id) as s:
                try:
                    result = await library_copy_to_adapt(
                        s,
                        org_id,
                        pid,
                        via_mcp=False,
                    )
                except LookupError:
                    return {"error": "not_found", "primitive_id": primitive_id}
                except ProgrammingError:
                    _log.exception("copy_library_primitive failed")
                    return {
                        "error": "migration_required",
                        "detail": "Database migration required. Run `alembic upgrade head`.",
                    }
                except Exception:
                    _log.exception("copy_library_primitive failed")
                    return _tool_error("Failed to copy library primitive")

        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in copy_library_primitive, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
    return {
        "status": "copied",
        "primitive_id": str(result.id),
        "name": result.name,
        "slug": result.slug,
    }


@mcp.tool(
    name="browse_library",
    description=("[DEPRECATED â€” use search_library] Browse/search the library of primitives."),
)
async def browse_library_alias(
    primitive_type: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    result = await search_library(primitive_type=primitive_type, search=search, cursor=cursor, limit=limit)
    if not isinstance(result, dict):
        raise RuntimeError("search_library returned an invalid response")
    return result


@mcp.tool(
    name="search_library",
    description=(
        "Search the library of primitives (schemas, agents, workflows, "
        "pipeline templates, test fixtures). Supports filtering by type, "
        "text search, and cursor-based pagination. "
        "For text output, see the modulo://library resource."
    ),
)
async def search_library(
    primitive_type: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in search_library, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except ProgrammingError:
            _log.exception("search_library failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("search_library failed")
            return _tool_error("Failed to search library")
        return None


@mcp.tool(
    name="get_trigger_events",
    description=("[DEPRECATED â€” use list_trigger_events] Get recent trigger events with cursor-based pagination."),
)
async def get_trigger_events_alias(
    trigger_id: str | None = None,
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    result = await list_trigger_events(trigger_id=trigger_id, pipeline_id=pipeline_id, cursor=cursor, limit=limit)
    if not isinstance(result, dict):
        raise RuntimeError("list_trigger_events returned an invalid response")
    return result


@mcp.tool(
    name="list_trigger_events",
    description=(
        "List recent trigger events with cursor-based pagination. "
        "Filter by trigger_id and/or pipeline_id. Returns events ordered "
        "by most recent first."
    ),
)
async def list_trigger_events(
    trigger_id: str | None = None,
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "list_trigger_events")
            from sqlalchemy import func, select

            from modulo.db.crud.pagination import CursorPaginator
            from modulo.db.models.trigger import Trigger
            from modulo.db.models.trigger_event import TriggerEvent

            org_id = _ctx_org_id_val()
            lim = max(1, min(limit, 100))

            async with _session(org_id) as s:
                q = select(TriggerEvent).where(TriggerEvent.organisation_id == org_id)

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

                if pipeline_id is not None:
                    try:
                        pid = uuid.UUID(pipeline_id)
                    except ValueError:
                        return {
                            "error": "invalid_id",
                            "field": "pipeline_id",
                            "detail": f"Invalid UUID format: {pipeline_id}",
                        }
                    q = q.join(Trigger, TriggerEvent.trigger_id == Trigger.id).where(
                        Trigger.pipeline_id == pid,
                    )

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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_trigger_events, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("list_trigger_events failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_trigger_events failed")
            return _tool_error("Failed to list trigger events")
        return None


@mcp.tool(
    description="List triggers configured for the organisation with cursor-based pagination. "
    "Optionally filter by pipeline_id. Returns trigger metadata "
    "including type, active status, and cron schedule.",
)
async def list_triggers(
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "list_triggers")
            from modulo.db.crud.trigger import list_triggers as db_list_triggers

            org_id = _ctx_org_id_val()
            pid = uuid.UUID(pipeline_id) if pipeline_id else None
            lim = max(1, min(limit, 100))

            async with _session(org_id) as s:
                result = await db_list_triggers(s, org_id, pipeline_id=pid, cursor=cursor, limit=lim)

            return {
                "data": [
                    {
                        "id": str(t.id),
                        "pipeline_id": str(t.pipeline_id),
                        "trigger_type": t.trigger_type,
                        "active": t.active,
                        "max_concurrent_runs": t.max_concurrent_runs,
                        "cron_expression": t.cron_expression,
                        "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                    }
                    for t in result.items
                ],
                "total": result.total,
                "next_cursor": result.next_cursor,
                "has_more": result.has_more,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_triggers, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("list_triggers failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_triggers failed")
            return _tool_error("Failed to list triggers")
        return None


@mcp.tool(
    description="Create a new model backend (provider configuration). "
    "The API key is NOT sent through this tool â€” instead, a one-time setup URL is returned. "
    "Open the URL in your browser to provide the API key directly. "
    "This keeps the secret out of the LLM context and MCP transport logs. "
    "Common providers include: openai, anthropic, gemini, deepseek, groq, opencode.",
)
async def create_model_backend(
    name: str,
    display_name: str,
    provider: str,
    model_id: str,
    default_params: dict[str, Any] | None = None,
    visibility: str = "org",
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in create_model_backend, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("create_model_backend failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("create_model_backend failed")
            return _tool_error("Failed to create model backend")
        return None


@mcp.tool(
    description="Create a new connector instance (provider configuration). "
    "Credentials are encrypted at rest. Returns the created connector details."
)
async def create_connector(
    name: str,
    connector_type_id: str,
    credentials: str,
    config_json: dict[str, Any] | None = None,
    allowed_operations: list[str] | None = None,
    visibility: str = "org",
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in create_connector, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("create_connector failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("create_connector failed")
            return _tool_error("Failed to create connector")
        return None


@mcp.tool(description="Create a new trigger for a pipeline.")
async def create_trigger(
    pipeline_id: str,
    trigger_type: str = "manual",
    active: bool = True,
    cron_expression: str | None = None,
    config_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "create_trigger")

            org_id = _ctx_org_id_val()
            account_id = _ctx_user_id_val()
            try:
                pid = uuid.UUID(pipeline_id)
            except ValueError:
                return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

            from modulo.db.models.trigger import Trigger

            async with _session(org_id) as s:
                trigger = Trigger(
                    organisation_id=org_id,
                    pipeline_id=pid,
                    trigger_type=trigger_type,
                    active=active,
                    config_json=config_json or {},
                    account_id=account_id,
                )
                if cron_expression:
                    trigger.cron_expression = cron_expression
                    error = validate_cron_expression(cron_expression)
                    if error:
                        return {"error": "invalid_cron", "detail": error}
                    trigger.next_fire_at = compute_next_fire(cron_expression, timezone=trigger.cron_timezone or "UTC")
                s.add(trigger)
                await s.flush()

            return {
                "id": str(trigger.id),
                "pipeline_id": str(trigger.pipeline_id),
                "trigger_type": trigger.trigger_type,
                "active": trigger.active,
                "cron_expression": trigger.cron_expression,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in create_trigger, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("create_trigger failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("create_trigger failed")
            return _tool_error("Failed to create trigger")
        return None


@mcp.tool(description="Delete a pipeline by ID.")
async def delete_pipeline(
    pipeline_id: str,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "delete_pipeline")

            org_id = _ctx_org_id_val()
            try:
                pid = uuid.UUID(pipeline_id)
            except ValueError:
                return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

            from modulo.db.crud.pipeline import delete_pipeline as db_delete_pipeline

            async with _session(org_id) as s:
                deleted = await db_delete_pipeline(s, pid)

            if not deleted:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

            return {"status": "deleted", "pipeline_id": pipeline_id}
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in delete_pipeline, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("delete_pipeline failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("delete_pipeline failed")
            return _tool_error("Failed to delete pipeline")
        return None


@mcp.tool(description="Create a new agent. Returns the created agent details.")
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
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "create_agent")

            from modulo.db.crud.agent import create_agent as db_create_agent

            org_id = _ctx_org_id_val()
            account_id = _ctx_user_id_val()

            parsed_model_backend_id = uuid.UUID(model_backend_id) if model_backend_id else uuid.UUID(int=0)
            parsed_input_schema_id = uuid.UUID(input_schema_id) if input_schema_id else uuid.UUID(int=0)
            parsed_output_schema_id = uuid.UUID(output_schema_id) if output_schema_id else uuid.UUID(int=0)

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
                )

            return {
                "id": str(agent.id),
                "name": agent.name,
                "description": agent.description,
                "is_executable": agent.is_executable,
                "created_at": agent.created_at.isoformat() if agent.created_at else None,
            }
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in create_agent, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("create_agent failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
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
    global _doc_index, _doc_index_ts, _doc_index_ttl
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
    name="get_documentation",
    description=("[DEPRECATED â€” use search_documentation] Search product documentation."),
)
async def get_documentation_alias(query: str, section: str | None = None) -> dict[str, Any]:
    result = await search_documentation(query=query, section=section)
    if not isinstance(result, dict):
        raise RuntimeError("search_documentation returned an invalid response")
    return result


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
            return _tool_auth_error("Token revoked or expired - re-authenticate")
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
            return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
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
            return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        elif section == "plan" or section == "rate_limits":
            key_prefixes = ["feature_flags", "default_plan", "rate_limits"]

        filtered = []
        for cfg in configs:
            if key_prefixes is not None and not any(cfg.key.startswith(p) for p in key_prefixes):
                continue
            if _is_sensitive_key(cfg.key):
                continue
            filtered.append(cfg)

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
        return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
    except Exception:
        _log.exception("get_org_config failed")
        return _tool_error("Failed to get org configuration")


@mcp.tool(
    description="List product features enabled on the current plan tier.",
)
async def get_available_features() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
    except Exception:
        _log.exception("get_available_features failed")
        return _tool_error("Failed to get available features")


@mcp.tool(
    description="List registered schemas with cursor-based pagination. Returns schema metadata.",
)
async def list_schemas(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_schemas, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except ProgrammingError:
            _log.exception("list_schemas failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_schemas failed")
            return _tool_error("Failed to list schemas")
        return None


@mcp.tool(
    description="AI-assisted schema inference. Takes a sample JSON payload and returns an inferred "
    "JSON Schema definition.",
)
async def infer_schema(
    input_sample: dict[str, Any],
    pipeline_id: str | None = None,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
            check_tool_scope(_ctx_role_val(), "infer_schema")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in infer_schema, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except SchemaInferenceError as exc:
            return {"error": "inference_failed", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("infer_schema failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("infer_schema failed")
            return _tool_error("Failed to infer schema")
        return None


@mcp.tool(
    description="Validate a payload against a registered schema by schema_id. Returns validation errors or success.",
)
async def validate_payload(
    schema_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in validate_payload, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except ProgrammingError:
            _log.exception("validate_payload failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("validate_payload failed")
            return _tool_error("Failed to validate payload")


@mcp.tool(
    description="List housekeeping cleanup candidates for the organisation. "
    "Returns categories of potential cleanup items such as orphan secrets, "
    "unbound connectors, stale pipelines, and other candidates.",
)
async def list_housekeeping(limit: int = 100) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not await validate_current_auth():
                return _tool_auth_error("Token revoked or expired - re-authenticate")
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
        except OperationalError as exc:
            if attempt == 2:
                raise
            _log.warning("Transient DB error in list_housekeeping, retrying (%d/3): %s", attempt + 1, exc)
            await asyncio.sleep(0.5 * (2**attempt))
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        except ProgrammingError:
            _log.exception("list_housekeeping failed")
            return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
        except Exception:
            _log.exception("list_housekeeping failed")
            return _tool_error("Failed to list housekeeping candidates")
        return None


@mcp.tool(
    description="Delete housekeeping cleanup candidates. "
    "Accepts a list of items with id and entity_type. "
    "Valid entity types: secret, connector, model_backend, pipeline, "
    "pipeline_snapshot, trigger, webhook_dedup. "
    "Deletions are grouped by entity type with per-group savepoints.",
)
async def perform_housekeeping(items: list[dict[str, str]]) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error("Token revoked or expired - re-authenticate")
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

                            stmt = _sa_select(model_cls).where(
                                model_cls.id == eid,
                                model_cls.organisation_id == org_id,
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
        return {"error": "migration_required", "detail": "Database migration required. Run `alembic upgrade head`."}
    except Exception:
        _log.exception("perform_housekeeping failed")
        return _tool_error("Failed to perform housekeeping")


# Backward-compatible function references for renamed tools
browse_library = browse_library_alias
get_documentation = get_documentation_alias
get_trigger_events = get_trigger_events_alias


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("modulo://pipelines")
async def resource_pipelines() -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
    from modulo.db.crud.pipeline import list_pipelines

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await list_pipelines(s, page=1, page_size=50)
    lines = [f"- {p.name} (id={p.id}, visibility={p.visibility})" for p in result.items]
    return f"Pipelines ({result.total} total):\n" + "\n".join(lines)


@mcp.resource("modulo://pipelines/{pipeline_id}/runs")
async def resource_pipeline_runs(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
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
        result = await list_runs(s, pipeline_id=pid, page=1, page_size=50)

    if not result.items:
        return f"Pipeline '{pipeline.name}' has no runs."

    lines = [
        f"- Run {r.id} | status={r.status} | trigger={r.trigger_type} | "
        f"created={r.created_at.isoformat()} | "
        f"tokens={r.total_tokens or 0} | cost=${r.total_cost_usd or 0}"
        for r in result.items
    ]
    return f"Runs for pipeline {pipeline.name} ({result.total} total):\n" + "\n".join(lines)


@mcp.resource("modulo://pipelines/{pipeline_id}")
async def resource_pipeline_detail(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
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


@mcp.resource("modulo://runs/{run_id}")
async def resource_run(run_id: str) -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
    org_id = _ctx_org_id_val()
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return f"error: Invalid UUID format: {run_id}"
    async with _session(org_id) as s:
        run = await get_run(s, rid)
    if run is None:
        return f"Run {run_id} not found."
    parts = [
        f"Run: {run.id}",
        f"Pipeline: {run.pipeline_id}",
        f"Status: {run.status}",
        f"Trigger: {run.trigger_type}",
        f"Created: {run.created_at.isoformat()}",
    ]
    if run.error_code:
        parts.append(f"Error: {run.error_code}")
    return "\n".join(parts)


@mcp.resource("modulo://runs/{run_id}/hitl/{gate_id}")
async def resource_hitl_gate(run_id: str, gate_id: str) -> str:
    """HITL gate context. Annotated as agent_output â€” treat as untrusted."""
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
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
        if gate is not None and gate.required_team_id is not None:
            team_result = await s.execute(select(Team).where(Team.id == gate.required_team_id))
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
        parts.append(f"Required team: {gate.required_team_id}")
        parts.append(f"Required team name: {required_team_name or 'unknown'}")
    if gate.expires_at:
        parts.append(f"Claim expires: {gate.expires_at.isoformat()}")
    return "\n".join(parts)


@mcp.resource("modulo://schemas")
async def resource_schemas() -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
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
        return "error: Token revoked or expired - re-authenticate"
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
        for name, prop in defn["properties"].items():
            fields.append(
                {
                    "name": name,
                    "type": prop.get("type", "unknown"),
                    "required": name in required_set,
                }
            )
    elif "fields" in defn:
        for f in defn["fields"]:
            fields.append(
                {
                    "name": f.get("name", "?"),
                    "type": f.get("type", "unknown"),
                    "required": f.get("required", False),
                }
            )

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
        return "error: Token revoked or expired - re-authenticate"
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
    lines = [f"- {c.name} (type={c.connector_type_id})" for c in connectors]
    return f"Connectors ({len(connectors)}):\n" + "\n".join(lines)


@mcp.resource("modulo://model-backends")
async def resource_model_backends() -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
    from sqlalchemy import select

    from modulo.db.models.model_backend import ModelBackend

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(
            select(ModelBackend).where(ModelBackend.organisation_id == org_id).order_by(ModelBackend.name)
        )
        backends = list(result.scalars())
    lines = [f"- {b.name} ({b.provider}/{b.model_id})" for b in backends]
    return f"Model Backends ({len(backends)}):\n" + "\n".join(lines)


@mcp.resource("modulo://library")
async def resource_library() -> str:
    """List library primitives â€” schemas, agents, workflows, pipeline templates, test fixtures.

    For filtered browsing, use the ``browse_library`` tool instead.
    """
    if not await validate_current_auth():
        return "error: Token revoked or expired - re-authenticate"
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
            desc = f" â€” {p.description}" if p.description else ""
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
        return "error: Token revoked or expired - re-authenticate"
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


async def _oauth_authorize(request: Request) -> JSONResponse:
    """POST /mcp/oauth/authorize â€” issue authorization code."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Request body must be JSON"},
            status_code=400,
        )

    response_type = body.get("response_type", "")
    if response_type != "code":
        return JSONResponse(
            {"error": "unsupported_response_type"},
            status_code=400,
        )

    client_id = body.get("client_id", "")
    redirect_uri = body.get("redirect_uri", "")
    scope = body.get("scope", "")
    state = body.get("state", "")

    if not client_id or not redirect_uri:
        return JSONResponse(
            {"error": "invalid_request", "detail": "client_id and redirect_uri required"},
            status_code=400,
        )

    from modulo.auth.oauth import (
        create_authorization_code,
        get_oauth_client_by_client_id,
        normalize_scopes,
        validate_client_scopes,
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
            code = await create_authorization_code(
                s,
                client_id=client_id,
                org_id=client.organisation_id,
                scopes=" ".join(valid_scopes),
                redirect_uri=redirect_uri,
            )
    except ProgrammingError:
        _log.warning("mcp_oauth.authorize.programming_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": "Feature is not available. Run database migrations to enable it."},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning("mcp_oauth.authorize.sqlalchemy_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": "Database error occurred. Please try again."},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.authorize.unexpected_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": "An unexpected error occurred"},
            status_code=500,
        )

    return JSONResponse({"code": code, "state": state})


async def _oauth_token(request: Request) -> JSONResponse:
    """POST /mcp/oauth/token â€” exchange code for access token."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Request body must be JSON"},
            status_code=400,
        )

    grant_type = body.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    code = body.get("code", "")
    redirect_uri = body.get("redirect_uri", "")
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")

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

            # Step 3: Consume the authorization code.
            auth_code = await consume_authorization_code(
                s,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                client_secret=client_secret,
            )

            # Step 4: Create a new token family.
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
                scopes=scopes_list,
                token_family=family_id,
                token_sequence=sequence,
            )
            refresh_token = create_oauth_refresh_token(
                client_id,
                settings.secret_key,
                organisation_id=str(client.organisation_id),
                scopes=scopes_list,
                token_family=family_id,
                token_sequence=sequence,
            )

        return JSONResponse(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
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
            {"error": "server_error", "detail": "Feature is not available. Run database migrations to enable it."},
            status_code=501,
        )
    except SQLAlchemyError:
        _log.warning(
            "mcp_oauth.token.sqlalchemy_error",
            extra={"client_id": client_id},
        )
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": "Database error occurred. Please try again."},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.token.unexpected_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": "An unexpected error occurred"},
            status_code=500,
        )


async def _oauth_refresh(request: Request) -> JSONResponse:
    """POST /mcp/oauth/refresh â€” exchange refresh token for new access token.

    Stateless validation of the refresh token JWT. The refresh token carries
    all claims needed (client_id, org_id, scopes, token_family, token_sequence)
    so a new access token can be issued without a DB round-trip.

    The refresh token itself is rotated: a new refresh token is issued with
    an incremented sequence, invalidating the old one if presented again.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Request body must be JSON"},
            status_code=400,
        )

    grant_type = body.get("grant_type", "")
    if grant_type != "refresh_token":
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    refresh_token_value = body.get("refresh_token", "")
    if not refresh_token_value:
        return JSONResponse(
            {"error": "invalid_request", "detail": "refresh_token is required"},
            status_code=400,
        )

    settings = get_settings()
    try:
        from modulo.auth.oauth import (
            create_oauth_access_token,
            create_oauth_refresh_token,
            decode_oauth_refresh_token,
        )

        claims = decode_oauth_refresh_token(refresh_token_value, settings.secret_key)

        new_sequence = claims.token_sequence + 1
        new_access_token = create_oauth_access_token(
            claims.client_id,
            settings.secret_key,
            organisation_id=str(claims.organisation_id),
            scopes=claims.scopes,
            token_family=claims.token_family,
            token_sequence=new_sequence,
        )
        new_refresh_token = create_oauth_refresh_token(
            claims.client_id,
            settings.secret_key,
            organisation_id=str(claims.organisation_id),
            scopes=claims.scopes,
            token_family=claims.token_family,
            token_sequence=new_sequence,
        )

        return JSONResponse(
            {
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(claims.scopes),
            }
        )
    except (ValueError, JWTError) as exc:
        return JSONResponse(
            {"error": "invalid_grant", "detail": str(exc)},
            status_code=400,
        )
    except Exception:
        _log.exception("mcp_oauth.refresh.unexpected_error")
        return JSONResponse(
            {"error": "server_error", "detail": "An unexpected error occurred"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Build the mounted ASGI app (called from main.py)
# ---------------------------------------------------------------------------


async def _mcp_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unhandled MCP exceptions and return a structured JSON error.

    Starlette's ``ServerErrorMiddleware`` (outermost in the middleware stack)
    catches unhandled exceptions and calls this handler instead of the default
    ``PlainTextResponse("Internal Server Error")`` â€” making errors observable
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
        content={"error": "internal_error", "detail": "An unexpected error occurred"},
    )


def build_mcp_asgi_app() -> Starlette:
    """Return the MCP Starlette app wrapped with auth middleware."""
    inner = mcp.streamable_http_app()

    # Mount an in-sub-app health check for orchestrators / load balancers.
    health_route = Route("/healthz", _mcp_healthz, methods=["GET"])

    # OAuth protocol endpoints â€” placed before auth middleware so they
    # don't require a Bearer token (they use client_id + client_secret).
    oauth_authorize_route = Route("/oauth/authorize", _oauth_authorize, methods=["POST"])
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
        # to ensure it is called â€” Starlette does not invoke sub-app lifespans.
    )
