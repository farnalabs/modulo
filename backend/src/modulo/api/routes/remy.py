"""Remy Chat API — CRUD sessions, messages, and SSE streaming for LLM chat.

Endpoints:
    Sessions:
        GET    /api/v1/remy/sessions             — list user's sessions
        POST   /api/v1/remy/sessions             — create new session
        GET    /api/v1/remy/sessions/{id}        — get session with message count
        PATCH  /api/v1/remy/sessions/{id}        — rename session
        DELETE /api/v1/remy/sessions/{id}        — delete session + messages

    Messages:
        GET    /api/v1/remy/sessions/{id}/messages   — list messages for session
        POST   /api/v1/remy/sessions/{id}/messages   — append a message

    Streaming:
        POST   /api/v1/remy/sessions/{id}/stream     — SSE stream of LLM response

    UI Commands:
        POST   /api/v1/remy/sessions/{id}/permission-response
        POST   /api/v1/remy/sessions/{id}/ui-command-results
        POST   /api/v1/remy/sessions/{id}/reset-permissions
"""

import asyncio
import contextlib
import importlib
import json
import logging
import time as _time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from httpx import AsyncClient
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallChunk,
    ToolMessage,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.api.mcp_tool_registry import build_tool_registry, get_mcp_tool_definitions
from modulo.api.ui_tools import (
    _UI_TOOLS,
    DESTRUCTIVE_PATTERNS,
    NOGO_PAGE_PATTERNS,
    NOGO_SELECTOR_PATTERNS,
    UI_TOOL_NAMES,
    WRITE_TOOLS,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.feature_flags import get_registry
from modulo.core.remy.config_service import RemyConfig, RemyConfigService
from modulo.core.remy.skill_loader import SkillLoader
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.remy_message import ChatMessage
from modulo.db.models.remy_session import ChatSession
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.model_backends.base import ModelBackendBase
from modulo.settings import Settings, get_settings

_MSG_SESSION_NOT_FOUND = "Session not found"
_CODE_REMY_DATABASE_ERROR = "remy.database_error"
_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again later."
_MSG_UI_DRIVING_DISABLED_ORGANISATION = "UI driving is disabled by your organisation."
_SSE_ERROR_PREFIX = "event: error\ndata: "


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/remy", tags=["remy"])

# ── Provider → backend class mapping ──────────────────────────────────────
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        "ai21",
        "anthropic",
        "deepseek",
        "fireworks",
        "gemini",
        "grok",
        "groq",
        "openai",
        "opencode",
        "openrouter",
        "perplexity",
        "qwen",
        "togetherai",
    }
)

# ── In-memory event registry (single-worker only) ────────────────────────
# For multi-worker deployments, replace with Redis pub/sub.

_pending_permissions: dict[str, tuple[asyncio.Event, str]] = {}
_permission_decisions: dict[str, dict[str, Any]] = {}
_pending_ui_results: dict[str, asyncio.Event] = {}
_ui_command_results: dict[str, list[dict[str, Any]]] = {}
_resume_events: dict[str, asyncio.Event] = {}
_session_approvals: dict[str, dict[str, dict[str, Any]]] = {}
_SESSION_APPROVAL_TTL = timedelta(minutes=30)

# Index: account_id -> set of session_ids that belong to this account.
# Used by per-account logout scoping (FAR-1470).
_account_sessions: dict[str, set[str]] = {}

# ── Redis registry (lazy-init, multi-worker capable) ─────────────────────

_redis_registry: Any | None = None


def _get_registry() -> Any | None:
    global _redis_registry
    if _redis_registry is None:
        redis_url = get_settings().redis_url
        if not redis_url:
            return None
        from modulo.core.remy.redis_registry import RemyRedisRegistry

        _redis_registry = RemyRedisRegistry(redis_url)
    return _redis_registry


# ── Action rate limiter ──────────────────────────────────────────────────


class ActionRateLimiter:
    def __init__(self, max_actions: int = 15, window_seconds: int = 60) -> None:
        self._max_actions = max_actions
        self._window_seconds = window_seconds
        self._timestamps: list[float] = []

    def check(self) -> bool:
        now = _time.monotonic()
        cutoff = now - self._window_seconds
        self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
        if len(self._timestamps) >= self._max_actions:
            return False
        self._timestamps.append(now)
        return True


_rate_limiters: dict[str, ActionRateLimiter] = {}

# ── Pydantic schemas ─────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    provider: str | None = Field(None, description="LLM provider (e.g. openai, anthropic). Auto-detected if omitted.")
    model: str | None = Field(
        None, description="Model ID (e.g. gpt-4o, claude-sonnet-4-20250514). Auto-detected if omitted."
    )
    context_window_tokens: int = Field(..., ge=1024, le=1_000_000)
    name: str | None = None


class RenameSessionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class AppendMessageRequest(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant|tool_use|tool_result|summary)$")
    content: str | None = None
    tool_calls_json: dict[str, Any] | None = None
    tool_results_json: dict[str, Any] | None = None
    token_count: int | None = None
    parent_id: uuid.UUID | None = None


class StreamRequest(BaseModel):
    content: str = Field(..., description="The user's message text")
    provider: str = Field(..., description="LLM provider")
    model: str = Field(..., description="Model ID")
    context_window_tokens: int | None = Field(
        None,
        ge=1024,
        le=1_000_000,
        description="Override context window (defaults to session value)",
    )
    api_key: str | None = Field(
        None, description="Optional API key override. Auto-resolved from model backends if omitted."
    )
    mcp_api_key: str | None = Field(None, description="MCP API key for tool execution.")
    page_context: str | None = Field(None, description="Current page context for Remy's context-awareness.")
    system_prompt: str | None = Field(None, description="System prompt override.")
    exclude_ui_tools: bool = Field(
        False,
        description="Exclude the UI-driving tool family (remy-only mode — no browser automation).",
    )


class PermissionResponse(BaseModel):
    request_id: str
    action: str  # "approve" | "reject" | "approve_for_session"


class UiCommandResultItem(BaseModel):
    id: str
    name: str
    success: bool
    result: dict[str, Any] | None = None
    error: str | None = None


class UiCommandResultsBatch(BaseModel):
    results: list[UiCommandResultItem]
    api_key: str = Field(default="", description="User's API key for the LLM provider (auto-resolved if empty)")
    mcp_api_key: str | None = Field(None, description="API key for MCP tool execution")
    system_prompt: str | None = Field(None, description="Optional system prompt override")
    page_context: str | None = Field(None, description="Page context from the frontend")


# ── Helpers ──────────────────────────────────────────────────────────────


def _serialise_session(s: ChatSession, message_count: int = 0) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "user_id": str(s.user_id),
        "name": s.name,
        "session_number": s.session_number,
        "provider": s.provider,
        "model": s.model,
        "context_window_tokens": s.context_window_tokens,
        "system_prompt_hash": s.system_prompt_hash,
        "message_count": message_count,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialise_message(m: ChatMessage) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "session_id": str(m.session_id),
        "role": m.role,
        "content": m.content,
        "tool_calls_json": m.tool_calls_json,
        "tool_results_json": m.tool_results_json,
        "token_count": m.token_count,
        "parent_id": str(m.parent_id) if m.parent_id else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _message_to_langchain(m: ChatMessage) -> BaseMessage:
    match m.role:
        case "user":
            return HumanMessage(content=m.content or "")
        case "assistant":
            kwargs: dict[str, Any] = {"content": m.content or ""}
            if m.tool_calls_json:
                kwargs["tool_calls"] = m.tool_calls_json.get("tool_calls", [])
            return AIMessage(**kwargs)
        case "tool_use":
            return AIMessage(
                content=m.content or "",
                tool_calls=m.tool_calls_json.get("tool_calls", []) if m.tool_calls_json else [],
            )
        case "tool_result":
            tool_call_id = ""
            if m.tool_results_json:
                raw_tool_call_id = m.tool_results_json.get("tool_call_id", "")
                tool_call_id = raw_tool_call_id if isinstance(raw_tool_call_id, str) else ""
            return ToolMessage(content=m.content or "", tool_call_id=tool_call_id)
        case "summary":
            return SystemMessage(content=m.content or "")
        case _:
            logger.warning("Unknown message role %r, treating as user message", m.role)
            return HumanMessage(content=m.content or "")


# Provider → (module, backend class) map. Imported lazily via importlib so the
# heavy model-backend dependencies stay out of the module import path.
_BACKEND_IMPORTS: dict[str, tuple[str, str]] = {
    "ai21": ("modulo.model_backends.ai21", "Ai21Backend"),
    "anthropic": ("modulo.model_backends.anthropic", "AnthropicBackend"),
    "deepseek": ("modulo.model_backends.deepseek", "DeepSeekBackend"),
    "fireworks": ("modulo.model_backends.fireworks", "FireworksBackend"),
    "gemini": ("modulo.model_backends.gemini", "GeminiBackend"),
    "grok": ("modulo.model_backends.grok", "GrokBackend"),
    "groq": ("modulo.model_backends.groq", "GroqBackend"),
    "openai": ("modulo.model_backends.openai", "OpenAIBackend"),
    "opencode": ("modulo.model_backends.opencode", "OpenCodeBackend"),
    "openrouter": ("modulo.model_backends.openrouter", "OpenRouterBackend"),
    "perplexity": ("modulo.model_backends.perplexity", "PerplexityBackend"),
    "qwen": ("modulo.model_backends.qwen", "QwenBackend"),
    "togetherai": ("modulo.model_backends.togetherai", "TogetherAIBackend"),
}


def _build_backend(provider: str, model: str, api_key: str, **kwargs: Any) -> ModelBackendBase:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {provider!r}. Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}",
        )

    entry = _BACKEND_IMPORTS.get(provider)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {provider!r}",
        )
    module_name, class_name = entry
    backend_module = importlib.import_module(module_name)
    backend_cls = getattr(backend_module, class_name)
    return cast(ModelBackendBase, backend_cls(api_key=api_key, model_id=model, **kwargs))


async def _resolve_api_key(
    provider: str,
    org_id: uuid.UUID,
    session: AsyncSession,
    fernet_key: str,
) -> str | None:
    result = await session.execute(
        select(ModelBackend).where(
            ModelBackend.organisation_id == org_id,
            ModelBackend.provider == provider,
            ModelBackend.status == "active",
            ModelBackend.credentials_ciphertext != b"",
        )
    )
    backend = result.scalar_one_or_none()
    if backend is None:
        return None
    try:
        fernet = Fernet(fernet_key.encode())
        return fernet.decrypt(backend.credentials_ciphertext).decode()
    except Exception:
        logger.exception("Failed to decrypt credentials for provider %r", provider)
        return None


async def _call_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    mcp_api_key: str,
    base_url: str,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            async with AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base_url}/mcp/tools/call",
                    json={"tool": tool_name, "arguments": arguments},
                    headers={"Authorization": f"Bearer {mcp_api_key}"},
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    await asyncio.sleep(min(retry_after, 30))
                    continue
                resp.raise_for_status()
                result = resp.json()
                if not isinstance(result, dict):
                    raise ValueError("MCP tool returned a non-object response")
                return result
        except httpx.TimeoutException:
            logger.warning("MCP tool %r timed out (attempt %d/3)", tool_name, attempt + 1)
            last_exc = None
            await asyncio.sleep(2**attempt)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (502, 503, 504):
                logger.warning("MCP tool %r returned %d (attempt %d/3)", tool_name, e.response.status_code, attempt + 1)
                last_exc = e
                await asyncio.sleep(2**attempt)
                continue
            raise
        except httpx.RequestError as e:
            logger.warning("MCP tool %r request failed (attempt %d/3): %s", tool_name, attempt + 1, e)
            last_exc = e
            await asyncio.sleep(2**attempt)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"MCP tool call '{tool_name}' failed after 3 attempts.",
    ) from last_exc


def _reconstruct_tool_calls(buffers: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for idx in sorted(buffers):
        buf = buffers[idx]
        try:
            parsed_args = json.loads(buf["args"]) if buf["args"] else {}
        except json.JSONDecodeError:
            logger.warning("Failed to parse tool call args for %r: %r", buf["name"], buf["args"][:200])
            parsed_args = {}
        tool_calls.append({"id": buf["id"], "name": buf["name"], "args": parsed_args})
    return tool_calls


async def _reconstruct_messages(session: AsyncSession, session_id: uuid.UUID) -> list[BaseMessage]:
    result = await session.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at.asc())
    )
    db_messages = result.scalars().all()
    return [_message_to_langchain(m) for m in db_messages]


async def _is_ui_driving_enabled(
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Check if the remy_ui_driving feature flag is enabled for the given org.

    Uses FeatureFlagRegistry backed by the DB tier catalog, with granular
    override resolution (user > team > org > system default).
    Falls back to the hardcoded _KNOWN_FLAGS list when DB data is unavailable.
    """
    try:
        registry = get_registry()
        return await registry.resolve_flag("remy_ui_driving", org_id=org_id, user_id=user_id)
    except Exception:
        logger.warning("Failed to resolve plan context for ui_driving check, defaulting to True", exc_info=True)
        return True


# ── UI command helpers ───────────────────────────────────────────────────


async def _get_owned_session(
    session_id: uuid.UUID,
    principal: TenantPrincipal,
    db: AsyncSession,
) -> ChatSession:
    chat_session = await db.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != principal.account_id:
        raise HTTPException(status_code=404, detail=_MSG_SESSION_NOT_FOUND)
    return chat_session


async def _validate_session_ownership(
    session_id: uuid.UUID,
    principal: TenantPrincipal,
    db: AsyncSession,
) -> ChatSession:
    chat_session = await _get_owned_session(session_id, principal, db)
    # Track session→account mapping for per-account logout (FAR-1470)
    account_id_str = str(principal.account_id)
    session_id_str = str(session_id)
    _account_sessions.setdefault(account_id_str, set()).add(session_id_str)
    return chat_session


def _has_destructive_pattern(selector: str) -> bool:
    lower = selector.lower()
    return any(p in lower for p in DESTRUCTIVE_PATTERNS)


def _check_nogo(tool_name: str, args: dict[str, Any], page_context: str) -> bool:
    page_path = page_context
    for pattern in NOGO_PAGE_PATTERNS:
        if pattern in page_path:
            return True
    if tool_name in WRITE_TOOLS:
        selector = args.get("selector", "")
        for pattern in NOGO_SELECTOR_PATTERNS:
            if pattern in selector.lower():
                return True
    return False


def _resolve_tool_permission(config: RemyConfig, tool_name: str, args: dict[str, Any], page_context: str = "") -> str:
    """Returns 'always_allowed', 'requires_approval', 'nogo_requires_approval', or 'disabled'."""
    # 0. No-go zone check (highest priority)
    if _check_nogo(tool_name, args, page_context) and config.permission_mode == "full_auto":
        return "disabled"

    # 0b. Allowlist enforcement (second priority — restricts which elements/pages are auto-allowed)
    if config.allowed_selectors and tool_name in ("click", "fill", "select", "extract"):
        selector = args.get("selector", "") or args.get("data-testid", "")
        if (
            not any(allowed in selector for allowed in config.allowed_selectors)
            and config.permission_mode == "full_auto"
        ):
            return "disabled"

    if config.allowed_page_patterns and tool_name == "navigate":
        path = args.get("path", "")
        if (
            not any(pattern in path for pattern in config.allowed_page_patterns)
            and config.permission_mode == "full_auto"
        ):
            return "disabled"

    # 1. Per-tool user override
    overrides = config.tool_permissions or {}
    if tool_name in overrides:
        return overrides[tool_name]

    # 2. Mode-based defaults
    mode = config.permission_mode
    if mode == "locked_down":
        base = "requires_approval" if tool_name in WRITE_TOOLS or tool_name == "press" else "always_allowed"
    elif mode == "full_auto":
        base = "always_allowed"
        raw_confidence = args.get("confidence", 1.0)
        confidence = raw_confidence if isinstance(raw_confidence, int | float) else 1.0
        if confidence < config.auto_execute_threshold:
            return "requires_approval"
    else:
        base = "requires_approval" if tool_name == "press" else "always_allowed"

    # 3. Destructive pattern override (applies regardless of mode)
    if base == "always_allowed" and tool_name in WRITE_TOOLS:
        selector = args.get("selector", "")
        if _has_destructive_pattern(selector):
            return "requires_approval"

    return base


async def _is_approved_for_session(session_id: str, tool_name: str, page_path: str) -> bool:
    registry = _get_registry()
    if registry is not None:
        approved = await registry.is_session_approved(session_id, tool_name, page_path)
        return bool(approved)
    session_approvals = _session_approvals.get(session_id)
    if not session_approvals:
        return False
    now = datetime.now(UTC)
    stale_keys = [k for k, v in session_approvals.items() if now >= v["expires_at"]]
    for k in stale_keys:
        del session_approvals[k]
    if not session_approvals:
        _session_approvals.pop(session_id, None)
        return False
    approval = session_approvals.get(tool_name)
    return bool(approval and now < approval["expires_at"] and approval["page_path"] == page_path)


async def _set_session_approval(session_id: str, tool_name: str, page_path: str) -> None:
    registry = _get_registry()
    if registry is not None:
        await registry.set_session_approval(session_id, tool_name, page_path)
        return
    if session_id not in _session_approvals:
        _session_approvals[session_id] = {}
    _session_approvals[session_id][tool_name] = {
        "page_path": page_path,
        "expires_at": datetime.now(UTC) + _SESSION_APPROVAL_TTL,
    }


async def _clear_session_approvals(session_id: str) -> None:
    registry = _get_registry()
    if registry is not None:
        await registry.clear_session_approvals(session_id)
        return
    _session_approvals.pop(session_id, None)


def clear_session_approvals_for_account(account_id: str) -> None:
    """Clear session approvals for a specific account only (FAR-1470).

    Scopes the logout to the caller's own sessions instead of clearing
    every user's in-memory approvals, using the account→sessions index. If the
    index wasn't populated on this worker (sessions live on another instance),
    there is nothing scoped to clear here — deliberately no clear-all fallback,
    which would wipe every account's approvals (FAR-1470 regression).
    """
    session_ids = _account_sessions.pop(account_id, set())
    for sid in session_ids:
        _session_approvals.pop(sid, None)
    # No clear-all fallback: if the account index wasn't populated on this
    # worker (sessions live on another instance), clearing every account's
    # in-memory approvals would recreate the cross-account wipe FAR-1470
    # set out to fix. The account has no scoped approvals here to clear.


def _get_all_tool_definitions(include_ui_tools: bool = True) -> list[dict[str, Any]]:
    """Combine UI tool and MCP tool definitions for the LLM's tools parameter."""
    tools: list[dict[str, Any]] = []
    if include_ui_tools:
        for name, schema in _UI_TOOLS.items():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": schema["description"],
                        "parameters": {
                            "type": "object",
                            "properties": schema["parameters"],
                        },
                    },
                }
            )
    tools.extend(get_mcp_tool_definitions())
    return tools


# ── Stream event-generator helpers ───────────────────────────────────────


def _sse_tool_call_event(tool_result: dict[str, Any]) -> str:
    return f"event: tool_call\ndata: {json.dumps(tool_result)}\n\n"


async def _resolve_stream_api_key(
    req: StreamRequest,
    principal: TenantPrincipal,
    db_session: AsyncSession,
    settings: Settings,
) -> tuple[str | None, str | None]:
    """Return (api_key, error_detail). A non-None error_detail means no key resolved."""
    if req.api_key:
        return req.api_key, None
    async with db_session.begin():
        await set_rls_org(db_session, principal.organisation_id)
        await set_rls_user_context(db_session, principal.account_id, principal.org_role)
        resolved = await _resolve_api_key(
            req.provider,
            principal.organisation_id,
            db_session,
            settings.fernet_key,
        )
    if resolved is None:
        msg = (
            f"No active {req.provider} API key configured. Add one in Settings > Model Backends or provide an api_key."
        )
        return None, msg
    return resolved, None


def _build_stream_backend(req: StreamRequest, api_key: str) -> tuple[ModelBackendBase | None, str | None]:
    """Return (backend, error_detail). A non-None error_detail means construction failed."""
    try:
        return _build_backend(req.provider, req.model, api_key), None
    except HTTPException as exc:
        return None, str(exc.detail)
    except Exception as exc:
        logger.exception("remy.backend_construction_failed")
        return None, f"Failed to initialize backend: {exc}"


async def _build_stream_system_prompt(
    db_session: AsyncSession,
    principal: TenantPrincipal,
    req: StreamRequest,
    supports_tools: bool,
) -> str:
    async with db_session.begin():
        await set_rls_org(db_session, principal.organisation_id)
        skill_loader = SkillLoader(db_session)
        return await skill_loader.build_system_prompt(
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            page_context=req.page_context,
            system_prompt_override=req.system_prompt,
            include_ui_tools_text=(not supports_tools) and not req.exclude_ui_tools,
        )


async def _save_stream_user_message(
    db_session: AsyncSession,
    principal: TenantPrincipal,
    session_id: uuid.UUID,
    req: StreamRequest,
) -> uuid.UUID:
    async with db_session.begin():
        await set_rls_org(db_session, principal.organisation_id)
        user_msg = ChatMessage(
            organisation_id=principal.organisation_id,
            session_id=session_id,
            role="user",
            content=req.content,
        )
        db_session.add(user_msg)
        await db_session.flush()
        return user_msg.id


async def _build_stream_tools_param(
    backend: ModelBackendBase,
    principal: TenantPrincipal,
    req: StreamRequest,
) -> list[dict[str, Any]] | None:
    if not getattr(backend, "supports_tools", False):
        return None
    await build_tool_registry()
    return _get_all_tool_definitions(
        include_ui_tools=(await _is_ui_driving_enabled(principal.organisation_id)) and not req.exclude_ui_tools,
    )


def _prune_context_window(messages: list[BaseMessage], context_window: int) -> int:
    """Trim old messages until the conversation fits the 80% token budget.

    Returns the number of messages pruned.
    """
    budget = int(context_window * 0.8)
    total_tokens = sum(max(1, len(m.content or "") // 4) for m in messages)
    pruned_count = 0
    while total_tokens > budget and len(messages) > 2:
        removed = messages.pop(1)
        total_tokens -= max(1, len(removed.content or "") // 4)
        pruned_count += 1
    return pruned_count


def _accumulate_tool_call_chunks(chunk: AIMessageChunk, buffers: dict[int, dict[str, Any]]) -> None:
    """Accumulate partial tool-call argument chunks into the index-keyed buffers."""
    if not chunk.tool_call_chunks:
        return
    for chunk_call in chunk.tool_call_chunks:
        _accumulate_one_tool_call(chunk_call, buffers)


def _accumulate_one_tool_call(chunk_call: ToolCallChunk, buffers: dict[int, dict[str, Any]]) -> None:
    """Merge a single tool-call chunk into the index-keyed buffers."""
    idx = chunk_call.get("index") or 0
    buf = buffers.get(idx)
    if buf is None:
        buffers[idx] = {
            "id": chunk_call.get("id", "") or "",
            "name": chunk_call.get("name", "") or "",
            "args": chunk_call.get("args", "") or "",
        }
        return
    if chunk_call.get("id"):
        buf["id"] = chunk_call["id"] or ""
    if chunk_call.get("name"):
        buf["name"] = chunk_call["name"] or ""
    if chunk_call.get("args"):
        buf["args"] += chunk_call["args"] or ""


async def _auto_name_stream_session(
    db_session: AsyncSession,
    session_id: uuid.UUID,
    req: StreamRequest,
    chat_session: ChatSession,
) -> None:
    """Derive and persist an auto-generated name for an unnamed session."""
    if chat_session.name:
        return
    msg_count_q = select(func.count(ChatMessage.id)).where(
        ChatMessage.session_id == session_id,
        ChatMessage.role == "user",
    )
    msg_count = (await db_session.execute(msg_count_q)).scalar() or 0
    auto_name: str | None = None
    if msg_count == 1:
        # First message: use first 40 chars of user's first message
        name_seed = req.content[:40].strip()
        auto_name = name_seed + ("..." if len(req.content) > 40 else "")
    elif msg_count >= 10:
        # After 10 messages: use first user message prefix
        first_msg_q = (
            select(ChatMessage.content)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(1)
        )
        first_msg = (await db_session.execute(first_msg_q)).scalar() or ""
        name_seed = first_msg[:30].strip()
        auto_name = f"{name_seed}... ({msg_count} msgs)" if first_msg else None
    if auto_name:
        try:
            async with db_session.begin():
                stmt = update(ChatSession).where(ChatSession.id == session_id).values(name=auto_name)
                await db_session.execute(stmt)
        except Exception:
            logger.exception("remy.auto_naming_failed")


async def _finalise_stream_assistant_message(
    db_session: AsyncSession,
    principal: TenantPrincipal,
    session_id: uuid.UUID,
    full_content: str,
    parent_msg_id: uuid.UUID | None,
    req: StreamRequest,
    chat_session: ChatSession,
) -> str:
    """Persist the final assistant message (no tool calls) and auto-name the session."""
    async with db_session.begin():
        await set_rls_org(db_session, principal.organisation_id)
        assistant_msg = ChatMessage(
            organisation_id=principal.organisation_id,
            session_id=session_id,
            role="assistant",
            content=full_content or None,
            tool_calls_json=None,
            parent_id=parent_msg_id,
        )
        db_session.add(assistant_msg)
        await db_session.flush()
        msg_id = str(assistant_msg.id)
        await _auto_name_stream_session(db_session, session_id, req, chat_session)
    return msg_id


async def _run_mcp_tool_calls(
    mcp_tool_calls: list[dict[str, Any]],
    req: StreamRequest,
    mcp_base_url: str,
) -> AsyncGenerator[dict[str, Any], None]:
    if mcp_tool_calls and not req.mcp_api_key:
        for tc in mcp_tool_calls:
            yield {
                "tool_call_id": tc["id"],
                "tool_name": tc["name"],
                "success": False,
                "error": "Tool execution requires an MCP API key",
            }
    elif req.mcp_api_key is not None:
        for tc in mcp_tool_calls:
            try:
                result = await _call_mcp_tool(
                    tool_name=tc["name"],
                    arguments=tc["args"],
                    mcp_api_key=req.mcp_api_key,
                    base_url=mcp_base_url,
                )
                yield {
                    "tool_call_id": tc["id"],
                    "tool_name": tc["name"],
                    "success": True,
                    "result": result,
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("MCP tool call failed: %r", tc["name"])
                err_msg = f"{type(exc).__name__}: {exc}"[:200]
                yield {
                    "tool_call_id": tc["id"],
                    "tool_name": tc["name"],
                    "success": False,
                    "error": err_msg,
                }


async def _run_manifest_calls(
    manifest_calls: list[dict[str, Any]],
    req: StreamRequest,
) -> AsyncGenerator[dict[str, Any], None]:
    for tc in manifest_calls:
        if req.exclude_ui_tools:
            yield {
                "tool_call_id": tc["id"],
                "tool_name": "get_manifest",
                "success": False,
                "error": "UI driving is not available in this view",
            }
            continue
        from modulo.core.manifest import get_manifest

        manifest = get_manifest()
        path = tc["args"].get("path")
        if path:
            route = manifest.get("routes", {}).get(path)
            elements = manifest.get("elements", {}).get(path, [])
            result = {"route": route, "elements": elements}
        else:
            result = {
                "routes": {
                    k: {
                        "name": v.get("name"),
                        "testid": v.get("testid"),
                        "type": v.get("type"),
                        "sidebar_group": v.get("sidebar_group"),
                    }
                    for k, v in manifest.get("routes", {}).items()
                },
                "elements": manifest.get("elements", {}),
                "sidebar_groups": manifest.get("sidebar_groups", {}),
            }
        yield {
            "tool_call_id": tc["id"],
            "tool_name": "get_manifest",
            "success": True,
            "result": result,
        }


async def _classify_ui_tool_permissions(
    config: RemyConfig,
    ui_tool_calls: list[dict[str, Any]],
    page_path: str,
    session_id_str: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split UI tool calls into (approved, pending-permission) buckets."""
    approved_calls: list[dict[str, Any]] = []
    pending_permission_calls: list[dict[str, Any]] = []
    for tc in ui_tool_calls:
        perm = _resolve_tool_permission(config, tc["name"], tc["args"], page_path)
        if perm == "disabled":
            continue
        if perm in ("requires_approval", "nogo_requires_approval") and not await _is_approved_for_session(
            session_id_str, tc["name"], page_path
        ):
            if perm == "nogo_requires_approval":
                tc["_nogo"] = True
            pending_permission_calls.append(tc)
            continue
        approved_calls.append(tc)
    return approved_calls, pending_permission_calls


async def _wait_for_stream_resume(
    registry: Any | None,
    session_id_str: str,
) -> None:
    if registry is not None:
        await registry.subscribe_resume(session_id_str, timeout=300.0)
    else:
        resume_ev = _resume_events.get(session_id_str)
        if resume_ev is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(resume_ev.wait(), timeout=300.0)


def _build_permission_request_payload(
    pending_permission_calls: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    req_id = str(uuid.uuid4())
    payload = {
        "request_id": req_id,
        "tools": [
            {
                "name": tc["name"],
                "args": tc["args"],
                **({"nogo": True} if tc.get("_nogo") else {}),
            }
            for tc in pending_permission_calls
        ],
    }
    return payload, req_id


async def _await_permission_decision(
    registry: Any | None,
    session_id_str: str,
    req_id: str,
    pending_permission_calls: list[dict[str, Any]],
    page_path: str,
) -> list[dict[str, Any]]:
    """Register the permission request, wait for a decision, return approved calls."""
    event = asyncio.Event()
    _pending_permissions[req_id] = (event, session_id_str)
    if registry is not None:
        await registry.set_permission_request(
            req_id,
            session_id_str,
            [{"name": tc["name"], "args": tc["args"]} for tc in pending_permission_calls],
        )
    approved: list[dict[str, Any]] = []
    try:
        if registry is not None:
            decision_data = await registry.subscribe_permission_response(req_id, timeout=60.0)
            decision = {"action": "reject"} if decision_data is None else decision_data
        else:
            await asyncio.wait_for(event.wait(), timeout=60.0)
            decision = _permission_decisions.pop(req_id, {"action": "reject"})
        if decision["action"] in ("approve", "approve_for_session"):
            approved = list(pending_permission_calls)
            if decision["action"] == "approve_for_session":
                for tc in pending_permission_calls:
                    await _set_session_approval(session_id_str, tc["name"], page_path)
    except TimeoutError:
        pass
    finally:
        _pending_permissions.pop(req_id, None)
    return approved


async def _wait_for_ui_command_results(
    registry: Any | None,
    session_id_str: str,
    event: asyncio.Event,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        if registry is not None:
            ready = await registry.subscribe_ui_results(session_id_str, timeout=120.0)
            if ready:
                results = await registry.get_and_clear_ui_command_results(session_id_str)
        else:
            await asyncio.wait_for(event.wait(), timeout=120.0)
            results = _ui_command_results.pop(session_id_str, [])
    except TimeoutError:
        pass
    return results


def _merge_ui_command_results(
    approved_calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for ac, r in zip(approved_calls, results, strict=False):
        merged.append(
            {
                "tool_call_id": ac["id"],
                "tool_name": r.get("name", ""),
                "success": r.get("success", False),
                "result": r.get("result"),
                "error": r.get("error"),
            }
        )
    return merged


def _tool_result_content(tool_result: dict[str, Any]) -> str:
    return json.dumps(tool_result.get("result", tool_result.get("error", "")))


async def _persist_assistant_and_tool_messages(
    db_session: AsyncSession,
    principal: TenantPrincipal,
    session_id: uuid.UUID,
    full_content: str,
    tool_calls: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    parent_msg_id: uuid.UUID | None,
) -> str:
    async with db_session.begin():
        await set_rls_org(db_session, principal.organisation_id)
        assistant_msg = ChatMessage(
            organisation_id=principal.organisation_id,
            session_id=session_id,
            role="assistant",
            content=full_content or None,
            tool_calls_json={"tool_calls": tool_calls} if tool_calls else None,
            parent_id=parent_msg_id,
        )
        db_session.add(assistant_msg)
        await db_session.flush()
        msg_id = str(assistant_msg.id)

        for tr in tool_results:
            tool_msg = ChatMessage(
                organisation_id=principal.organisation_id,
                session_id=session_id,
                role="tool_result",
                content=_tool_result_content(tr),
                tool_results_json=tr,
                parent_id=assistant_msg.id,
            )
            db_session.add(tool_msg)
    return msg_id


# ── Session endpoints ────────────────────────────────────────────────────


@router.get("/sessions", status_code=status.HTTP_200_OK)
@handle_db_errors("remy.list_sessions")
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            total_q = select(func.count(ChatSession.id)).where(ChatSession.user_id == principal.account_id)
            total_result = await session.execute(total_q)
            total = total_result.scalar() or 0

            q = (
                select(ChatSession)
                .where(ChatSession.user_id == principal.account_id)
                .order_by(ChatSession.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            result = await session.execute(q)
            sessions = result.scalars().all()

            if sessions:
                session_ids = [s.id for s in sessions]
                count_q = (
                    select(ChatMessage.session_id, func.count(ChatMessage.id).label("cnt"))
                    .where(ChatMessage.session_id.in_(session_ids))
                    .group_by(ChatMessage.session_id)
                )
                count_result = await session.execute(count_q)
                count_map = {row.session_id: row.cnt for row in count_result}
            else:
                count_map = {}

            items = [_serialise_session(s, count_map.get(s.id, 0)) for s in sessions]

        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except ProgrammingError:
        logger.exception("remy.list_sessions")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except Exception:
        logger.exception("remy.list_sessions.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
@handle_db_errors("remy.create_session")
async def create_session(
    req: CreateSessionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            max_sn = await session.execute(
                select(func.coalesce(func.max(ChatSession.session_number), 0)).where(
                    ChatSession.user_id == principal.account_id
                )
            )
            next_session_number = (max_sn.scalar() or 0) + 1

            provider = req.provider
            model = req.model

            if provider is None or model is None:
                mb_result = await session.execute(
                    select(ModelBackend)
                    .where(
                        ModelBackend.organisation_id == principal.organisation_id,
                        ModelBackend.credentials_ciphertext != b"",
                    )
                    .limit(1)
                )
                mb = mb_result.scalar_one_or_none()
                if mb:
                    provider = provider or mb.provider
                    model = model or mb.model_id
                else:
                    config = await RemyConfigService(session).get_config(principal.organisation_id)
                    provider = provider or config.default_provider
                    model = model or config.default_model

            if not model:
                default_models: dict[str, str] = {
                    "opencode": "deepseek-v4-flash",
                }
                if provider in default_models:
                    model = default_models[provider]

            chat_session = ChatSession(
                organisation_id=principal.organisation_id,
                user_id=principal.account_id,
                name=req.name,
                provider=provider,
                model=model,
                context_window_tokens=req.context_window_tokens,
                session_number=next_session_number,
            )
            session.add(chat_session)
            await session.flush()

        return _serialise_session(chat_session)
    except ProgrammingError:
        logger.exception("remy.create_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except Exception:
        logger.exception("remy.create_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.get("/sessions/{session_id}", status_code=status.HTTP_200_OK)
@handle_db_errors("remy.get_session")
async def get_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)

            count_q = select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session_id)
            count_result = await session.execute(count_q)
            msg_count = count_result.scalar() or 0

        return _serialise_session(chat_session, msg_count)
    except ProgrammingError:
        logger.exception("remy.get_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.get_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.patch("/sessions/{session_id}", status_code=status.HTTP_200_OK)
@handle_db_errors("remy.rename_session")
async def rename_session(
    session_id: uuid.UUID,
    req: RenameSessionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)

            chat_session.name = req.name
            await session.flush()

        return _serialise_session(chat_session)
    except ProgrammingError:
        logger.exception("remy.rename_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.rename_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.delete("/sessions/{session_id}", status_code=status.HTTP_200_OK)
@handle_db_errors("remy.delete_session")
async def delete_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)

            await session.delete(chat_session)

        # Clean up in-memory state
        session_id_str = str(session_id)
        _pending_permissions.pop(session_id_str, None)
        _permission_decisions.pop(session_id_str, None)
        _pending_ui_results.pop(session_id_str, None)
        _ui_command_results.pop(session_id_str, None)
        _resume_events.pop(session_id_str, None)
        _session_approvals.pop(session_id_str, None)
        _rate_limiters.pop(session_id_str, None)

        # Clean up Redis registries if active
        registry = _get_registry()
        if registry is not None:
            try:
                await registry.clear_session(session_id_str)
            except Exception:
                logger.exception("remy.redis_cleanup_failed")

        return {"status": "deleted", "id": str(session_id)}
    except ProgrammingError:
        logger.exception("remy.delete_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.delete_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ── Message endpoints ────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/messages", status_code=status.HTTP_200_OK)
@handle_db_errors("remy.list_messages")
async def list_messages(
    session_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _get_owned_session(session_id, principal, session)

            total_q = select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session_id)
            total_result = await session.execute(total_q)
            total = total_result.scalar() or 0

            q = (
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id, ChatMessage.organisation_id == principal.organisation_id)
                .order_by(ChatMessage.created_at.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            result = await session.execute(q)
            messages = result.scalars().all()

        return {
            "items": [_serialise_message(m) for m in messages],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except ProgrammingError:
        logger.exception("remy.list_messages")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.list_messages.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_201_CREATED)
@handle_db_errors("remy.append_message")
async def append_message(
    session_id: uuid.UUID,
    req: AppendMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _get_owned_session(session_id, principal, session)

            msg = ChatMessage(
                organisation_id=principal.organisation_id,
                session_id=session_id,
                role=req.role,
                content=req.content,
                tool_calls_json=req.tool_calls_json,
                tool_results_json=req.tool_results_json,
                token_count=req.token_count,
                parent_id=req.parent_id,
            )
            session.add(msg)
            await session.flush()

        return _serialise_message(msg)
    except ProgrammingError:
        logger.exception("remy.append_message")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.append_message.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ── Streaming endpoint ───────────────────────────────────────────────────


@router.post("/sessions/{session_id}/stream")
@handle_db_errors("remy.stream_chat")
async def stream_chat(
    session_id: uuid.UUID,
    req: StreamRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    # Validate the session exists and belongs to user
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)
    except ProgrammingError:
        logger.exception("remy.stream_chat")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None

    mcp_base_url = settings.modulo_public_url.rstrip("/")

    session_id_str = str(session_id)

    logger.info(
        "remy.stream_started",
        extra={
            "org_id": str(principal.organisation_id),
            "session_id": session_id_str,
            "exclude_ui_tools": req.exclude_ui_tools,
            "remy_only": req.exclude_ui_tools,
        },
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        """SSE event generator — agentic loop with multi-turn LLM + UI commands."""
        msg_id: str | None = None
        last_ping_at = _time.monotonic()
        parent_msg_id: uuid.UUID | None = None
        try:
            async with AsyncSession(session.bind, autobegin=False) as db_session:
                # 1. Resolve API key
                api_key, api_key_error = await _resolve_stream_api_key(req, principal, db_session, settings)
                if api_key_error is not None:
                    yield f"event: error\ndata: {json.dumps({'detail': api_key_error})}\n\n"
                    return
                if api_key is None:
                    yield f"event: error\ndata: {json.dumps({'detail': 'Failed to resolve API key for streaming'})}\n\n"
                    return

                # 2. Create backend (needed before system prompt for supports_tools)
                backend, backend_error = _build_stream_backend(req, api_key)
                if backend_error is not None:
                    yield f"event: error\ndata: {json.dumps({'detail': backend_error})}\n\n"
                    return
                if backend is None:
                    yield f"event: error\ndata: {json.dumps({'detail': 'Failed to build model backend'})}\n\n"
                    return

                # 3. Construct system prompt from config + skills
                supports_tools = getattr(backend, "supports_tools", False)
                system_prompt = await _build_stream_system_prompt(db_session, principal, req, supports_tools)

                # 4. Save user message to DB
                parent_msg_id = await _save_stream_user_message(db_session, principal, session_id, req)

                # 5. Reconstruct conversation
                async with db_session.begin():
                    await set_rls_org(db_session, principal.organisation_id)
                    langchain_messages = await _reconstruct_messages(db_session, session_id)

                # 6. Prepend system prompt
                if system_prompt:
                    langchain_messages.insert(0, SystemMessage(content=system_prompt))

                # 7. Context window pruning
                context_window = (
                    req.context_window_tokens
                    if req.context_window_tokens is not None
                    else (chat_session.context_window_tokens or 200000)
                )
                pruned_count = _prune_context_window(langchain_messages, context_window)
                if pruned_count:
                    logger.info("Pruned %d messages from session %s", pruned_count, session_id)

                # ── Agentic loop ────────────────────────────────────────
                while True:
                    full_content = ""
                    tool_call_buffers: dict[int, dict[str, Any]] = {}

                    tools_param = await _build_stream_tools_param(backend, principal, req)

                    async for chunk in backend.stream(langchain_messages, tools=tools_param):
                        if await request.is_disconnected():
                            return
                        if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
                            full_content += chunk.content
                            yield f"event: token\ndata: {json.dumps({'token': chunk.content})}\n\n"
                            _accumulate_tool_call_chunks(chunk, tool_call_buffers)

                    if await request.is_disconnected():
                        return

                    tool_calls = _reconstruct_tool_calls(tool_call_buffers)

                    if not tool_calls:
                        # LLM done — save assistant message and exit loop
                        msg_id = await _finalise_stream_assistant_message(
                            db_session, principal, session_id, full_content, parent_msg_id, req, chat_session
                        )
                        break

                    # Separate UI vs MCP tool calls
                    ui_tool_calls = [tc for tc in tool_calls if tc["name"] in UI_TOOL_NAMES]
                    mcp_tool_calls = [tc for tc in tool_calls if tc["name"] not in UI_TOOL_NAMES]

                    tool_results: list[dict[str, Any]] = []

                    # Execute MCP tools
                    async for tr in _run_mcp_tool_calls(mcp_tool_calls, req, mcp_base_url):
                        tool_results.append(tr)
                        yield _sse_tool_call_event(tr)

                    # Handle get_manifest calls server-side
                    manifest_calls = [tc for tc in ui_tool_calls if tc["name"] == "get_manifest"]
                    ui_tool_calls = [tc for tc in ui_tool_calls if tc["name"] != "get_manifest"]

                    async for tr in _run_manifest_calls(manifest_calls, req):
                        tool_results.append(tr)
                        yield _sse_tool_call_event(tr)

                    # Handle UI tools
                    if ui_tool_calls and (
                        req.exclude_ui_tools or not await _is_ui_driving_enabled(principal.organisation_id)
                    ):
                        ui_driving_error = (
                            "UI driving is not available in this view"
                            if req.exclude_ui_tools
                            else _MSG_UI_DRIVING_DISABLED_ORGANISATION
                        )
                        for tc in ui_tool_calls:
                            tr = {
                                "tool_call_id": tc["id"],
                                "tool_name": tc["name"],
                                "success": False,
                                "error": ui_driving_error,
                            }
                            tool_results.append(tr)
                            yield _sse_tool_call_event(tr)
                    elif ui_tool_calls:
                        async with db_session.begin():
                            await set_rls_org(db_session, principal.organisation_id)
                            config_service = RemyConfigService(db_session)
                            config = await config_service.get_config(principal.organisation_id)

                        # Check if session is paused — wait for resume
                        registry = _get_registry()
                        paused = session_id_str in _resume_events
                        if paused:
                            yield (
                                "event: paused\ndata: "
                                + json.dumps({"detail": "Session paused. Waiting for resume."})
                                + "\n\n"
                            )
                            await _wait_for_stream_resume(registry, session_id_str)

                        page_path = req.page_context or ""
                        approved_calls, pending_permission_calls = await _classify_ui_tool_permissions(
                            config, ui_tool_calls, page_path, session_id_str
                        )

                        if pending_permission_calls:
                            payload, req_id = _build_permission_request_payload(pending_permission_calls)
                            yield f"event: permission_request\ndata: {json.dumps(payload)}\n\n"
                            approved_calls.extend(
                                await _await_permission_decision(
                                    registry, session_id_str, req_id, pending_permission_calls, page_path
                                )
                            )

                        if approved_calls:
                            # Rate limiter check before yielding commands
                            rate_limiter = _rate_limiters.get(session_id_str)
                            if rate_limiter is None:
                                rate_limiter = ActionRateLimiter(
                                    max_actions=config.rate_limit_max_actions,
                                    window_seconds=config.rate_limit_window_seconds,
                                )
                                _rate_limiters[session_id_str] = rate_limiter
                            if not rate_limiter.check():
                                yield (
                                    _SSE_ERROR_PREFIX
                                    + json.dumps({"detail": "Rate limited. Too many UI actions in quick succession."})
                                    + "\n\n"
                                )
                                break

                            yield f"event: ui_command_batch\ndata: {
                                json.dumps(
                                    {
                                        'commands': approved_calls,
                                    }
                                )
                            }\n\n"

                            registry = _get_registry()
                            event = asyncio.Event()
                            _pending_ui_results[session_id_str] = event
                            try:
                                results = await _wait_for_ui_command_results(registry, session_id_str, event)
                            finally:
                                _pending_ui_results.pop(session_id_str, None)

                            if len(approved_calls) != len(results):
                                logger.warning(
                                    "remy.ui_result_length_mismatch",
                                    extra={"approved": len(approved_calls), "results": len(results)},
                                )
                            for tr in _merge_ui_command_results(approved_calls, results):
                                tool_results.append(tr)
                                yield _sse_tool_call_event(tr)

                            if results and all(r.get("error") == "cancelled_by_user" for r in results):
                                skipped = len(results)
                                s = "s" if skipped != 1 else ""
                                summary = f"Action cancelled by user. {skipped} action{s} skipped."
                                yield f"event: abort_summary\ndata: {
                                    json.dumps(
                                        {
                                            'completed': 0,
                                            'skipped': skipped,
                                            'summary': summary,
                                        }
                                    )
                                }\n\n"
                                break

                    # Add to conversation for next LLM turn
                    langchain_messages.append(AIMessage(content=full_content, tool_calls=tool_calls))
                    for tr in tool_results:
                        langchain_messages.append(
                            ToolMessage(
                                content=_tool_result_content(tr),
                                tool_call_id=tr["tool_call_id"],
                            )
                        )

                    # Save to DB
                    msg_id = await _persist_assistant_and_tool_messages(
                        db_session, principal, session_id, full_content, tool_calls, tool_results, parent_msg_id
                    )

                    # Ping keepalive if idle
                    now = _time.monotonic()
                    if now - last_ping_at >= 15:
                        yield "event: ping\ndata: {}\n\n"
                        last_ping_at = now

            yield f"event: done\ndata: {json.dumps({'message_id': msg_id})}\n\n"

        except HTTPException as exc:
            yield f"event: error\ndata: {json.dumps({'detail': exc.detail})}\n\n"
        except ProgrammingError:
            logger.exception("Remy streaming error — missing DB table or schema")
            yield (_SSE_ERROR_PREFIX + json.dumps({"detail": MSG_FEATURE_NOT_AVAILABLE}) + "\n\n")
        except SQLAlchemyError:
            logger.exception(_CODE_REMY_DATABASE_ERROR)
            yield (_SSE_ERROR_PREFIX + json.dumps({"detail": _MSG_DATABASE_ERROR_PLEASE_TRY}) + "\n\n")
        except Exception:
            logger.exception("Remy streaming error")
            yield f"event: error\ndata: {json.dumps({'detail': 'An unexpected error occurred. Please try again.'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── UI Command endpoints ─────────────────────────────────────────────────


@router.post(
    "/sessions/{session_id}/permission-response",
    responses={404: {"description": "Not Found"}},
)
@handle_db_errors("remy.submit_permission_response")
async def submit_permission_response(
    session_id: uuid.UUID,
    req: PermissionResponse,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    if not await _is_ui_driving_enabled(principal.organisation_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MSG_UI_DRIVING_DISABLED_ORGANISATION,
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        logger.exception("remy.submit_permission_response")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.submit_permission_response.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    registry = _get_registry()
    if registry is not None:
        req_data = await registry.get_permission_request(req.request_id)
        if req_data is None:
            raise HTTPException(status_code=404, detail="Permission request not found or expired")
        if req_data["session_id"] != str(session_id):
            raise HTTPException(status_code=403, detail="Permission request does not belong to this session")
        decision = {"action": req.action}
        await registry.set_permission_decision(req.request_id, decision)
        await registry.publish_permission_response(req.request_id, decision)
        return {"status": "ok"}

    entry = _pending_permissions.get(req.request_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Permission request not found or expired")
    event, req_session_id = entry
    if req_session_id != str(session_id):
        raise HTTPException(status_code=403, detail="Permission request does not belong to this session")
    _permission_decisions[req.request_id] = {"action": req.action}
    event.set()
    return {"status": "ok"}


@router.post(
    "/sessions/{session_id}/ui-command-results",
    responses={404: {"description": "Not Found"}},
)
@handle_db_errors("remy.submit_ui_command_results")
async def submit_ui_command_results(
    session_id: uuid.UUID,
    req: UiCommandResultsBatch,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    if not await _is_ui_driving_enabled(principal.organisation_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MSG_UI_DRIVING_DISABLED_ORGANISATION,
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        logger.exception("remy.submit_ui_command_results")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.submit_ui_command_results.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    sid = str(session_id)
    registry = _get_registry()
    if registry is not None:
        results = [r.model_dump() for r in req.results]
        await registry.set_ui_command_results(sid, results)
        await registry.publish_ui_results(sid)
        return {"status": "ok"}
    event = _pending_ui_results.get(sid)
    if event is None:
        return {"status": "ok"}
    _ui_command_results[sid] = [r.model_dump() for r in req.results]
    event.set()
    return {"status": "ok"}


@router.post(
    "/sessions/{session_id}/reset-permissions",
    responses={404: {"description": "Not Found"}},
)
@handle_db_errors("remy.reset_session_permissions")
async def reset_session_permissions(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        logger.exception("remy.reset_session_permissions")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.reset_session_permissions.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    session_id_str = str(session_id)
    await _clear_session_approvals(session_id_str)
    _rate_limiters.pop(session_id_str, None)
    return {"status": "ok"}


@router.post(
    "/sessions/{session_id}/resume",
    responses={
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("remy.resume_session")
async def resume_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        logger.exception("remy.resume_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.resume_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    sid = str(session_id)
    registry = _get_registry()
    if registry is not None:
        await registry.publish_resume(sid)
        return {"status": "ok"}
    event = _resume_events.get(sid)
    if event is not None:
        event.set()
    return {"status": "ok"}


@router.post(
    "/sessions/{session_id}/stop",
    responses={
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("remy.stop_session")
async def stop_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        logger.exception("remy.stop_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.stop_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    sid = str(session_id)
    cancelled_result = [{"id": "", "name": "", "success": False, "error": "cancelled_by_user"}]
    registry = _get_registry()
    if registry is not None:
        await registry.set_ui_command_results(sid, cancelled_result)
        await registry.publish_ui_results(sid)
        await registry.publish_resume(sid)
        return {"status": "stopped"}
    # Set cancelled results so the loop sees cancel
    _ui_command_results[sid] = cancelled_result
    # Wake up the loop if paused
    resume_ev = _resume_events.pop(sid, None)
    if resume_ev is not None:
        resume_ev.set()
    # Wake up the loop if waiting for UI results
    ui_event = _pending_ui_results.pop(sid, None)
    if ui_event is not None:
        ui_event.set()
    return {"status": "stopped"}


@router.get(
    "/sessions/{session_id}/audit-trail",
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
    },
)
@handle_db_errors("remy.get_audit_trail")
async def get_audit_trail(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    if not principal.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system admins can access the audit trail.",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == "tool_result",
                )
                .order_by(ChatMessage.created_at.asc())
            )
            messages = result.scalars().all()

        trail = []
        for m in messages:
            tr = m.tool_results_json or {}
            result_data = tr.get("result")
            result_dict = result_data if isinstance(result_data, dict) else {}
            snapshot_data = result_dict.get("snapshotBefore")
            snapshot = snapshot_data if isinstance(snapshot_data, dict) else {}
            trail.append(
                {
                    "timestamp": m.created_at.isoformat() if m.created_at else None,
                    "action": tr.get("tool_name", ""),
                    "args": result_dict.get("args", {}),
                    "url": snapshot.get("url", ""),
                    "success": tr.get("success", False),
                    "error": tr.get("error"),
                }
            )

        return {"items": trail}
    except ProgrammingError:
        logger.exception("remy.get_audit_trail")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except Exception:
        logger.exception("remy.get_audit_trail.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.post(
    "/sessions/{session_id}/undo",
    responses={404: {"description": "Not Found"}},
)
@handle_db_errors("remy.undo_last_action")
async def undo_last_action(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)

            result = await session.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == "tool_result",
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
            last_result = result.scalar_one_or_none()

        if last_result is None:
            return {"status": "no_action", "detail": "No previous action to undo."}

        tr = last_result.tool_results_json or {}
        tool_name = tr.get("tool_name", "")
        tool_args = {}
        inner_result = tr.get("result")
        if isinstance(inner_result, dict):
            tool_args = inner_result.get("args", {})

        inverse: dict[str, Any] | None = None
        match tool_name:
            case "navigate":
                inverse = {"name": "go_back", "args": {}}
            case "go_back":
                inverse = {"name": "reload", "args": {}}
            case "fill":
                prior = tool_args.get("prior_value", tool_args.get("value", ""))
                inverse = {
                    "name": "fill",
                    "args": {"selector": tool_args.get("selector", ""), "value": prior},
                }
            case _:
                if tool_name in ("click", "select", "press"):
                    inverse = {"name": tool_name, "args": tool_args, "reversible": False}

        return {
            "status": "found" if inverse else "no_inverse",
            "last_action": tool_name,
            "undo_action": inverse,
        }
    except ProgrammingError:
        logger.exception("remy.undo_last_action")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_REMY_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("remy.undo_last_action.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
