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

import contextvars
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from jose import JWTError

# Alpha limitation: _PLACEHOLDER_ORG_ID is hardcoded for single-org mode.
# In v1, the API key record will carry org context and the McpAuthMiddleware
# will resolve org_id dynamically from the key's organisation_id column.
# See https://linear.app/modulo/issue/MOD-XXX for the multi-tenant epic.
from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
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
    CommunityPrimitiveReadOnlyError,
    list_primitives,
)
from modulo.core.library_service import (
    copy_to_adapt as library_copy_to_adapt,
)
from modulo.core.mcp.scope_validator import MCPAuthorizationError, check_tool_scope
from modulo.db.crud.pipeline import get_pipeline, list_pipelines
from modulo.db.crud.run import get_run
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# ContextVars populated by McpAuthMiddleware before each request.
_ctx_org_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_org_id")
_ctx_role: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_role")
_ctx_key_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_key_id")
_ctx_auth_token: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_token")
_ctx_auth_type: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_type")

# Placeholder org for alpha (single-org). Replaced by multi-tenant auth in v1.
_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-global session factory, sharing the engine from dependencies.py."""
    settings = get_settings()
    return get_or_create_session_factory(get_or_create_engine(settings))


@asynccontextmanager
async def _session(org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    factory = _get_session_factory()
    async with factory() as s:
        async with s.begin():
            await set_rls_org(s, org_id)
            yield s


# ---------------------------------------------------------------------------
# Per-event auth validation
# ---------------------------------------------------------------------------


async def validate_current_auth() -> bool:
    """Re-validate the current auth credential for per-event SSE enforcement.

    Checks the stored credential against the DB/issuer to detect mid-session
    revocation, expiry, or OAuth token family blacklisting.
    Returns True if the credential is still valid, False otherwise.
    """
    auth_type = _ctx_auth_type.get(None)
    token = _ctx_auth_token.get(None)
    org_id = _ctx_org_id.get(None)

    if auth_type is None or token is None or org_id is None:
        return False

    try:
        if auth_type == "api_key":
            async with _session(org_id) as s:
                await validate_api_key(s, token, org_id)
            return True

        if auth_type == "oauth":
            settings = get_settings()
            claims = decode_oauth_access_token(token, settings.secret_key)
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

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # Allow unauthenticated access to the health check endpoint.
        clean = request.url.path.rstrip("/")
        if clean in ("/mcp/healthz", "/healthz"):
            resp: Response = await call_next(request)
            return resp

        # Allow unauthenticated access to the OAuth protocol endpoints.
        # These endpoints manage their own auth via client_id + client_secret.
        if clean in ("/mcp/oauth/authorize", "/mcp/oauth/token"):
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
            org_id = _PLACEHOLDER_ORG_ID
            try:
                async with _session(org_id) as s:
                    key = await validate_api_key(s, token, org_id)
                _ctx_org_id.set(org_id)
                _ctx_role.set(key.role)
                _ctx_key_id.set(key.id)
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
            return Response(
                '{"error":"unauthorized","detail":"Invalid or expired access token"}',
                status_code=401,
                media_type="application/json",
            )

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
        _ctx_auth_token.set(token)
        _ctx_auth_type.set("oauth")
        request.scope["auth_principal"] = {
            "type": "user",
            "org_id": str(claims.organisation_id),
            "user_id": str(claims.client_id),
        }

        resp = await call_next(request)
        return resp


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Modulo",
    instructions=(
        "Modulo is a self-hosted agentic SDLC platform. "
        "Use trigger_pipeline to fire runs, get_run_status to track them, "
        "get_run_output to inspect node outputs, "
        "and review_hitl to handle human-in-the-loop gates."
    ),
    stateless_http=True,
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _tool_error(msg: str) -> dict[str, Any]:
    """Return a safe error dict so internal traces don't leak to the MCP client."""
    return {"error": "internal_error", "detail": msg}


@mcp.tool(description="List pipelines in the organisation. Returns summaries.")
async def list_pipelines_tool(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
        async with _session(org_id) as s:
            result = await list_pipelines(s, page=page, page_size=page_size)
        return {
            "pipelines": [{"id": str(p.id), "name": p.name, "visibility": p.visibility} for p in result.items],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        }
    except Exception:
        _log.exception("list_pipelines_tool failed")
        return _tool_error("Failed to list pipelines")


@mcp.tool(description="Fire a pipeline run and return immediately with run_id. Poll get_run_status to track progress.")
async def trigger_pipeline(
    pipeline_id: str,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        check_tool_scope(_ctx_role.get(None), "trigger_pipeline")
        from modulo.db.crud.pipeline import get_pipeline
        from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
        from modulo.db.crud.run import create_run

        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
        pid = uuid.UUID(pipeline_id)
        payload = input_payload or {}

        async with _session(org_id) as s:
            pipeline = await get_pipeline(s, pid)
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            snapshot = await create_snapshot_from_live_graph(s, pipeline_id=pid, created_by=None)
            if snapshot is None:
                return {"error": "snapshot_failed", "pipeline_id": pipeline_id}
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

        return {
            "run_id": str(run_id),
            "status": "pending",
            "langgraph_thread_id": thread_id,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": exc.message}
    except Exception:
        _log.exception("trigger_pipeline failed")
        return _tool_error("Failed to trigger pipeline")


@mcp.tool(description="Get current run status. Pass detail=true for per-node breakdown.")
async def get_run_status(run_id: str, detail: bool = False) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
        rid = uuid.UUID(run_id)
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
        return result
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
async def get_run_output(run_id: str, node_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        check_tool_scope(_ctx_role.get(None), "get_run_output")
        from modulo.api.routes.runs import _mask_output_value

        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
        rid = uuid.UUID(run_id)
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
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": exc.message}
    except Exception:
        _log.exception("get_run_output failed")
        return _tool_error("Failed to get node output")


@mcp.tool(description="Cancel a running pipeline run.")
async def cancel_run(run_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        check_tool_scope(_ctx_role.get(None), "cancel_run")
        from modulo.db.crud.run import request_cancellation

        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
        rid = uuid.UUID(run_id)
        async with _session(org_id) as s:
            run = await request_cancellation(s, rid)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        return {"run_id": run_id, "cancellation_requested": True}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": exc.message}
    except Exception:
        _log.exception("cancel_run failed")
        return _tool_error("Failed to cancel run")


@mcp.tool(description="List all pending (undecided) HITL gates across all runs.")
async def list_pending_hitl(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        check_tool_scope(_ctx_role.get(None), "list_pending_hitl")
        from sqlalchemy import select

        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
        async with _session(org_id) as s:
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
            "pending_gates": [
                {
                    "run_id": str(g.run_id),
                    "gate_id": g.gate_id,
                    "pipeline_id": str(g.pipeline_id),
                    "claimed_by": str(g.claimed_by) if g.claimed_by else None,
                    "expires_at": g.expires_at.isoformat() if g.expires_at else None,
                    "required_team_id": str(g.required_team_id) if g.required_team_id else None,
                }
                for g in gates
            ]
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": exc.message}
    except Exception:
        _log.exception("list_pending_hitl failed")
        return _tool_error("Failed to list pending HITL gates")


@mcp.tool(
    description=(
        "Unified HITL gate action: claim, approve, or reject. "
        "Step 1: call with action='claim' to get a claim_token. "
        "Step 2: call with action='approve' or 'reject' + your claim_token. "
        "human_only gates return 403 on approve — only a browser-authenticated human can approve."
    ),
)
async def review_hitl(
    run_id: str,
    gate_id: str,
    action: str,
    claim_token: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_error("Token revoked or expired — re-authenticate")

    from sqlalchemy import select

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    key_id = _ctx_key_id.get(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    rid = uuid.UUID(run_id)
    mgr = HITLManager()

    if action not in ("claim", "approve", "reject"):
        return {"error": "invalid_action", "detail": "action must be claim, approve, or reject"}

    try:
        check_tool_scope(_ctx_role.get(None), "review_hitl", action=action)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": exc.message}

    if action == "approve" and claim_token is None:
        return {"error": "claim_token_required", "detail": "approve requires claim_token"}
    if action == "reject" and claim_token is None:
        return {"error": "claim_token_required", "detail": "reject requires claim_token"}

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
                if edge and edge.hitl_gate_config:
                    if edge.hitl_gate_config.get("human_only", False):
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
            elif action == "approve":
                gate = await mgr.approve(
                    s,
                    run_id=rid,
                    gate_id=gate_id,
                    org_id=org_id,
                    claim_token=claim_token or "",
                )
                return {"status": "approved", "gate_id": gate_id}
            else:
                gate = await mgr.reject(
                    s,
                    run_id=rid,
                    gate_id=gate_id,
                    org_id=org_id,
                    claim_token=claim_token or "",
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
        except Exception:
            _log.exception("review_hitl failed")
            return _tool_error("Failed to process HITL action")


@mcp.tool(
    description=(
        "Copy a library primitive to the org workspace. "
        "COMMUNITY PRIMITIVES are NOT accessible via MCP — they return a 403 error. "
        "Use the browser UI to adapt community primitives."
    ),
)
async def copy_library_primitive(
    primitive_id: str,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_error("Token revoked or expired — re-authenticate")
    try:
        check_tool_scope(_ctx_role.get(None), "copy_library_primitive")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": exc.message}

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    pid = uuid.UUID(primitive_id)

    async with _session(org_id) as s:
        try:
            result = await library_copy_to_adapt(
                s,
                org_id,
                pid,
                via_mcp=True,
            )
        except CommunityPrimitiveReadOnlyError:
            return {
                "error": "community_primitive_read_only",
                "detail": (
                    "Community primitives may only be adapted via the browser UI, not via MCP. "
                    "Open the Modulo dashboard in your browser, navigate to the Library section, "
                    "and use the 'Copy to Adapt' button there."
                ),
            }
        except LookupError:
            return {"error": "not_found", "primitive_id": primitive_id}
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
    description=(
        "Browse the library of primitives (schemas, agents, workflows, "
        "pipeline templates, test fixtures). Supports filtering by type, "
        "text search, and cursor-based pagination."
    ),
)
async def browse_library(
    primitive_type: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_error("Token revoked or expired — re-authenticate")
        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
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
    except Exception:
        _log.exception("browse_library failed")
        return _tool_error("Failed to browse library")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("modulo://pipelines")
async def resource_pipelines() -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired — re-authenticate"
    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    async with _session(org_id) as s:
        result = await list_pipelines(s, page=1, page_size=50)
    lines = [f"- {p.name} (id={p.id}, visibility={p.visibility})" for p in result.items]
    return f"Pipelines ({result.total} total):\n" + "\n".join(lines)


@mcp.resource("modulo://pipelines/{pipeline_id}")
async def resource_pipeline_detail(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired — re-authenticate"
    from sqlalchemy import func, select

    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.run import Run

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    pid = uuid.UUID(pipeline_id)
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
        return "error: Token revoked or expired — re-authenticate"
    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    rid = uuid.UUID(run_id)
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
    """HITL gate context. Annotated as agent_output — treat as untrusted."""
    if not await validate_current_auth():
        return "error: Token revoked or expired — re-authenticate"
    from sqlalchemy import select

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    rid = uuid.UUID(run_id)
    async with _session(org_id) as s:
        result = await s.execute(
            select(HitlClaim).where(
                HitlClaim.run_id == rid,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
            )
        )
        gate = result.scalar_one_or_none()
    if gate is None:
        return f"HITL gate '{gate_id}' not found on run {run_id}."
    parts = [
        f"Gate: {gate_id}",
        f"Run: {run_id}",
        f"Pipeline: {gate.pipeline_id}",
        f"Decision: {gate.decision or 'pending'}",
        f"Claimed by: {gate.claimed_by or 'unclaimed'}",
    ]
    if gate.expires_at:
        parts.append(f"Claim expires: {gate.expires_at.isoformat()}")
    return "\n".join(parts)


@mcp.resource("modulo://schemas")
async def resource_schemas() -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired — re-authenticate"
    from sqlalchemy import select

    from modulo.db.models.schema import Schema

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    async with _session(org_id) as s:
        result = await s.execute(select(Schema).where(Schema.organisation_id == org_id).order_by(Schema.name))
        schemas = list(result.scalars())
    lines = [f"- {sc.name} (id={sc.id})" for sc in schemas]
    return f"Schemas ({len(schemas)}):\n" + "\n".join(lines)


@mcp.resource("modulo://connectors")
async def resource_connectors() -> str:
    if not await validate_current_auth():
        return "error: Token revoked or expired — re-authenticate"
    from sqlalchemy import select

    from modulo.db.models.connector_instance import ConnectorInstance

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
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
        return "error: Token revoked or expired — re-authenticate"
    from sqlalchemy import select

    from modulo.db.models.model_backend import ModelBackend

    org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
    async with _session(org_id) as s:
        result = await s.execute(
            select(ModelBackend).where(ModelBackend.organisation_id == org_id).order_by(ModelBackend.name)
        )
        backends = list(result.scalars())
    lines = [f"- {b.name} ({b.provider}/{b.model_id})" for b in backends]
    return f"Model Backends ({len(backends)}):\n" + "\n".join(lines)


@mcp.resource("modulo://library")
async def resource_library() -> str:
    """List library primitives — schemas, agents, workflows, pipeline templates, test fixtures.

    For filtered browsing, use the ``browse_library`` tool instead.
    """
    if not await validate_current_auth():
        return "error: Token revoked or expired — re-authenticate"
    try:
        org_id = _ctx_org_id.get(_PLACEHOLDER_ORG_ID)
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
            lines.append(f"- {p.name} (id={p.id}, type={p.primitive_type}, v{p.version}, tags=[{tags_str}], rating={rating_str}){desc}")
        header = f"Library ({result.total} primitives):"
        return header + "\n" + "\n".join(lines)
    except Exception:
        _log.exception("resource_library failed")
        return "error: Failed to browse library"


# ---------------------------------------------------------------------------
# Health check (mounted inside the MCP sub-app, before auth middleware)
# ---------------------------------------------------------------------------


async def _mcp_healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# OAuth 2.0 protocol endpoints (mounted inside the MCP sub-app, before auth)
# ---------------------------------------------------------------------------


async def _oauth_authorize(request: Request) -> JSONResponse:
    """POST /mcp/oauth/authorize — issue authorization code."""
    try:
        body = await request.json()
    except Exception:
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

    session_factory = _get_session_factory()
    async with session_factory() as s:
        async with s.begin():
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

            code = await create_authorization_code(
                s,
                client_id=client_id,
                org_id=client.organisation_id,
                scopes=" ".join(valid_scopes),
                redirect_uri=redirect_uri,
            )

    return JSONResponse({"code": code, "state": state})


async def _oauth_token(request: Request) -> JSONResponse:
    """POST /mcp/oauth/token — exchange code for access token."""
    try:
        body = await request.json()
    except Exception:
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
        consume_authorization_code,
        create_oauth_access_token,
        create_oauth_token_family,
        get_oauth_client_by_client_id,
    )

    settings = get_settings()
    session_factory = _get_session_factory()
    async with session_factory() as s:
        async with s.begin():
            try:
                auth_code = await consume_authorization_code(
                    s,
                    code=code,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    client_secret=client_secret,
                )
            except Exception as exc:
                return JSONResponse(
                    {"error": "invalid_grant", "detail": str(exc)},
                    status_code=400,
                )

            client = await get_oauth_client_by_client_id(s, client_id)
            if client is None:
                return JSONResponse(
                    {"error": "invalid_client", "detail": "Client not found"},
                    status_code=400,
                )

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

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(scopes_list),
        }
    )


# ---------------------------------------------------------------------------
# Build the mounted ASGI app (called from main.py)
# ---------------------------------------------------------------------------


def build_mcp_asgi_app() -> Starlette:
    """Return the MCP Starlette app wrapped with auth middleware."""
    inner = mcp.streamable_http_app()

    # Mount an in-sub-app health check for orchestrators / load balancers.
    health_route = Route("/healthz", _mcp_healthz, methods=["GET"])

    # OAuth protocol endpoints — placed before auth middleware so they
    # don't require a Bearer token (they use client_id + client_secret).
    oauth_authorize_route = Route("/oauth/authorize", _oauth_authorize, methods=["POST"])
    oauth_token_route = Route("/oauth/token", _oauth_token, methods=["POST"])

    all_routes = [
        health_route,
        oauth_authorize_route,
        oauth_token_route,
        *list(inner.routes),
    ]
    app = Starlette(
        routes=all_routes,
        middleware=[
            Middleware(McpAuthMiddleware),
            Middleware(RateLimiterMiddleware),  # type: ignore[arg-type]
        ],
    )
    return app
