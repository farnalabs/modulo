"""OpsGenie error forwarder."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from modulo.core.error_tracking.forwarders.base import BaseForwarder

_log = logging.getLogger(__name__)

_DEFAULT_PRIORITY_MAP: dict[str, str] = {
    "critical": "P1",
    "error": "P2",
    "warning": "P3",
}


class OpsGenieErrorForwarder(BaseForwarder):
    """Forwards error events to OpsGenie via the Alert API."""

    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool:
        api_key = config.get("api_key")
        team = config.get("team", "")
        priority_map = config.get("priority_mapping", _DEFAULT_PRIORITY_MAP)

        if not api_key:
            _log.warning("opsgenie_forwarder.no_api_key")
            return False

        level = error_event.level or "error"
        priority = priority_map.get(level, "P3")
        message = error_event.message or ""
        source = error_event.source or ""
        environment = error_event.environment or "unknown"
        version = error_event.version or "unknown"

        try:
            url = "https://api.opsgenie.com/v2/alerts"
            body = {
                "message": message[:512],
                "alias": f"modulo:{org_id}:{error_group.fingerprint}" if error_group else f"modulo:{org_id}",
                "description": self._build_description(org_id, error_group, error_event),
                "source": "Modulo Error Forwarder",
                "priority": priority,
                "tags": [
                    f"org_id:{org_id}",
                    f"source:{source}",
                    f"level:{level}",
                    f"environment:{environment}",
                ],
                "details": {
                    "fingerprint": error_group.fingerprint if error_group else "",
                    "count": (
                        str(error_group.count) if error_group is not None and error_group.count is not None else "1"
                    ),
                    "version": version,
                },
            }

            if team:
                body["responders"] = [{"type": "team", "name": team}]

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={
                        "Authorization": f"GenieKey {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.is_success:
                    return True
                _log.warning(
                    "opsgenie_forwarder.api_error",
                    extra={"status": resp.status_code, "org_id": str(org_id)},
                )
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("opsgenie_forwarder.request_failed")
            return False

    @staticmethod
    def _build_description(_org_id: Any, error_group: Any, error_event: Any) -> str:
        stacktrace = error_event.stacktrace or ""
        parts = [
            f"Group fingerprint: {error_group.fingerprint if error_group else 'unknown'}",
            f"Count: {error_group.count if error_group else 1}",
            f"Environment: {error_event.environment or 'unknown'}",
            f"Version: {error_event.version or 'unknown'}",
        ]
        if stacktrace:
            parts.append(f"\nStacktrace:\n{stacktrace[:2000]}")
        return "\n".join(parts)
