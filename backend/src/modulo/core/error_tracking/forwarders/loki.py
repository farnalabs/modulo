from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from modulo.core.error_tracking.forwarders.base import BaseForwarder

_log = logging.getLogger(__name__)


class LokiErrorForwarder(BaseForwarder):
    """Forwards error events to Grafana Loki via the push API.

    Config:
        push_url (str): Loki HTTP push endpoint (e.g. ``https://loki.example.com/loki/api/v1/push``).
        tenant_id (str, optional): ``X-Scope-OrgID`` header value.
        labels (dict, optional): Additional labels to include (defaults to ``{"app": "modulo"}``).
    """

    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool:
        push_url = config.get("push_url")
        tenant_id = config.get("tenant_id", "")
        labels = config.get("labels", {"app": "modulo"})

        if not push_url:
            _log.warning("loki_forwarder.no_push_url")
            return False

        try:
            stream_labels = {
                **labels,
                "org_id": str(org_id),
                "source": error_event.source,
                "level": error_event.level,
                "environment": error_event.environment or "unknown",
                "fingerprint": error_group.fingerprint if error_group else "",
            }
            stream_labels = {k: str(v) for k, v in stream_labels.items()}

            log_entry = json.dumps({
                "message": error_event.message,
                "fingerprint": error_group.fingerprint if error_group else "",
                "count": error_group.count if error_group else 1,
                "version": error_event.version or "unknown",
                "stacktrace": (error_event.stacktrace or "")[:5000],
            })

            body = {
                "streams": [
                    {
                        "stream": stream_labels,
                        "values": [
                            [str(int(time.time() * 1e9)), log_entry],
                        ],
                    },
                ],
            }

            headers = {
                "Content-Type": "application/json",
            }
            if tenant_id:
                headers["X-Scope-OrgID"] = tenant_id

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    push_url,
                    json=body,
                    headers=headers,
                )
                if resp.is_success:
                    return True
                _log.warning(
                    "loki_forwarder.api_error",
                    extra={"status": resp.status_code, "org_id": str(org_id)},
                )
                return False
        except Exception:
            _log.exception("loki_forwarder.request_failed")
            return False
