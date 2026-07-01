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
"""

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from httpx import AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
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
    "openrouter": OpenRouterBackend,
    "perplexity": PerplexityBackend,
    "qwen": QwenBackend,
    "togetherai": TogetherAIBackend,
}

# ── Pydantic schemas ─────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    provider: str = Field(..., description="LLM provider (e.g. openai, anthropic)")
    model: str = Field(..., description="Model ID (e.g. gpt-4o, claude-sonnet-4-20250514)")
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
    context_window_tokens: int | None = Field(None, ge=1024, le=1_000_000, description="Override context window (defaults to session value)")
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


async def _reconstruct_messages(session: AsyncSession, session_id: uuid.UUID) -> list[BaseMessage]:
    result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    db_messages = result.scalars().all()
    return [_message_to_langchain(m) for m in db_messages]


# ── Session endpoints ────────────────────────────────────────────────────


@router.get("/sessions", status_code=status.HTTP_200_OK)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
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


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        chat_session = ChatSession(
            organisation_id=principal.organisation_id,
            user_id=principal.account_id,
            name=body.name,
            provider=body.provider,
            model=body.model,
            context_window_tokens=body.context_window_tokens,
        )
        session.add(chat_session)
        await session.flush()

    return _serialise_session(chat_session)


@router.get("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def get_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        chat_session = await session.get(ChatSession, session_id)
        if chat_session is None or chat_session.user_id != principal.account_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        count_q = select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session_id)
        count_result = await session.execute(count_q)
        msg_count = count_result.scalar() or 0

    return _serialise_session(chat_session, msg_count)


@router.patch("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def rename_session(
    session_id: uuid.UUID,
    body: RenameSessionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        chat_session = await session.get(ChatSession, session_id)
        if chat_session is None or chat_session.user_id != principal.account_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        chat_session.name = body.name
        await session.flush()

    return _serialise_session(chat_session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def delete_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        chat_session = await session.get(ChatSession, session_id)
        if chat_session is None or chat_session.user_id != principal.account_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        await session.delete(chat_session)

    return {"status": "deleted", "id": str(session_id)}


# ── Message endpoints ────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/messages", status_code=status.HTTP_200_OK)
async def list_messages(
    session_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
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


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_201_CREATED)
async def append_message(
    session_id: uuid.UUID,
    body: AppendMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        chat_session = await session.get(ChatSession, session_id)
        if chat_session is None or chat_session.user_id != principal.account_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        msg = ChatMessage(
            organisation_id=principal.organisation_id,
            session_id=session_id,
            role=body.role,
            content=body.content,
            tool_calls_json=body.tool_calls_json,
            tool_results_json=body.tool_results_json,
            token_count=body.token_count,
            parent_id=body.parent_id,
        )
        session.add(msg)
        await session.flush()

    return _serialise_message(msg)


# ── Streaming endpoint ───────────────────────────────────────────────────


@router.post("/sessions/{session_id}/stream")
async def stream_chat(
    session_id: uuid.UUID,
    body: StreamRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    # Validate the session exists and belongs to user
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        chat_session = await session.get(ChatSession, session_id)
        if chat_session is None or chat_session.user_id != principal.account_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    mcp_base_url = settings.modulo_public_url.rstrip("/")

    async def event_generator() -> AsyncGenerator[str, None]:
        """SSE event generator for the streaming LLM response."""
        msg_id: str | None = None
        try:
            # Use a single DB session for all operations inside the generator
            async with AsyncSession(session.bind) as db_session:
                # 1. Construct system prompt from config + skills
                skill_loader = SkillLoader(db_session)
                system_prompt = await skill_loader.build_system_prompt(
                    org_id=principal.organisation_id,
                    user_id=principal.account_id,
                    page_context=body.page_context,
                )
                if body.system_prompt:
                    system_prompt = body.system_prompt

                # 2. Save the user message to DB
                async with db_session.begin():
                    await set_rls_org(db_session, principal.organisation_id)
                    user_msg = ChatMessage(
                        organisation_id=principal.organisation_id,
                        session_id=session_id,
                        role="user",
                        content=body.content,
                    )
                    db_session.add(user_msg)
                    await db_session.flush()

                # 3. Reconstruct conversation from DB
                async with db_session.begin():
                    await set_rls_org(db_session, principal.organisation_id)
                    langchain_messages = await _reconstruct_messages(db_session, session_id)

                # 4. Prepend system prompt
                if system_prompt:
                    langchain_messages.insert(0, SystemMessage(content=system_prompt))

                # 5. Enforce context window — prune oldest messages if over budget
                context_window = (
                    body.context_window_tokens
                    if body.context_window_tokens is not None
                    else (chat_session.context_window_tokens or 200000)
                )
                budget = int(context_window * 0.8)
                total_tokens = sum(
                    max(1, len(m.content or "") // 4) for m in langchain_messages
                )
                pruned_count = 0
                while total_tokens > budget and len(langchain_messages) > 2:
                    removed = langchain_messages.pop(1)
                    total_tokens -= max(1, len(removed.content or "") // 4)
                    pruned_count += 1
                if pruned_count:
                    logger.info(
                        "Pruned %d messages from session %s to fit context window",
                        pruned_count, session_id,
                    )

                # 6. Resolve API key (from request or DB backend)
                api_key = body.api_key
                if not api_key:
                    resolved = await _resolve_api_key(
                        body.provider,
                        principal.organisation_id,
                        db_session,
                        settings.fernet_key,
                    )
                    if resolved is None:
                        msg = f"No active {body.provider} API key configured. "
                        msg += "Add one in Settings > Model Backends or provide an api_key."
                        yield f"event: error\ndata: {json.dumps({'detail': msg})}\n\n"
                        return
                    api_key = resolved

                backend = _build_backend(body.provider, body.model, api_key)

                # 7. Stream tokens from the LLM
                full_content = ""
                tool_call_buffers: dict[int, dict[str, Any]] = {}

                async for chunk in backend.stream(langchain_messages):
                    if await request.is_disconnected():
                        break
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

                # 8. Reconstruct tool calls from accumulated chunks
                tool_calls = []
                for idx in sorted(tool_call_buffers.keys()):
                    buf = tool_call_buffers[idx]
                    try:
                        parsed_args = json.loads(buf["args"]) if buf["args"] else {}
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse tool call args for %r: %r", buf["name"], buf["args"][:200])
                        parsed_args = {}
                    tool_calls.append({
                        "id": buf["id"],
                        "name": buf["name"],
                        "args": parsed_args,
                    })

                # 9. Execute tool calls via MCP
                tool_results: list[dict[str, Any]] = []
                if tool_calls and body.mcp_api_key:
                    for tc in tool_calls:
                        try:
                            result = await _call_mcp_tool(
                                tool_name=tc["name"],
                                arguments=tc["args"],
                                mcp_api_key=body.mcp_api_key,
                                base_url=mcp_base_url,
                            )
                            tool_results.append({
                                "tool_call_id": tc["id"],
                                "tool_name": tc["name"],
                                "success": True,
                                "result": result,
                            })
                            tc_data = {"tool_call_id": tc["id"], "tool_name": tc["name"], "result": result}
                            yield f"event: tool_call\ndata: {json.dumps(tc_data)}\n\n"
                        except Exception as exc:
                            tool_results.append({
                                "tool_call_id": tc["id"],
                                "tool_name": tc["name"],
                                "success": False,
                                "error": str(exc),
                            })
                            tc_err = {"tool_call_id": tc["id"], "tool_name": tc["name"], "error": str(exc)}
                            yield f"event: tool_call\ndata: {json.dumps(tc_err)}\n\n"

                # 10. Save assistant message to DB
                async with db_session.begin():
                    await set_rls_org(db_session, principal.organisation_id)
                    assistant_msg = ChatMessage(
                        organisation_id=principal.organisation_id,
                        session_id=session_id,
                        role="assistant",
                        content=full_content if full_content else None,
                        tool_calls_json={"tool_calls": tool_calls} if tool_calls else None,
                        parent_id=user_msg.id,
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

            # 11. Send done event
            yield f"event: done\ndata: {json.dumps({'message_id': msg_id})}\n\n"

        except HTTPException:
            raise
        except ProgrammingError as exc:
            logger.exception("Remy streaming error — missing DB table or schema")
            yield f"event: error\ndata: {json.dumps({'detail': 'Feature is not available. Run database migrations to enable it.'})}\n\n"
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
