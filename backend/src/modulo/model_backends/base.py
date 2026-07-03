from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.messages import BaseMessage


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str = ""


async def _openai_compatible_health_check(
    base_url: str,
    api_key: str | None,
) -> HealthResult:
    """Try GET {base_url}/models to verify reachability + credentials."""
    url = f"{base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.is_success:
                return HealthResult(ok=True)
            return HealthResult(ok=False, detail=response.text[:500])
    except httpx.TimeoutException:
        return HealthResult(ok=False, detail="Health check timed out")
    except Exception as exc:
        return HealthResult(ok=False, detail=str(exc)[:500])


class ModelBackendBase(ABC):
    """Abstract base for all model backends (real + stub)."""

    supports_tools: bool = False

    @abstractmethod
    async def invoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> BaseMessage:
        """Send messages and return the assistant reply."""

    @abstractmethod
    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        """Return an async iterator that yields token chunks."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable identifier for this backend (e.g. 'anthropic/claude-sonnet-4-6')."""

    async def health_check(self) -> HealthResult:
        """Verify connectivity. Default: minimal ping invoke. Override for efficiency."""
        import asyncio

        from langchain_core.messages import HumanMessage
        try:
            await asyncio.wait_for(
                self.invoke([HumanMessage(content="ping")], max_tokens=1),
                timeout=10.0,
            )
            return HealthResult(ok=True)
        except TimeoutError:
            return HealthResult(ok=False, detail="Health check timed out")
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:500])
