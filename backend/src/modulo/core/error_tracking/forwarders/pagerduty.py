from __future__ import annotations

import logging
from typing import Any

import httpx

from modulo.core.error_tracking.forwarders.base import BaseForwarder

_log = logging.getLogger(__name__)

_DEFAULT_SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "error": "error",
    "warning": "warning",
}


class PagerDutyErrorForwarder(BaseForwarder):
    """Forwards critical error events to PagerDuty Events API v2.

    By default only forwards ``critical``-level errors.  The severity
    mapping is configurable via ``severity_mapping`` in the config dict.
    """

    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool:
        routing_key = config.get("routing_key")
        severity_map = config.get("severity_mapping", _DEFAULT_SEVERITY_MAP)
        forward_levels = config.get("forward_levels", ("critical",))

        if not routing_key:
            _log.warning("pagerduty_forwarder.no_routing_key")
            return False

        if error_event.level not in forward_levels:
            _log.debug(
                "pagerduty_forwarder.skipped",
                extra={"level": error_event.level, "forward_levels": forward_levels},
            )
            return False

        severity = severity_map.get(error_event.level, "error")

        try:
            url = "https://events.pagerduty.com/v2/enqueue"
            body = {
                "routing_key": routing_key,
                "event_action": "trigger",
                "payload": {
                    "summary": error_event.message[:1024],
                    "source": str(org_id),
                    "severity": severity,
                    "component": error_event.source,
                    "group": error_group.fingerprint if error_group else "unknown",
                    "class": "error_event",
                    "custom_details": {
                        "fingerprint": error_group.fingerprint if error_group else "",
                        "count": error_group.count if error_group else 1,
                        "environment": error_event.environment or "unknown",
                        "version": error_event.version or "unknown",
                        "stacktrace": (error_event.stacktrace or "")[:2000],
                    },
                },
                "dedup_key": f"modulo:{org_id}:{error_group.fingerprint}" if error_group else f"modulo:{org_id}",
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                    },
                )
                if resp.is_success:
                    return True
                _log.warning(
                    "pagerduty_forwarder.api_error",
                    extra={"status": resp.status_code, "org_id": str(org_id)},
                )
                return False
        except Exception:
            _log.exception("pagerduty_forwarder.request_failed")
            return False
