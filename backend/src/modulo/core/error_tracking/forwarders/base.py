from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

_log = logging.getLogger(__name__)


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
    ) -> bool:
        ...

    def _safe_call(self, coro: Any) -> bool:
        try:
            return coro
        except Exception:
            _log.exception("forwarder.forward_failed")
            return False
