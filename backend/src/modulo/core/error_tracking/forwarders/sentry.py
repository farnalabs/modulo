from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from modulo.core.error_tracking.forwarders.base import BaseForwarder

_log = logging.getLogger(__name__)

_LEVEL_MAP: dict[str, str] = {
    "critical": "fatal",
    "error": "error",
    "warning": "warning",
}


def _parse_dsn_token(dsn: str) -> str | None:
    """Extract the auth token from a Sentry DSN.

    Sentry DSN format: ``https://<public_key>@<host>/<project_id>``
    The public key portion before ``@`` is used as a Bearer token
    for the Sentry API fallback path.
    """
    try:
        parsed = urlparse(dsn)
        if parsed.username:
            return parsed.username
    except Exception:
        _log.debug("sentry_forwarder.dsn_parse_failed")
    return None


class SentryErrorForwarder(BaseForwarder):
    """Forwards error events to a Sentry project via the Sentry API."""

    async def forward(
        self,
        org_id: Any,
        error_group: Any,
        error_event: Any,
        config: dict[str, Any],
    ) -> bool:
        dsn = config.get("dsn")
        org_slug = config.get("org_slug", "")
        project_slug = config.get("project_slug", "")

        if not dsn:
            _log.warning("sentry_forwarder.no_dsn")
            return False

        try:
            sentry_sdk = __import__("sentry_sdk")
        except ImportError:
            return await self._forward_via_api(dsn, org_slug, project_slug, org_id, error_group, error_event)

        return await self._forward_via_sdk(sentry_sdk, dsn, org_id, error_group, error_event)

    async def _forward_via_sdk(
        self,
        sentry_sdk: Any,
        _dsn: str,
        org_id: Any,
        _error_group: Any,
        error_event: Any,
    ) -> bool:
        try:
            level = _LEVEL_MAP.get(error_event.level, "error")
            message = error_event.message or ""
            source = error_event.source or ""
            environment = error_event.environment or "unknown"
            version = error_event.version or "unknown"
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("org_id", str(org_id))
                scope.set_tag("source", source)
                scope.set_tag("environment", environment)
                scope.set_tag("version", version)
                scope.set_level(level)
                sentry_sdk.capture_message(message, level=level)
            return True
        except Exception:
            _log.exception("sentry_forwarder.sdk_failed")
            return False

    async def _forward_via_api(
        self,
        dsn: str,
        org_slug: str,
        project_slug: str,
        org_id: Any,
        error_group: Any,
        error_event: Any,
    ) -> bool:
        try:
            url = f"https://sentry.io/api/0/projects/{org_slug}/{project_slug}/events/"
            level = _LEVEL_MAP.get(error_event.level, "error")
            auth_token = _parse_dsn_token(dsn)

            message = error_event.message or ""
            stacktrace = error_event.stacktrace or ""
            source = error_event.source or ""
            environment = error_event.environment or "unknown"
            version = error_event.version or "unknown"

            body = {
                "message": message,
                "level": level,
                "tags": {
                    "org_id": str(org_id),
                    "source": source,
                    "environment": environment,
                    "version": version,
                },
                "stacktrace": stacktrace,
                "fingerprint": [error_group.fingerprint] if error_group and error_group.fingerprint else [],
            }

            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Modulo-Error-Forwarder/1.0",
            }
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.is_success:
                    return True
                _log.warning(
                    "sentry_forwarder.api_error",
                    extra={"status": resp.status_code, "org_id": str(org_id), "body": resp.text[:500]},
                )
                return False
        except Exception:
            _log.exception("sentry_forwarder.api_request_failed")
            return False
