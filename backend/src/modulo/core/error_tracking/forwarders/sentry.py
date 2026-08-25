"""Sentry error forwarder."""

from __future__ import annotations

import asyncio
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
            import sentry_sdk  # type: ignore[import-not-found]  # optional integration
        except ImportError:
            return await self._forward_via_api(dsn, org_slug, project_slug, org_id, error_group, error_event)

        return await self._forward_via_sdk(sentry_sdk, dsn, org_id, error_group, error_event)

    async def _forward_via_sdk(
        self,
        sentry_sdk: Any,
        _dsn: str,
        org_id: Any,
        error_group: Any,
        error_event: Any,
    ) -> bool:
        try:
            level = _LEVEL_MAP.get(error_event.level, "error")
            message = error_event.message or ""
            source = error_event.source or ""
            environment = error_event.environment or "unknown"
            version = error_event.version or "unknown"
            fingerprint = [error_group.fingerprint] if error_group and error_group.fingerprint else []
            org_id_str = str(org_id)
            await asyncio.to_thread(
                self._send_via_sdk_sync,
                sentry_sdk,
                level,
                message,
                source,
                environment,
                version,
                fingerprint,
                org_id_str,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("sentry_forwarder.sdk_failed")
            return False

    @staticmethod
    def _send_via_sdk_sync(
        sentry_sdk: Any,
        level: str,
        message: str,
        source: str,
        environment: str,
        version: str,
        fingerprint: list[str],
        org_id_str: str,
    ) -> None:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("org_id", org_id_str)
            scope.set_tag("source", source)
            scope.set_tag("environment", environment)
            scope.set_tag("version", version)
            scope.set_level(level)
            if fingerprint:
                scope.fingerprint = fingerprint
            sentry_sdk.capture_message(message, level=level)

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
            parsed = urlparse(dsn)
            host = parsed.hostname or "sentry.io"
            url = f"https://{host}/api/0/projects/{org_slug}/{project_slug}/events/"
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
                    extra={"status": resp.status_code, "org_id": str(org_id)},
                )
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("sentry_forwarder.api_request_failed")
            return False
