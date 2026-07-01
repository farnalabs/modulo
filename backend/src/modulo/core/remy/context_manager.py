from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.remy_message import ChatMessage

logger = logging.getLogger(__name__)

try:
    import tiktoken

    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False


class PruneResult(BaseModel):
    kept_messages: list[dict[str, Any]]
    pruned_count: int
    summary: str | None = None


class ConversationContext(BaseModel):
    messages: list[dict[str, Any]]
    total_tokens: int
    original_token_count: int
    pruned_count: int
    has_summary: bool


_ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


class ContextManager:
    """Manages conversation reconstruction within the model's context window.

    Stateless utility — no DB writes, only reads.
    Called by the stream endpoint before invoking the LLM backend.
    """

    @staticmethod
    def count_tokens(text: str, provider: str = "anthropic") -> int:
        if not text:
            return 0

        if HAS_TIKTOKEN:
            try:
                encoding = tiktoken.get_encoding("cl100k_base")
                return len(encoding.encode(text))
            except Exception:
                logger.debug("tiktoken encoding failed, falling back to heuristic")

        return max(1, len(text) // 4)

    @staticmethod
    def _message_to_dict(m: ChatMessage) -> dict[str, Any]:
        """Convert a ChatMessage ORM row to an OpenAI/Anthropic message dict."""
        msg: dict[str, Any] = {"role": m.role, "content": m.content or ""}
        if m.tool_calls_json:
            tc = m.tool_calls_json.get("tool_calls", [])
            if tc:
                msg["tool_calls"] = tc
        if m.tool_results_json:
            msg["tool_result"] = m.tool_results_json
        return msg

    @staticmethod
    def _build_summary_prompt(pruned_messages: list[ChatMessage]) -> str:
        """Build a prompt that asks the LLM to summarise pruned conversation."""
        lines: list[str] = [
            "Summarise the following conversation in a single paragraph. "
            "Capture the key topics, decisions, and unresolved questions. "
            "Write in the same voice and style as the original messages.",
            "",
            "---",
        ]
        for m in pruned_messages:
            role_label = m.role.replace("_", " ").title()
            lines.append(f"[{role_label}]: {m.content or ''}")
        lines.append("---")
        lines.append("Summary:")
        return "\n".join(lines)

    @staticmethod
    def prune_messages(
        messages: list[dict[str, Any]],
        budget: int,
    ) -> PruneResult:
        """Remove oldest messages until remaining fit within ``budget`` tokens.

        The first message (system prompt) and last message (newest user
        message) are always preserved.
        """
        if not messages:
            return PruneResult(kept_messages=[], pruned_count=0)

        kept = list(messages)
        pruned_count = 0

        while len(kept) > 2:
            tokens = sum(
                ContextManager.count_tokens(m.get("content", "") or "")
                for m in kept
            )
            if tokens <= budget:
                break
            kept.pop(1)
            pruned_count += 1

        return PruneResult(
            kept_messages=kept,
            pruned_count=pruned_count,
        )

    async def generate_summary(
        self,
        pruned_messages: list[ChatMessage],
        provider: str,
        model: str,
        api_key: str,
    ) -> str | None:
        """Call the LLM to generate a one-turn summary of pruned conversation.

        Returns ``None`` if the provider is unsupported or the call fails.
        """
        prompt = self._build_summary_prompt(pruned_messages)

        try:
            if provider == "anthropic":
                return await self._call_anthropic(prompt, model, api_key)
            if provider in ("openai", "deepseek", "groq", "fireworks", "together"):
                return await self._call_openai_compat(prompt, model, api_key, provider)
            logger.warning("Summary generation not supported for provider %r", provider)
            return None
        except Exception:
            logger.exception("Failed to generate conversation summary")
            return None

    async def _call_anthropic(self, prompt: str, model: str, api_key: str) -> str:
        """Call the Anthropic Messages API for summarization."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _ANTHROPIC_API,
                json={
                    "model": model,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]

    async def _call_openai_compat(
        self,
        prompt: str,
        model: str,
        api_key: str,
        provider: str,
    ) -> str:
        """Call an OpenAI-compatible chat completions API for summarization."""
        base_urls = {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "fireworks": "https://api.fireworks.ai/inference/v1",
            "together": "https://api.together.xyz/v1",
        }
        base = base_urls.get(provider, "https://api.openai.com/v1")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                },
                headers={
                    "authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def reconstruct(
        self,
        session_id: uuid.UUID,
        new_message: str,
        system_prompt: str,
        page_context: str | None,
        context_window_tokens: int,
        session: AsyncSession,
        provider: str = "anthropic",
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
    ) -> ConversationContext:
        """Reconstruct conversation within the model's context window.

        1. Loads all messages for the session from DB (ordered by created_at asc)
        2. Counts tokens of each message
        3. Calculates available budget = ``context_window_tokens`` - 20 % safety margin
        4. If the total fits within budget: returns all messages
        5. If not: prunes oldest messages until it fits
        6. If pruning removes >50 % of messages: generates a summary of pruned content
        7. Returns a ``ConversationContext`` with messages, summary, and metadata
        """
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
        db_messages = list(result.scalars().all())

        system_content = system_prompt
        if page_context:
            system_content = f"{system_content}\n\n{page_context}"
        system_message: dict[str, Any] = {"role": "system", "content": system_content}

        conversation_messages = [self._message_to_dict(m) for m in db_messages]

        user_message: dict[str, Any] = {"role": "user", "content": new_message}

        all_messages = [system_message, *conversation_messages, user_message]
        original_token_count = sum(
            self.count_tokens(m.get("content", "") or "", provider)
            for m in all_messages
        )

        budget = int(context_window_tokens * 0.8)

        total_tokens = original_token_count
        pruned_count = 0

        if total_tokens <= budget:
            return ConversationContext(
                messages=all_messages,
                total_tokens=total_tokens,
                original_token_count=original_token_count,
                pruned_count=0,
                has_summary=False,
            )

        pruned_db_messages: list[ChatMessage] = []
        while total_tokens > budget and conversation_messages:
            removed = conversation_messages.pop(0)
            removed_db = db_messages.pop(0)
            removed_tokens = self.count_tokens(
                removed.get("content", "") or "", provider
            )
            total_tokens -= removed_tokens
            pruned_count += 1
            pruned_db_messages.append(removed_db)
            all_messages = [system_message, *conversation_messages, user_message]

        has_summary = False
        if pruned_count > 0 and len(db_messages) > 0:
            pruned_ratio = pruned_count / (pruned_count + len(db_messages))
            if pruned_ratio > 0.5 and api_key:
                summary_text = await self.generate_summary(
                    pruned_db_messages, provider, model, api_key
                )
                has_summary = summary_text is not None

        return ConversationContext(
            messages=all_messages,
            total_tokens=total_tokens,
            original_token_count=original_token_count,
            pruned_count=pruned_count,
            has_summary=has_summary,
        )
