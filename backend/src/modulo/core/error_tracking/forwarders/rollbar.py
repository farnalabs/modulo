from __future__ import annotations

import logging
from typing import Any

import httpx

from modulo.core.error_tracking.forwarders.base import BaseForwarder

_log = logging.getLogger(__name__)

_LEVEL_MAP: dict[str, str] = {
    "critical": "critical",
    "error": "error",
    "warning": "warning",
}


class RollbarErrorForwarder(BaseForwarder):
    """Forwards error events to Rollbar via the Item API."""

    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool:
        access_token = config.get("access_token")
        environment = config.get("environment", "production")

        if not access_token:
            _log.warning("rollbar_forwarder.no_access_token")
            return False

        try:
            url = "https://api.rollbar.com/api/1/item/"
            level = _LEVEL_MAP.get(error_event.level, "error")
            message = error_event.message or ""
            source = error_event.source or ""
            code_version = error_event.version or "unknown"

            body = {
                "access_token": access_token,
                "data": {
                    "environment": environment,
                    "level": level,
                    "body": {
                        "message": {
                            "body": message[:4096],
                        },
                    },
                    "fingerprint": error_group.fingerprint if error_group else "",
                    "code_version": code_version,
                    "request": {
                        "url": "",
                        "method": "",
                    },
                    "custom": {
                        "org_id": str(org_id),
                        "source": source,
                        "fingerprint": error_group.fingerprint if error_group else "",
                        "count": error_group.count if error_group else 1,
                    },
                },
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Modulo-Error-Forwarder/1.0",
                    },
                )
                if resp.is_success:
                    return True
                _log.warning(
                    "rollbar_forwarder.api_error",
                    extra={"status": resp.status_code, "org_id": str(org_id)},
                )
                return False
        except Exception:
            _log.exception("rollbar_forwarder.request_failed")
            return False
