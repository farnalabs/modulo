"""Scheduled-report registry + fire + delivery (SAQ path).

Report generators are registered via ``register_report_type()``. The per-item
fire job (``fire_report_trigger`` in :mod:`modulo.core.cron_helpers`, enqueued
by ``fire_due_triggers``) generates, formats, and delivers each due report as a
bounded SAQ job. The Celery beat scheduler (``DatabaseReportScheduler`` /
``ReportFireTask``) was removed in PR C of the Celery->SAQ migration.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.scheduled_report import ScheduledReport
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_ENGINE: AsyncEngine | None = None
_ENGINE_LOCK: threading.Lock = threading.Lock()

_TEST_ENGINE: AsyncEngine | None = None

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ReportData = dict[str, Any]
"""Returned by a report generator."""

ReportGenerator = Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[ReportData]]
"""(session, org_id, config) -> report_data"""

ReportFormatter = Callable[[ReportData], Any]
"""Takes report_data, returns a deliverable payload."""

ReportDeliverer = Callable[[Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
"""(payload, recipient_config) -> list[delivery_result]"""

# ---------------------------------------------------------------------------
# Report-type registry
# ---------------------------------------------------------------------------

_generators: dict[str, ReportGenerator] = {}
_formatters: dict[str, ReportFormatter] = {}
_deliverers: dict[str, ReportDeliverer] = {}


def register_report_type(
    report_type: str,
    generator: ReportGenerator,
    formatter: ReportFormatter | None = None,
    deliverer: ReportDeliverer | None = None,
) -> None:
    """Register a report generator (and optional formatter/deliverer) by type."""
    _generators[report_type] = generator
    if formatter is not None:
        _formatters[report_type] = formatter
    if deliverer is not None:
        _deliverers[report_type] = deliverer


def get_generator(report_type: str) -> ReportGenerator | None:
    return _generators.get(report_type)


def get_formatter(report_type: str) -> ReportFormatter | None:
    return _formatters.get(report_type)


def get_deliverer(report_type: str) -> ReportDeliverer | None:
    return _deliverers.get(report_type)


# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------


def _get_engine() -> AsyncEngine:
    if _TEST_ENGINE is not None:
        return _TEST_ENGINE
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                settings = get_settings()
                connect_args: dict[str, Any] = {"timeout": 10}
                if settings.modulo_db.lower() == "postgres":
                    connect_args["ssl"] = False
                    connect_args["statement_cache_size"] = 0
                _ENGINE = create_async_engine(
                    settings.database_url,
                    pool_pre_ping=True,
                    connect_args=connect_args,
                    pool_recycle=3600,
                    pool_timeout=30,
                )
    return _ENGINE


def _set_test_engine(engine: AsyncEngine | None) -> None:
    """Override the engine for testing. Pass None to reset."""
    global _TEST_ENGINE
    _TEST_ENGINE = engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_next_send(cron_expression: str, after: datetime.datetime | None = None) -> datetime.datetime:
    """Compute the next send time for a cron expression.

    If *after* is None, uses the current UTC time.
    """
    base = after or datetime.datetime.now(datetime.UTC)
    cron = croniter(cron_expression, base)
    next_dt = cron.get_next(datetime.datetime)
    if not isinstance(next_dt, datetime.datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    return next_dt


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    from modulo.db.rls import set_rls_execution_context
    from modulo.db.rls import set_rls_org as _set_rls

    await _set_rls(session, org_id)
    # Scheduled-report generation is background machinery (no user principal) —
    # the execution hatch lets it read team-scoped tables org-wide.
    await set_rls_execution_context(session)


# ---------------------------------------------------------------------------
# Fire logic — shared async core (called by the SAQ fire_report_trigger job)
# ---------------------------------------------------------------------------


async def _fire_scheduled_report(
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Core fire logic — runs inside asyncio.run() (the Celery task was removed in PR C)."""
    engine = _get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)

    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)

        now = datetime.datetime.now(datetime.UTC)
        result = await session.execute(
            select(ScheduledReport)
            .where(
                ScheduledReport.id == report_id,
                ScheduledReport.organisation_id == org_id,
                ScheduledReport.next_send_at <= now,
            )
            .with_for_update()
        )
        report = result.scalar_one_or_none()
        if report is None or not report.active:
            return {"status": "skipped", "reason": "report_inactive_or_missing"}

        generator = get_generator(report.report_type)
        if generator is None:
            _log.warning("No generator registered for report type %s", report.report_type)
            return {"status": "failed", "reason": f"no_generator_for_{report.report_type}"}

        try:
            config = report.config_json or {}
            report_data = await generator(session, org_id, config)

            formatter = get_formatter(report.report_type)
            payload: Any = report_data
            if formatter is not None:
                payload = formatter(report_data)

            deliverer = get_deliverer(report.report_type)
            recipient_config = report.recipient_config or {}
            delivery_results: list[dict[str, Any]] = []
            if deliverer is not None:
                delivery_results = await deliverer(payload, recipient_config)
            else:
                delivery_results = await _deliver_via_config(payload, recipient_config)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Report %s (%s) generation or delivery failed", report_id, report.report_type)
            return {"status": "failed", "reason": "generation_or_delivery_failed"}

        schedule_type = config.get("schedule_type")
        if schedule_type == "one_time":
            next_send = None
        else:
            try:
                next_send = compute_next_send(report.cron_expression, after=now)
            except (ValueError, TypeError, KeyError) as exc:
                _log.error(
                    "Invalid cron expression '%s' for report %s: %s",
                    report.cron_expression,
                    report_id,
                    exc,
                    exc_info=True,
                )
                await session.execute(
                    update(ScheduledReport).where(ScheduledReport.id == report_id).values(active=False)
                )
                return {"status": "failed", "reason": f"invalid_cron: {exc}"}

        await session.execute(
            update(ScheduledReport)
            .where(ScheduledReport.id == report_id)
            .values(
                last_sent_at=now,
                next_send_at=next_send,
                active=schedule_type != "one_time",
            )
        )

        _log.info(
            "Report %s (%s) sent. Next send: %s",
            report_id,
            report.report_type,
            next_send.isoformat() if next_send is not None else "none (one-time report completed)",
        )

        return {
            "status": "sent",
            "report_id": str(report_id),
            "report_type": report.report_type,
            "next_send_at": next_send.isoformat() if next_send is not None else None,
            "delivery_results": delivery_results,
        }


# ---------------------------------------------------------------------------
# Registry + delivery (shared — imported by cron_helpers fire jobs)
# ---------------------------------------------------------------------------


async def _deliver_via_config(
    payload: Any,
    recipient_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deliver a report payload based on recipient_config type."""
    config_type = recipient_config.get("type", "webhook")

    if config_type == "slack_webhook":
        urls = recipient_config.get("webhook_urls", [])
        return await _deliver_slack_webhook(
            payload,
            urls,
            signing_secret=recipient_config.get("signing_secret"),
            request_timeout=recipient_config.get("timeout"),
        )

    _log.warning("Unknown recipient config type '%s', falling back to generic webhook", config_type)
    return await _deliver_webhook(payload, recipient_config)


_REPORT_HTTP_TIMEOUT = 30.0
_REPORT_MAX_RETRIES = 3
_REPORT_BACKOFF_BASE = 2.0
_REPORT_MAX_BACKOFF = 30.0

_SIGNATURE_HEADER = "X-Modulo-Signature"


def _webhook_url_error(url: object) -> str | None:
    """Pre-flight validate a webhook recipient URL.

    Returns a short machine-readable reason string when *url* can never be
    delivered (permanent config error, not a transient failure), or ``None``
    when it is safe to attempt delivery.

    Validation contract — the URL must:

    * be a non-empty string (whitespace-only rejected)
    * carry no whitespace anywhere in the value
    * parse via ``urlsplit`` (broken IPv6 like ``http://[::1`` rejected)
    * use the ``http`` or ``https`` scheme (``ftp://``, ``file://``, etc. rejected)
    * carry a non-empty hostname (bare ``https://`` rejected)
    * carry a valid port when one is present (``https://host:abc`` and
      ``https://host:99999`` are malformed — ``urlsplit`` accepts them but
      httpx raises ``InvalidURL`` on ``post``, which would otherwise fall into
      the retry/backoff loop)
    * not embed userinfo credentials (``https://user:pass@host`` — httpx would
      send them as a Basic-Auth header and they would leak into audit/delivery
      logs)

    Reasons are stable tokens (``url_not_a_string``, ``url_empty``,
    ``url_contains_whitespace``, ``url_malformed``, ``url_scheme_not_http``,
    ``url_missing_host``, ``url_contains_credentials``) so callers and tests can
    assert on them.
    """
    if not isinstance(url, str):
        return "url_not_a_string"
    candidate = url.strip()
    if not candidate:
        return "url_empty"
    if any(ch.isspace() for ch in candidate):
        return "url_contains_whitespace"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "url_malformed"
    if parts.scheme not in ("http", "https"):
        return "url_scheme_not_http"
    if not parts.hostname:
        return "url_missing_host"
    if parts.username is not None or parts.password is not None:
        return "url_contains_credentials"
    try:
        _ = parts.port
    except ValueError:
        return "url_malformed"
    return None


def _redact_url_credentials(url: str) -> str:
    """Strip embedded ``user:pass@`` credentials from *url* for storage/logging.

    ``https://user:pass@host/x`` becomes ``https://host/x`` so the credentialed
    URL rejected by ``_webhook_url_error`` does not leak its credentials into
    delivery logs, per-URL results, or the SAQ job result. URLs without
    userinfo are returned unchanged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _serialize_json_body(body: dict[str, Any] | list[Any]) -> bytes:
    """Serialize a payload to the exact bytes that will be sent on the wire.

    Compact separators are used so the signed bytes match what the recipient
    receives byte-for-byte.
    """
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign_payload(secret: str, body_bytes: bytes) -> str:
    """Compute the HMAC-SHA256 signature of a serialized payload.

    Returns ``sha256=<hex digest>`` so the recipient can verify authenticity
    against a shared secret. This is a plain HMAC-SHA256 over the raw body
    bytes (not Slack's ``v0:timestamp:body`` signing scheme).
    """
    digest = hmac.new(str(secret).encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _coerce_timeout(value: object) -> float | None:
    """Validate a caller-supplied timeout, falling back to ``None`` (default)."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


async def _deliver_to_urls(
    urls: list[str],
    body: dict[str, Any] | list[Any],
    headers: dict[str, str] | None = None,
    *,
    request_timeout: float | None = None,
    signing_secret: str | None = None,
) -> list[dict[str, Any]]:
    """POST JSON body to each URL, returning per-URL results.

    Retries transient failures (5xx, connection errors, 429) per URL
    with exponential backoff. Non-transient 4xx errors are not retried.

    When ``signing_secret`` is provided, the body is serialized once, signed
    with HMAC-SHA256, and sent byte-for-byte with an ``X-Modulo-Signature``
    header so the recipient can verify authenticity. ``request_timeout``
    overrides the per-request timeout (default ``_REPORT_HTTP_TIMEOUT``).
    """
    if signing_secret:
        body_bytes = _serialize_json_body(body)
        merged_headers = {**(headers or {}), _SIGNATURE_HEADER: _sign_payload(signing_secret, body_bytes)}
        merged_headers.setdefault("Content-Type", "application/json")
    else:
        body_bytes = None
        merged_headers = headers or {}
    effective_timeout = _coerce_timeout(request_timeout) if request_timeout is not None else None
    request_timeout_seconds = effective_timeout if effective_timeout is not None else _REPORT_HTTP_TIMEOUT

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
        for url in urls:
            display_url = _redact_url_credentials(url)
            result: dict[str, Any] = {"url": display_url, "status": "failed", "status_code": None, "error": None}
            invalid_reason = _webhook_url_error(url)
            if invalid_reason is not None:
                # Permanent config error — skip the retry/backoff loop entirely;
                # a malformed URL cannot become deliverable on a later attempt.
                _log.warning(
                    "Delivery to %s skipped: invalid webhook URL (%s)",
                    display_url,
                    invalid_reason,
                )
                result.update(
                    {
                        "status": "failed",
                        "status_code": None,
                        "error": f"invalid_webhook_url: {invalid_reason}",
                    }
                )
                results.append(result)
                continue
            last_resp_or_exc: httpx.Response | Exception | None = None
            for attempt in range(_REPORT_MAX_RETRIES):
                try:
                    if body_bytes is not None:
                        resp = await client.post(url, content=body_bytes, headers=merged_headers)
                    else:
                        resp = await client.post(url, json=body, headers=merged_headers)
                    last_resp_or_exc = resp
                    if resp.is_success:
                        result.update({"status": "delivered", "status_code": resp.status_code, "error": None})
                        break
                    if resp.status_code == 429:
                        retry_after = _parse_retry_after(resp)
                        _log.warning(
                            "Delivery to %s rate-limited (429), retrying after %.0fs (attempt %d/%d)",
                            url,
                            retry_after,
                            attempt + 1,
                            _REPORT_MAX_RETRIES,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status_code >= 500:
                        _log.warning(
                            "Delivery to %s returned %d, retrying (attempt %d/%d)",
                            url,
                            resp.status_code,
                            attempt + 1,
                            _REPORT_MAX_RETRIES,
                        )
                        delay = min(_REPORT_BACKOFF_BASE**attempt, _REPORT_MAX_BACKOFF)
                        await asyncio.sleep(delay)
                        continue
                    result.update(
                        {
                            "status": "failed",
                            "status_code": resp.status_code,
                            "error": resp.text[:200],
                        }
                    )
                    break
                except (httpx.RequestError, TypeError) as exc:
                    _log.warning(
                        "Delivery to %s failed: %s (attempt %d/%d)",
                        url,
                        exc,
                        attempt + 1,
                        _REPORT_MAX_RETRIES,
                        exc_info=True,
                    )
                    last_resp_or_exc = exc
                    if attempt < _REPORT_MAX_RETRIES - 1:
                        delay = min(_REPORT_BACKOFF_BASE**attempt, _REPORT_MAX_BACKOFF)
                        await asyncio.sleep(delay)
            else:
                if isinstance(last_resp_or_exc, Exception):
                    result.update({"status": "failed", "status_code": None, "error": str(last_resp_or_exc)})
                elif last_resp_or_exc is not None:
                    err_text = getattr(last_resp_or_exc, "text", None) or "max_retries_exceeded"
                    result.update(
                        {
                            "status": "failed",
                            "status_code": last_resp_or_exc.status_code,
                            "error": err_text[:200],
                        }
                    )
                else:
                    result.update({"status": "failed", "error": "max_retries_exceeded"})
            results.append(result)
    return results


def _parse_retry_after(resp: httpx.Response) -> float:
    """Extract Retry-After header value, defaulting to 5 seconds."""
    raw = resp.headers.get("Retry-After", "5")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 5.0


async def _deliver_slack_webhook(
    payload: Any,
    webhook_urls: list[str],
    *,
    signing_secret: str | None = None,
    request_timeout: float | None = None,
) -> list[dict[str, Any]]:
    body = payload if isinstance(payload, (dict, list)) else {"text": str(payload)}
    return await _deliver_to_urls(webhook_urls, body, signing_secret=signing_secret, request_timeout=request_timeout)


async def _deliver_webhook(
    payload: Any,
    recipient_config: dict[str, Any],
) -> list[dict[str, Any]]:
    urls = recipient_config.get("urls", [])
    headers = recipient_config.get("headers", {})
    signing_secret = recipient_config.get("signing_secret")
    request_timeout = recipient_config.get("timeout")
    body = payload if isinstance(payload, (dict, list)) else {"data": str(payload)}
    return await _deliver_to_urls(
        urls,
        body,
        headers=headers,
        signing_secret=signing_secret,
        request_timeout=request_timeout,
    )
