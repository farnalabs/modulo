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
import json
import logging
import time as _time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from httpx import AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from modulo.api.dependencies import get_db_session
from modulo.api.ui_tools import _UI_TOOLS, DESTRUCTIVE_PATTERNS, UI_TOOL_NAMES, WRITE_TOOLS
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.remy.config_service import RemyConfig, RemyConfigService
from modulo.core.remy.skill_loader import SkillLoader
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.remy_message import ChatMessage
from modulo.db.models.remy_session import ChatSession
from modulo.db.rls import set_rls_org
from modulo.model_backends.ai21 import Ai21Backend
from modulo.model_backends.anthropic import AnthropicBackend
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.deepseek import DeepSeekBackend
from modulo.model_backends.fireworks import FireworksBackend
from modulo.model_backends.gemini import GeminiBackend
from modulo.model_backends.grok import GrokBackend
from modulo.model_backends.groq import GroqBackend
from modulo.model_backends.openai import OpenAIBackend
from modulo.model_backends.opencode import OpenCodeBackend
from modulo.model_backends.openrouter import OpenRouterBackend
from modulo.model_backends.perplexity import PerplexityBackend
from modulo.model_backends.qwen import QwenBackend
from modulo.model_backends.togetherai import TogetherAIBackend
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/remy", tags=["remy"])

# ── Provider → backend class mapping ──────────────────────────────────────
_SIMPLE_BACKENDS: dict[str, type[ModelBackendBase]] = {
    "ai21": Ai21Backend,
    "anthropic": AnthropicBackend,
    "deepseek": DeepSeekBackend,
    "fireworks": FireworksBackend,
    "gemini": GeminiBackend,
    "grok": GrokBackend,
    "groq": GroqBackend,
    "openai": OpenAIBackend,
    "opencode": OpenCodeBackend,
    "openrouter": OpenRouterBackend,
    "perplexity": PerplexityBackend,
    "qwen": QwenBackend,
    "togetherai": TogetherAIBackend,
}

# ── In-memory event registry (single-worker only) ────────────────────────
# For multi-worker deployments, replace with Redis pub/sub.

_pending_permissions: dict[str, tuple[asyncio.Event, str]] = {}
_permission_decisions: dict[str, dict] = {}
_pending_ui_results: dict[str, asyncio.Event] = {}
_ui_command_results: dict[str, list[dict]] = {}
_session_approvals: dict[str, dict[str, dict]] = {}
_SESSION_APPROVAL_TTL = timedelta(minutes=30)

# ── Pydantic schemas ─────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    provider: str | None = Field(None, description="LLM provider (e.g. openai, anthropic). Auto-detected if omitted.")
    model: str | None = Field(None, description="Model ID (e.g. gpt-4o, claude-sonnet-4-20250514). Auto-detected if omitted.")
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
        None, ge=1024, le=1_000_000,
        description="Override context window (defaults to session value)",
    )


class PermissionResponse(BaseModel):
    request_id: str
    action: str  # "approve" | "reject" | "approve_for_session"


class UiCommandResultItem(BaseModel):
    id: str
    name: str
    success: bool
    result: dict | None = None
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
                tool_call_id = m.tool_results_json.get("tool_call_id", "")
            return ToolMessage(content=m.content or "", tool_call_id=tool_call_id)
        case "summary":
            return SystemMessage(content=m.content or "")
        case _:
            logger.warning("Unknown message role %r, treating as user message", m.role)
            return HumanMessage(content=m.content or "")


def _build_backend(provider: str, model: str, api_key: str, **kwargs: Any) -> ModelBackendBase:
    cls = _SIMPLE_BACKENDS.get(provider)
    if cls is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {provider!r}. Supported: {', '.join(sorted(_SIMPLE_BACKENDS))}",
        )
    return cls(api_key=api_key, model_id=model, **kwargs)


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
    async with AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base_url}/mcp/tools/call",
            json={"tool": tool_name, "arguments": arguments},
            headers={"Authorization": f"Bearer {mcp_api_key}"},
        )
        resp.raise_for_status()
        return resp.json()


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
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    db_messages = result.scalars().all()
    return [_message_to_langchain(m) for m in db_messages]


# ── UI command helpers ───────────────────────────────────────────────────


async def _validate_session_ownership(
    session_id: uuid.UUID,
    principal: AuthenticatedPrincipal,
    db: AsyncSession,
) -> ChatSession:
    chat_session = await db.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != principal.account_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return chat_session


def _has_destructive_pattern(selector: str) -> bool:
    lower = selector.lower()
    return any(p in lower for p in DESTRUCTIVE_PATTERNS)


def _resolve_tool_permission(config: RemyConfig, tool_name: str, args: dict[str, Any]) -> str:
    """Returns 'always_allowed', 'requires_approval', or 'disabled'."""
    # 1. Per-tool user override (highest priority)
    overrides = config.tool_permissions or {}
    if tool_name in overrides:
        return overrides[tool_name]

    # 2. Mode-based defaults
    mode = config.permission_mode
    if mode == "locked_down":
        base = "requires_approval" if tool_name in WRITE_TOOLS or tool_name == "press" else "always_allowed"
    elif mode == "full_auto":
        base = "always_allowed"
    else:
        base = "requires_approval" if tool_name == "press" else "always_allowed"

    # 3. Destructive pattern override (applies regardless of mode)
    if base == "always_allowed" and tool_name in WRITE_TOOLS:
        selector = args.get("selector", "")
        if _has_destructive_pattern(selector):
            return "requires_approval"

    return base


def _is_approved_for_session(session_id: str, tool_name: str, page_path: str) -> bool:
    session_approvals = _session_approvals.get(session_id)
    if not session_approvals:
        return False
    now = datetime.now(UTC)
    stale_keys = [k for k, v in session_approvals.items() if now >= v["expires_at"]]
    for k in stale_keys:
        del session_approvals[k]
    approval = session_approvals.get(tool_name)
    return bool(approval and now < approval["expires_at"] and approval["page_path"] == page_path)


def _set_session_approval(session_id: str, tool_name: str, page_path: str) -> None:
    if session_id not in _session_approvals:
        _session_approvals[session_id] = {}
    _session_approvals[session_id][tool_name] = {
        "page_path": page_path,
        "expires_at": datetime.now(UTC) + _SESSION_APPROVAL_TTL,
    }


def clear_all_session_approvals() -> None:
    """Clear all in-memory session approvals (called on logout)."""
    _session_approvals.clear()


def _get_all_tool_definitions() -> list[dict[str, Any]]:
    """Combine UI tool definitions for the LLM's tools parameter."""
    tools: list[dict[str, Any]] = []
    for name, schema in _UI_TOOLS.items():
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": schema["description"],
                "parameters": {
                    "type": "object",
                    "properties": schema["parameters"],
                },
            },
        })
    return tools


# ── Session endpoints ────────────────────────────────────────────────────


@router.get("/sessions", status_code=status.HTTP_200_OK)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    req: CreateSessionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            max_sn = await session.execute(
                select(func.coalesce(func.max(ChatSession.session_number), 0)).where(
                    ChatSession.user_id == principal.account_id
                )
            )
            next_session_number = max_sn.scalar() + 1

            provider = req.provider
            model = req.model

            if provider is None or model is None:
                mb_result = await session.execute(
                    select(ModelBackend).where(
                        ModelBackend.organisation_id == principal.organisation_id,
                        ModelBackend.credentials_ciphertext.is_not(None),
                    ).limit(1)
                )
                mb = mb_result.scalar_one_or_none()
                if mb:
                    provider = provider or mb.provider
                    model = model or mb.model_id
                else:
                    config = await RemyConfigService(session).get_config(principal.organisation_id)
                    provider = provider or config.default_provider
                    model = model or config.default_model

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
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


@router.get("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def get_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.user_id != principal.account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

            count_q = select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session_id)
            count_result = await session.execute(count_q)
            msg_count = count_result.scalar() or 0

        return _serialise_session(chat_session, msg_count)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


@router.patch("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def rename_session(
    session_id: uuid.UUID,
    req: RenameSessionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.user_id != principal.account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

            chat_session.name = req.name
            await session.flush()

        return _serialise_session(chat_session)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


@router.delete("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def delete_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.user_id != principal.account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

            await session.delete(chat_session)

        return {"status": "deleted", "id": str(session_id)}
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


# ── Message endpoints ────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/messages", status_code=status.HTTP_200_OK)
async def list_messages(
    session_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.user_id != principal.account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

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
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_201_CREATED)
async def append_message(
    session_id: uuid.UUID,
    req: AppendMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.user_id != principal.account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

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
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None


# ── Streaming endpoint ───────────────────────────────────────────────────


@router.post("/sessions/{session_id}/stream")
async def stream_chat(
    session_id: uuid.UUID,
    req: StreamRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    # Validate the session exists and belongs to user
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.user_id != principal.account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None

    mcp_base_url = settings.modulo_public_url.rstrip("/")

    session_id_str = str(session_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        """SSE event generator — agentic loop with multi-turn LLM + UI commands."""
        msg_id: str | None = None
        last_ping_at = _time.monotonic()
        parent_msg_id: uuid.UUID | None = None
        try:
            async with AsyncSession(session.bind) as db_session:
                # 1. Resolve API key
                api_key = req.api_key
                if not api_key:
                    async with db_session.begin():
                        await set_rls_org(db_session, principal.organisation_id)
                        resolved = await _resolve_api_key(
                            req.provider, principal.organisation_id, db_session, settings.fernet_key,
                        )
                    if resolved is None:
                        msg = (
                            f"No active {req.provider} API key configured. "
                            "Add one in Settings > Model Backends or provide an api_key."
                        )
                        yield f"event: error\ndata: {json.dumps({'detail': msg})}\n\n"
                        return
                    api_key = resolved

                # 2. Create backend (needed before system prompt for supports_tools)
                try:
                    backend = _build_backend(req.provider, req.model, api_key)
                except HTTPException as exc:
                    yield f"event: error\ndata: {json.dumps({'detail': exc.detail})}\n\n"
                    return

                # 3. Construct system prompt from config + skills
                supports_tools = getattr(backend, 'supports_tools', False)
                async with db_session.begin():
                    await set_rls_org(db_session, principal.organisation_id)
                    skill_loader = SkillLoader(db_session)
                    system_prompt = await skill_loader.build_system_prompt(
                        org_id=principal.organisation_id,
                        user_id=principal.account_id,
                        page_context=req.page_context,
                        system_prompt_override=req.system_prompt,
                        include_ui_tools_text=not supports_tools,
                    )

                # 4. Save user message to DB
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
                    parent_msg_id = user_msg.id

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
                budget = int(context_window * 0.8)
                total_tokens = sum(max(1, len(m.content or "") // 4) for m in langchain_messages)
                pruned_count = 0
                while total_tokens > budget and len(langchain_messages) > 2:
                    removed = langchain_messages.pop(1)
                    total_tokens -= max(1, len(removed.content or "") // 4)
                    pruned_count += 1
                if pruned_count:
                    logger.info("Pruned %d messages from session %s", pruned_count, session_id)

                # ── Agentic loop ────────────────────────────────────────
                while True:
                    full_content = ""
                    tool_call_buffers: dict[int, dict[str, Any]] = {}

                    tools_param = None
                    if getattr(backend, 'supports_tools', False):
                        tools_param = _get_all_tool_definitions()

                    async for chunk in backend.stream(langchain_messages, tools=tools_param):
                        if await request.is_disconnected():
                            return
                        if isinstance(chunk, AIMessageChunk):
                            if chunk.content:
                                full_content += chunk.content
                                yield f"event: token\ndata: {json.dumps({'token': chunk.content})}\n\n"
                            if chunk.tool_call_chunks:
                                for tc in chunk.tool_call_chunks:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_call_buffers:
                                        tool_call_buffers[idx] = {
                                            "id": tc.get("id", ""),
                                            "name": tc.get("name", ""),
                                            "args": tc.get("args", ""),
                                        }
                                    else:
                                        tool_call_buffers[idx]["args"] += tc.get("args", "")

                    if await request.is_disconnected():
                        return

                    tool_calls = _reconstruct_tool_calls(tool_call_buffers)

                    if not tool_calls:
                        # LLM done — save assistant message and exit loop
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
                        break

                    # Separate UI vs MCP tool calls
                    ui_tool_calls = [tc for tc in tool_calls if tc["name"] in UI_TOOL_NAMES]
                    mcp_tool_calls = [tc for tc in tool_calls if tc["name"] not in UI_TOOL_NAMES]

                    tool_results: list[dict[str, Any]] = []

                    # Execute MCP tools
                    if mcp_tool_calls:
                        if not req.mcp_api_key:
                            yield (
                                "event: error\ndata: "
                                + json.dumps({"detail": "Tool execution requires an MCP API key"})
                                + "\n\n"
                            )
                            return
                        for tc in mcp_tool_calls:
                            try:
                                result = await _call_mcp_tool(
                                    tool_name=tc["name"],
                                    arguments=tc["args"],
                                    mcp_api_key=req.mcp_api_key,
                                    base_url=mcp_base_url,
                                )
                                tool_results.append({
                                    "tool_call_id": tc["id"], "tool_name": tc["name"],
                                    "success": True, "result": result,
                                })
                                yield f"event: tool_call\ndata: {json.dumps(tool_results[-1])}\n\n"
                            except Exception as exc:
                                logger.exception("MCP tool call failed: %r", tc["name"])
                                err_msg = f"{type(exc).__name__}: {exc}"[:200]
                                tool_results.append({
                                    "tool_call_id": tc["id"], "tool_name": tc["name"],
                                    "success": False, "error": err_msg,
                                })
                                yield f"event: tool_call\ndata: {json.dumps(tool_results[-1])}\n\n"

                    # Handle get_manifest calls server-side
                    manifest_calls = [tc for tc in ui_tool_calls if tc["name"] == "get_manifest"]
                    ui_tool_calls = [tc for tc in ui_tool_calls if tc["name"] != "get_manifest"]

                    for tc in manifest_calls:
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
                        tool_results.append({
                            "tool_call_id": tc["id"],
                            "tool_name": "get_manifest",
                            "success": True,
                            "result": result,
                        })
                        yield f"event: tool_call\ndata: {json.dumps(tool_results[-1])}\n\n"

                    # Handle UI tools
                    if ui_tool_calls:
                        config_service = RemyConfigService(db_session)
                        config = await config_service.get_config(principal.organisation_id)

                        approved_calls: list[dict[str, Any]] = []
                        pending_permission_calls: list[dict[str, Any]] = []

                        for tc in ui_tool_calls:
                            perm = _resolve_tool_permission(config, tc["name"], tc["args"])
                            if perm == "disabled":
                                continue
                            elif perm == "requires_approval":
                                page_path = req.page_context or ""
                                if not _is_approved_for_session(
                                    session_id_str, tc["name"], page_path,
                                ):
                                    pending_permission_calls.append(tc)
                                    continue
                            approved_calls.append(tc)

                        if pending_permission_calls:
                            req_id = str(uuid.uuid4())
                            yield f"event: permission_request\ndata: {json.dumps({
                                'request_id': req_id,
                                'tools': [{'name': tc['name'], 'args': tc['args']}
                                          for tc in pending_permission_calls],
                            })}\n\n"

                            event = asyncio.Event()
                            _pending_permissions[req_id] = (event, session_id_str)
                            try:
                                await asyncio.wait_for(event.wait(), timeout=60.0)
                                decision = _permission_decisions.pop(req_id, {"action": "reject"})
                                if decision["action"] in ("approve", "approve_for_session"):
                                    approved_calls.extend(pending_permission_calls)
                                    if decision["action"] == "approve_for_session":
                                        for tc in pending_permission_calls:
                                            _set_session_approval(
                                                session_id_str, tc["name"], req.page_context or "",
                                            )
                            except TimeoutError:
                                pass
                            finally:
                                _pending_permissions.pop(req_id, None)

                        if approved_calls:
                            event = asyncio.Event()
                            _pending_ui_results[session_id_str] = event

                            yield f"event: ui_command_batch\ndata: {json.dumps({
                                'commands': approved_calls,
                            })}\n\n"

                            try:
                                await asyncio.wait_for(event.wait(), timeout=120.0)
                                results = _ui_command_results.pop(session_id_str, [])
                            except TimeoutError:
                                results = []
                            finally:
                                _pending_ui_results.pop(session_id_str, None)

                            for r in results:
                                tool_results.append({
                                    "tool_call_id": r.get("id", ""),
                                    "tool_name": r.get("name", ""),
                                    "success": r.get("success", False),
                                    "result": r.get("result"),
                                    "error": r.get("error"),
                                })
                                yield f"event: tool_call\ndata: {json.dumps(tool_results[-1])}\n\n"

                            if all(r.get("error") == "cancelled_by_user" for r in results):
                                completed_count = sum(
                                    1 for r in results if r.get("error") != "cancelled_by_user"
                                )
                                skipped_count = len(results) - completed_count
                                yield f"event: abort_summary\ndata: {json.dumps({
                                    'completed': completed_count, 'skipped': skipped_count,
                                })}\n\n"
                                break

                    # Add to conversation for next LLM turn
                    langchain_messages.append(
                        AIMessage(content=full_content, tool_calls=tool_calls)
                    )
                    for tr in tool_results:
                        langchain_messages.append(ToolMessage(
                            content=json.dumps(tr.get("result", tr.get("error", ""))),
                            tool_call_id=tr["tool_call_id"],
                        ))

                    # Save to DB
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
                                content=json.dumps(tr.get("result", tr.get("error", ""))),
                                tool_results_json=tr,
                                parent_id=assistant_msg.id,
                            )
                            db_session.add(tool_msg)

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
            yield (
                "event: error\ndata: "
                + json.dumps({"detail": "Feature is not available. Run database migrations to enable it."})
                + "\n\n"
            )
        except SQLAlchemyError:
            logger.exception("remy.database_error")
            yield (
                "event: error\ndata: "
                + json.dumps({"detail": "Database error. Please try again later."})
                + "\n\n"
            )
        except Exception as exc:
            logger.exception("Remy streaming error")
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

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


@router.post("/sessions/{session_id}/permission-response")
async def submit_permission_response(
    session_id: uuid.UUID,
    req: PermissionResponse,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None

    entry = _pending_permissions.get(req.request_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Permission request not found or expired")
    event, req_session_id = entry
    if req_session_id != str(session_id):
        raise HTTPException(status_code=403, detail="Permission request does not belong to this session")
    _permission_decisions[req.request_id] = {"action": req.action}
    event.set()
    return {"status": "ok"}


@router.post("/sessions/{session_id}/ui-command-results")
async def submit_ui_command_results(
    session_id: uuid.UUID,
    req: UiCommandResultsBatch,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None

    sid = str(session_id)
    event = _pending_ui_results.get(sid)
    if event is None:
        raise HTTPException(status_code=404, detail="No pending UI command batch")
    _ui_command_results[sid] = [r.model_dump() for r in req.results]
    event.set()
    return {"status": "ok"}


@router.post("/sessions/{session_id}/reset-permissions")
async def reset_session_permissions(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_session_ownership(session_id, principal, session)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("remy.database_error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database error. Please try again later.",
        ) from None

    _session_approvals.pop(str(session_id), None)
    return {"status": "ok"}
