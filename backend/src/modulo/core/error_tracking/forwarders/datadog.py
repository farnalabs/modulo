from __future__ import annotations

import logging
from typing import Any

import httpx

from modulo.core.error_tracking.forwarders.base import BaseForwarder

_log = logging.getLogger(__name__)


class DatadogErrorForwarder(BaseForwarder):
    """Forwards error events to DataDog Events API."""

    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool:
        api_key = config.get("api_key")
        site = config.get("site", "datadoghq.com")

        if not api_key:
            _log.warning("datadog_forwarder.no_api_key")
            return False

        try:
            url = f"https://api.{site}/api/v1/events"
            message = error_event.message or ""
            level = error_event.level or "error"
            source = error_event.source or ""
            environment = error_event.environment or "unknown"
            body = {
                "title": f"[{level.upper()}] {message[:200]}",
                "text": self._build_text(org_id, error_group, error_event),
                "tags": [
                    "modulo",
                    f"org_id:{org_id}",
                    f"source:{source}",
                    f"level:{level}",
                    f"environment:{environment}",
                ],
                "alert_type": "error" if level in ("error", "critical") else "warning",
                "priority": "normal",
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={
                        "DD-API-KEY": api_key,
                        "Content-Type": "application/json",
                    },
                )
                if resp.is_success:
                    return True
                _log.warning(
                    "datadog_forwarder.api_error",
                    extra={"status": resp.status_code, "org_id": str(org_id)},
                )
                return False
        except Exception:
            _log.exception("datadog_forwarder.request_failed")
            return False

    @staticmethod
    def _build_text(_org_id: Any, error_group: Any, error_event: Any) -> str:
        stacktrace = error_event.stacktrace or ""
        parts = [
            f"Group: {error_group.fingerprint if error_group else 'unknown'}",
            f"Count: {error_group.count if error_group else 1}",
            f"Environment: {error_event.environment or 'unknown'}",
            f"Version: {error_event.version or 'unknown'}",
        ]
        if stacktrace:
            parts.append(f"\nStacktrace:\n{stacktrace[:1000]}")
        return "\n".join(parts)
