import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

HEALTH_CHECK_TIMEOUT = 10.0
HEALTH_DETAIL_MAX_LENGTH = 500


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
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
            if response.is_success:
                return HealthResult(ok=True)
            return HealthResult(ok=False, detail=response.text[:HEALTH_DETAIL_MAX_LENGTH])
    except httpx.TimeoutException:
        logger.warning("Health check timed out for %s", url)
        return HealthResult(ok=False, detail="Health check timed out")
    except Exception as exc:
        logger.warning("Health check failed for %s: %s", url, exc)
        return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])


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
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            return HealthResult(ok=True)
        except TimeoutError:
            logger.warning("Health check timed out for %s", type(self).__name__)
            return HealthResult(ok=False, detail="Health check timed out")
        except Exception as exc:
            logger.warning("Health check failed for %s: %s", type(self).__name__, exc)
            return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])
