"""Assistant Chat API — CRUD sessions, messages, and SSE streaming for LLM chat.

Endpoints:
    Sessions:
        GET    /api/v1/assistant/sessions             — list user's sessions
        POST   /api/v1/assistant/sessions             — create new session
        GET    /api/v1/assistant/sessions/{id}        — get session with message count
        PATCH  /api/v1/assistant/sessions/{id}        — rename session
        DELETE /api/v1/assistant/sessions/{id}        — delete session + messages

    Messages:
        GET    /api/v1/assistant/sessions/{id}/messages   — list messages for session
        POST   /api/v1/assistant/sessions/{id}/messages   — append a message

    Streaming:
        POST   /api/v1/assistant/sessions/{id}/stream     — SSE stream of LLM response
"""

import asyncio
import importlib
import json
import logging
import time as _time
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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

from modulo.api.constants import (
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_UNEXPECTED_ERROR,
    MSG_UNEXPECTED_ERROR_PLEASE_TRY,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session
from modulo.api.mcp_tool_registry import build_tool_registry, get_mcp_tool_definitions
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.core.assistant.config_service import AssistantConfigService
from modulo.core.assistant.skill_loader import SkillLoader
from modulo.core.audit_coverage import audited
from modulo.core.runtime_config.key_bridge import get_public_url
from modulo.core.ssrf import pinned_async_client
from modulo.db.models.assistant_message import ChatMessage
from modulo.db.models.assistant_session import ChatSession
from modulo.db.models.model_backend import ModelBackend
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.model_backends.base import ModelBackendBase
from modulo.settings import Settings, get_settings

_MSG_SESSION_NOT_FOUND = "Session not found"
_CODE_ASSISTANT_DATABASE_ERROR = "assistant.database_error"
_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again later."
_SSE_ERROR_PREFIX = "event: error\ndata: "


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

_NAV_TOOLS: dict[str, dict[str, Any]] = {
    "get_manifest": {
        "description": (
            "Resolve Modulo route details from the app manifest. Provide a route path to "
            "get one page's route and element details, or omit it to get the whole app map. "
            "Read-only: this does not move the user's browser."
        ),
        "parameters": {"path": {"type": "string", "description": "Optional route path to resolve."}},
    },
}
NAV_TOOL_NAMES: frozenset[str] = frozenset(_NAV_TOOLS)

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

# ── Redis registry (lazy-init, multi-worker capable) ─────────────────────

_redis_registry: Any | None = None


def _get_registry() -> Any | None:
    global _redis_registry
    if _redis_registry is None:
        redis_url = get_settings().redis_url
        if not redis_url:
            return None
        from modulo.core.assistant.redis_registry import AssistantRedisRegistry

        _redis_registry = AssistantRedisRegistry(redis_url)
    return _redis_registry


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
    page_context: str | None = Field(None, description="Current page context for Assistant's context-awareness.")
    system_prompt: str | None = Field(None, description="System prompt override.")
    exclude_navigation_tools: bool = Field(
        False,
        description="Exclude navigation tools (assistant-only mode).",
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _serialise_session(s: ChatSession, message_count: int = 0) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "account_id": str(s.account_id),
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
        return await decode_stored_secret_scoped(session, backend.credentials_ciphertext, fernet_key, org_id=org_id)
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
            async with await pinned_async_client(
                f"{base_url}/mcp/tools/call",
                timeout=60.0,
            ) as client:
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


async def _get_owned_session(
    session_id: uuid.UUID,
    principal: TenantPrincipal,
    db: AsyncSession,
) -> ChatSession:
    chat_session = await db.get(ChatSession, session_id)
    if chat_session is None or chat_session.account_id != principal.account_id:
        raise HTTPException(status_code=404, detail=_MSG_SESSION_NOT_FOUND)
    return chat_session


def _get_all_tool_definitions(include_navigation: bool = True) -> list[dict[str, Any]]:
    """Combine navigation and MCP tool definitions for the LLM's tools parameter."""
    tools: list[dict[str, Any]] = []
    if include_navigation:
        for name, schema in _NAV_TOOLS.items():
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


class _StreamContext:
    """Per-request state shared by the stream helpers (reduces arg passing)."""

    __slots__ = (
        "chat_session",
        "db_session",
        "mcp_base_url",
        "principal",
        "req",
        "session_id",
        "session_id_str",
        "settings",
    )

    def __init__(
        self,
        db_session: AsyncSession,
        principal: TenantPrincipal,
        session_id: uuid.UUID,
        req: StreamRequest,
        settings: Settings,
        chat_session: ChatSession,
    ) -> None:
        self.db_session = db_session
        self.principal = principal
        self.session_id = session_id
        self.req = req
        self.settings = settings
        self.chat_session = chat_session
        self.session_id_str = str(session_id)
        self.mcp_base_url = get_public_url(settings).rstrip("/")


class _StreamInit:
    """Result of stream initialisation (API key → pruned conversation)."""

    __slots__ = ("backend", "error_detail", "messages", "parent_msg_id")

    def __init__(
        self,
        error_detail: str | None = None,
        backend: ModelBackendBase | None = None,
        parent_msg_id: uuid.UUID | None = None,
        messages: list[BaseMessage] | None = None,
    ) -> None:
        self.error_detail = error_detail
        self.backend = backend
        self.parent_msg_id = parent_msg_id
        self.messages = messages or []


def _sse_tool_call_event(tool_result: dict[str, Any]) -> str:
    return f"event: tool_call\ndata: {json.dumps(tool_result)}\n\n"


async def _resolve_stream_api_key(ctx: _StreamContext) -> tuple[str | None, str | None]:
    """Return (api_key, error_detail). A non-None error_detail means no key resolved."""
    req = ctx.req
    if req.api_key:
        return req.api_key, None
    async with ctx.db_session.begin():
        await set_rls_org(ctx.db_session, ctx.principal.organisation_id)
        await set_rls_user_context(ctx.db_session, ctx.principal.account_id, ctx.principal.org_role)
        resolved = await _resolve_api_key(
            req.provider,
            ctx.principal.organisation_id,
            ctx.db_session,
            ctx.settings.fernet_key,
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
        logger.exception("assistant.backend_construction_failed")
        return None, f"Failed to initialize backend: {exc}"


async def _build_stream_system_prompt(ctx: _StreamContext, supports_tools: bool) -> str:
    async with ctx.db_session.begin():
        await set_rls_org(ctx.db_session, ctx.principal.organisation_id)
        skill_loader = SkillLoader(ctx.db_session)
        return await skill_loader.build_system_prompt(
            org_id=ctx.principal.organisation_id,
            user_id=ctx.principal.account_id,
            page_context=ctx.req.page_context,
            system_prompt_override=ctx.req.system_prompt,
            include_ui_tools_text=False,
        )


async def _save_stream_user_message(ctx: _StreamContext) -> uuid.UUID:
    async with ctx.db_session.begin():
        await set_rls_org(ctx.db_session, ctx.principal.organisation_id)
        user_msg = ChatMessage(
            organisation_id=ctx.principal.organisation_id,
            session_id=ctx.session_id,
            role="user",
            content=ctx.req.content,
        )
        ctx.db_session.add(user_msg)
        await ctx.db_session.flush()
        return user_msg.id


async def _build_stream_tools_param(
    backend: ModelBackendBase,
    ctx: _StreamContext,
) -> list[dict[str, Any]] | None:
    if not getattr(backend, "supports_tools", False):
        return None
    await build_tool_registry()
    return _get_all_tool_definitions(include_navigation=not ctx.req.exclude_navigation_tools)


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
            logger.exception("assistant.auto_naming_failed")


async def _add_assistant_message(
    ctx: _StreamContext,
    full_content: str,
    tool_calls: list[dict[str, Any]] | None,
    parent_msg_id: uuid.UUID | None,
) -> str:
    """Add an assistant-message row inside an already-open transaction (RLS set)."""
    assistant_msg = ChatMessage(
        organisation_id=ctx.principal.organisation_id,
        session_id=ctx.session_id,
        role="assistant",
        content=full_content or None,
        tool_calls_json={"tool_calls": tool_calls} if tool_calls else None,
        parent_id=parent_msg_id,
    )
    ctx.db_session.add(assistant_msg)
    await ctx.db_session.flush()
    return str(assistant_msg.id)


async def _finalise_stream_assistant_message(
    ctx: _StreamContext,
    full_content: str,
    parent_msg_id: uuid.UUID | None,
) -> str:
    """Persist the final assistant message (no tool calls) and auto-name the session."""
    async with ctx.db_session.begin():
        await set_rls_org(ctx.db_session, ctx.principal.organisation_id)
        msg_id = await _add_assistant_message(ctx, full_content, None, parent_msg_id)
        await _auto_name_stream_session(ctx.db_session, ctx.session_id, ctx.req, ctx.chat_session)
    return msg_id


async def _run_manifest_calls(
    manifest_calls: list[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """Resolve manifest tool calls locally (read-only; never drives the browser)."""
    from modulo.core.manifest import get_manifest

    manifest = get_manifest()
    for tool_call in manifest_calls:
        path = tool_call["args"].get("path")
        if path:
            result = {
                "route": manifest.get("routes", {}).get(path),
                "elements": manifest.get("elements", {}).get(path, []),
            }
        else:
            result = {
                "routes": {
                    route: {
                        "name": data.get("name"),
                        "testid": data.get("testid"),
                        "type": data.get("type"),
                        "sidebar_group": data.get("sidebar_group"),
                    }
                    for route, data in manifest.get("routes", {}).items()
                },
                "elements": manifest.get("elements", {}),
                "sidebar_groups": manifest.get("sidebar_groups", {}),
            }
        yield {
            "tool_call_id": tool_call["id"],
            "tool_name": tool_call["name"],
            "success": True,
            "result": result,
        }


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


def _tool_result_content(tool_result: dict[str, Any]) -> str:
    return json.dumps(tool_result.get("result", tool_result.get("error", "")))


async def _persist_assistant_and_tool_messages(
    ctx: _StreamContext,
    full_content: str,
    tool_calls: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    parent_msg_id: uuid.UUID | None,
) -> str:
    async with ctx.db_session.begin():
        await set_rls_org(ctx.db_session, ctx.principal.organisation_id)
        msg_id = await _add_assistant_message(ctx, full_content, tool_calls, parent_msg_id)

        for tr in tool_results:
            tool_msg = ChatMessage(
                organisation_id=ctx.principal.organisation_id,
                session_id=ctx.session_id,
                role="tool_result",
                content=_tool_result_content(tr),
                tool_results_json=tr,
                parent_id=uuid.UUID(msg_id),
            )
            ctx.db_session.add(tool_msg)
    return msg_id


async def _initialise_stream(ctx: _StreamContext) -> _StreamInit:
    """Run the stream preamble: API key → pruned conversation.

    Returns a ``_StreamInit``; a non-None ``error_detail`` means initialisation
    failed and the caller should emit the SSE error event and stop.
    """
    # 1. Resolve API key
    api_key, api_key_error = await _resolve_stream_api_key(ctx)
    if api_key_error is not None:
        return _StreamInit(error_detail=api_key_error)
    if api_key is None:
        return _StreamInit(error_detail="Failed to resolve API key for streaming")

    # 2. Create backend (needed before system prompt for supports_tools)
    backend, backend_error = _build_stream_backend(ctx.req, api_key)
    if backend_error is not None:
        return _StreamInit(error_detail=backend_error)
    if backend is None:
        return _StreamInit(error_detail="Failed to build model backend")

    # 3. Construct system prompt from config + skills
    supports_tools = getattr(backend, "supports_tools", False)
    system_prompt = await _build_stream_system_prompt(ctx, supports_tools)

    # 4. Save user message to DB
    parent_msg_id = await _save_stream_user_message(ctx)

    # 5. Reconstruct conversation
    async with ctx.db_session.begin():
        await set_rls_org(ctx.db_session, ctx.principal.organisation_id)
        langchain_messages = await _reconstruct_messages(ctx.db_session, ctx.session_id)

    # 6. Prepend system prompt
    if system_prompt:
        langchain_messages.insert(0, SystemMessage(content=system_prompt))

    # 7. Context window pruning
    context_window = (
        ctx.req.context_window_tokens
        if ctx.req.context_window_tokens is not None
        else (ctx.chat_session.context_window_tokens or 200000)
    )
    pruned_count = _prune_context_window(langchain_messages, context_window)
    if pruned_count:
        logger.info("Pruned %d messages from session %s", pruned_count, ctx.session_id)

    return _StreamInit(
        backend=backend,
        parent_msg_id=parent_msg_id,
        messages=langchain_messages,
    )


# ── Session endpoints ────────────────────────────────────────────────────


@router.get("/sessions", status_code=status.HTTP_200_OK)
@handle_db_errors("assistant.list_sessions")
async def list_sessions(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            total_q = select(func.count(ChatSession.id)).where(ChatSession.account_id == principal.account_id)
            total_result = await session.execute(total_q)
            total = total_result.scalar() or 0

            q = (
                select(ChatSession)
                .where(ChatSession.account_id == principal.account_id)
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
        logger.exception("assistant.list_sessions")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.list_sessions")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except Exception:
        logger.exception("assistant.list_sessions.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


async def _resolve_provider_model(
    session: AsyncSession,
    principal: TenantPrincipal,
    provider: str | None,
    model: str | None,
) -> tuple[str | None, str | None]:
    """Resolve (provider, model) for a new session.

    Falls back to the org's first configured model backend, then to the
    Assistant default provider/model, and finally to the opencode default model
    when no model could be resolved.
    """
    if provider is not None and model is not None:
        return provider, model

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
        config = await AssistantConfigService(session).get_config(principal.organisation_id)
        provider = provider or config.default_provider
        model = model or config.default_model

    if not model:
        default_models: dict[str, str] = {
            "opencode": "deepseek-v4-flash",
        }
        if provider in default_models:
            model = default_models[provider]
    return provider, model


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(audited("assistant_session_created", "assistant_session", principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors("assistant.create_session")
async def create_session(
    req: CreateSessionRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            max_sn = await session.execute(
                select(func.coalesce(func.max(ChatSession.session_number), 0)).where(
                    ChatSession.account_id == principal.account_id
                )
            )
            next_session_number = (max_sn.scalar() or 0) + 1

            provider, model = await _resolve_provider_model(session, principal, req.provider, req.model)

            chat_session = ChatSession(
                organisation_id=principal.organisation_id,
                account_id=principal.account_id,
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
        logger.exception("assistant.create_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.create_session")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except Exception:
        logger.exception("assistant.create_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.get(
    "/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Session not found"}},
)
@handle_db_errors("assistant.get_session")
async def get_session(
    session_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
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
        logger.exception("assistant.get_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.get_session")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("assistant.get_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.patch(
    "/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Session not found"}},
    dependencies=[
        Depends(audited("assistant_session_renamed", "assistant_session", principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors("assistant.rename_session")
async def rename_session(
    session_id: uuid.UUID,
    req: RenameSessionRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)

            chat_session.name = req.name
            await session.flush()

        return _serialise_session(chat_session)
    except ProgrammingError:
        logger.exception("assistant.rename_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.rename_session")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("assistant.rename_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Session not found"}},
    dependencies=[
        Depends(
            audited(
                "assistant_session_deleted",
                "assistant_session",
                principal_dep=get_current_tenant_user,
                fail_closed=True,
            ),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        )
    ],
)
@handle_db_errors("assistant.delete_session")
async def delete_session(
    session_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)

            await session.delete(chat_session)

        # Clean up Redis registry if active
        session_id_str = str(session_id)
        registry = _get_registry()
        if registry is not None:
            try:
                await registry.clear_session(session_id_str)
            except Exception:
                logger.exception("assistant.redis_cleanup_failed")

        return {"status": "deleted", "id": str(session_id)}
    except ProgrammingError:
        logger.exception("assistant.delete_session")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.delete_session")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("assistant.delete_session.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ── Message endpoints ────────────────────────────────────────────────────


@router.get(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Session not found"}},
)
@handle_db_errors("assistant.list_messages")
async def list_messages(
    session_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
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
        logger.exception("assistant.list_messages")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.list_messages")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("assistant.list_messages.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Session not found"}},
    dependencies=[
        Depends(audited("assistant_message_appended", "assistant_message", principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors("assistant.append_message")
async def append_message(
    session_id: uuid.UUID,
    req: AppendMessageRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
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
        logger.exception("assistant.append_message")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.append_message")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("assistant.append_message.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ── Streaming endpoint ───────────────────────────────────────────────────


async def _stream_backend_tokens(
    backend: Any,
    langchain_messages: list[Any],
    tools_param: Any,
    request: Request,
    tool_call_buffers: dict[int, dict[str, Any]],
    state: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Stream one LLM turn's tokens as SSE events, tracking content in ``state``.

    Sets ``state["disconnected"]`` when the client disconnects mid-stream.
    """
    state["full_content"] = ""
    async for chunk in backend.stream(langchain_messages, tools=tools_param):
        if await request.is_disconnected():
            state["disconnected"] = True
            return
        if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
            state["full_content"] += chunk.content
            yield f"event: token\ndata: {json.dumps({'token': chunk.content})}\n\n"
            _accumulate_tool_call_chunks(chunk, tool_call_buffers)


async def _stream_tool_events(
    tool_calls: list[dict[str, Any]],
    ctx: _StreamContext,
    tool_results: list[dict[str, Any]],
) -> AsyncGenerator[str, None]:
    """Execute navigation and MCP tool calls, yielding SSE events."""
    navigation_calls = [tc for tc in tool_calls if tc["name"] in NAV_TOOL_NAMES]
    mcp_calls = [tc for tc in tool_calls if tc["name"] not in NAV_TOOL_NAMES]
    async for tr in _run_manifest_calls(navigation_calls):
        tool_results.append(tr)
        yield _sse_tool_call_event(tr)
    async for tr in _run_mcp_tool_calls(mcp_calls, ctx.req, ctx.mcp_base_url):
        tool_results.append(tr)
        yield _sse_tool_call_event(tr)


def _append_turn_messages(
    langchain_messages: list[Any],
    full_content: str,
    tool_calls: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> None:
    """Append the assistant message and tool results to the conversation for the next turn."""
    langchain_messages.append(AIMessage(content=full_content, tool_calls=tool_calls))
    langchain_messages.extend(
        ToolMessage(content=_tool_result_content(tr), tool_call_id=tr["tool_call_id"]) for tr in tool_results
    )


def _ping_event_if_due(state: dict[str, Any]) -> str | None:
    """Return a keepalive ping event when one is due, updating the last-ping timestamp."""
    now = _time.monotonic()
    if now - state["last_ping_at"] < 15:
        return None
    state["last_ping_at"] = now
    return "event: ping\ndata: {}\n\n"


async def _run_stream_loop(
    ctx: _StreamContext,
    request: Request,
    backend: Any,
    langchain_messages: list[Any],
    parent_msg_id: uuid.UUID | None,
    state: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Multi-turn agentic loop: stream tokens, execute tools, persist, ping."""
    while True:
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        tools_param = await _build_stream_tools_param(backend, ctx)

        async for event in _stream_backend_tokens(
            backend, langchain_messages, tools_param, request, tool_call_buffers, state
        ):
            yield event
        if state["disconnected"]:
            return

        tool_calls = _reconstruct_tool_calls(tool_call_buffers)
        if not tool_calls:
            # LLM done — save assistant message and exit loop
            state["msg_id"] = await _finalise_stream_assistant_message(ctx, state["full_content"], parent_msg_id)
            return

        tool_results: list[dict[str, Any]] = []
        async for event in _stream_tool_events(tool_calls, ctx, tool_results):
            yield event

        # Add to conversation for next LLM turn, then persist
        _append_turn_messages(langchain_messages, state["full_content"], tool_calls, tool_results)
        state["msg_id"] = await _persist_assistant_and_tool_messages(
            ctx, state["full_content"], tool_calls, tool_results, parent_msg_id
        )

        # Ping keepalive if idle
        ping = _ping_event_if_due(state)
        if ping is not None:
            yield ping


async def _stream_event_generator(
    session: AsyncSession,
    request: Request,
    principal: TenantPrincipal,
    session_id: uuid.UUID,
    req: StreamRequest,
    settings: Settings,
    chat_session: ChatSession,
) -> AsyncGenerator[str, None]:
    """SSE event generator — agentic loop with multi-turn LLM + MCP tools."""
    msg_id: str | None = None
    try:
        # expire_on_commit=False matches the DI session factory: this is an
        # autobegin=False session, so a post-commit attribute read that
        # triggers a refresh would raise InvalidRequestError (the defect class
        # behind FAR-808/FAR-820) and be swallowed as a silent stream failure.
        async with AsyncSession(session.bind, expire_on_commit=False, autobegin=False) as db_session:
            ctx = _StreamContext(db_session, principal, session_id, req, settings, chat_session)

            # 1-7. Resolve API key → build backend → system prompt →
            # save user message → reconstruct → prepend → prune.
            init = await _initialise_stream(ctx)
            if init.error_detail is not None:
                yield f"event: error\ndata: {json.dumps({'detail': init.error_detail})}\n\n"
                return

            backend = init.backend
            if backend is None:
                yield f"event: error\ndata: {json.dumps({'detail': MSG_UNEXPECTED_ERROR})}\n\n"
                return

            state: dict[str, Any] = {
                "disconnected": False,
                "full_content": "",
                "should_break": False,
                "msg_id": None,
                "last_ping_at": _time.monotonic(),
            }
            async for event in _run_stream_loop(ctx, request, backend, init.messages, init.parent_msg_id, state):
                yield event
            msg_id = state["msg_id"]

        yield f"event: done\ndata: {json.dumps({'message_id': msg_id})}\n\n"

    except HTTPException as exc:
        yield f"event: error\ndata: {json.dumps({'detail': exc.detail})}\n\n"
    except ProgrammingError:
        logger.exception("Assistant streaming error — missing DB table or schema")
        yield (_SSE_ERROR_PREFIX + json.dumps({"detail": MSG_FEATURE_NOT_AVAILABLE}) + "\n\n")
    except SQLAlchemyError:
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        yield (_SSE_ERROR_PREFIX + json.dumps({"detail": _MSG_DATABASE_ERROR_PLEASE_TRY}) + "\n\n")
    except Exception:
        logger.exception("Assistant streaming error")
        yield f"event: error\ndata: {json.dumps({'detail': MSG_UNEXPECTED_ERROR_PLEASE_TRY})}\n\n"


@router.post(
    "/sessions/{session_id}/stream",
    responses={
        404: {"description": "Session not found"},
        403: {"description": "Forbidden"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
    dependencies=[
        Depends(audited("assistant_chat_streamed", "assistant_session", principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors("assistant.stream_chat")
async def stream_chat(
    session_id: uuid.UUID,
    req: StreamRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    # Validate the session exists and belongs to user
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await _get_owned_session(session_id, principal, session)
    except ProgrammingError:
        logger.exception("assistant.stream_chat")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "assistant.stream_chat")
        logger.exception(_CODE_ASSISTANT_DATABASE_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None

    session_id_str = str(session_id)

    logger.info(
        "assistant.stream_started",
        extra={
            "org_id": str(principal.organisation_id),
            "session_id": session_id_str,
            "exclude_navigation_tools": req.exclude_navigation_tools,
        },
    )

    return StreamingResponse(
        _stream_event_generator(session, request, principal, session_id, req, settings, chat_session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
