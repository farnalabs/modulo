"""Abstract base for error forwarders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseForwarder(ABC):
    """Abstract base for all error forwarders.

    Subclasses implement ``forward()`` which sends an error event to an
    external service (Sentry, DataDog, PagerDuty, etc.).  The method must
    never raise — return ``True`` on success, ``False`` on failure.
    """

    @abstractmethod
    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool: ...
