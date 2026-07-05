"""Notifier — dispatch webhook notifications with HMAC signing, retry, and dead-letter tracking.

Event types dispatched:
  - hitl_awaiting     (run_id, gate_id, pipeline_name, threshold)
  - run_failed        (run_id, error_code, pipeline_name)
  - claim_expired     (run_id, gate_id, claimed_by)
  - hitl_overdue      (run_id, gate_id, minutes_overdue)

For each event, the notifier:
  1. Queries all active NotificationEndpoints subscribed to the event type.
  2. Builds an HMAC-SHA256 signature over the JSON payload.
  3. POSTs to the endpoint URL with ``X-Modulo-Signature`` header.
  4. Records delivery outcome in ``notification_delivery_log``.
  5. On HTTP failure: retries up to 3 times with exponential backoff.
  6. On final failure: marks dead_lettered, increments endpoint's dead-letter counter.
  7. On success: resets endpoint's consecutive-dead-letter counter to 0.
  8. Auto-disables endpoint after ``MAX_DEAD_LETTERS`` consecutive failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.notification_endpoint import NotificationEndpoint

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_DEAD_LETTERS",
    "RETRY_DELAYS",
    "DispatchResult",
    "Notifier",
]

_log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4  # 1 initial + 3 retries
MAX_DEAD_LETTERS = 10
RETRY_DELAYS = [5.0, 30.0, 120.0]


@dataclass
class DispatchResult:
    endpoint_id: uuid.UUID
    status: str
    attempt_count: int
    response_code: int | None = None
    last_error: str | None = None


class Notifier:
    """Dispatch notifications to configured endpoints with retry and dead-letter.

    When ``use_celery`` is True, ``dispatch_event()`` enqueues the work as a
    Celery task instead of running inline. Falls back to inline dispatch if
    the Celery broker is unreachable.
    """

    def __init__(self, db_engine: AsyncEngine, fernet_key: str, *, use_celery: bool = False) -> None:
        self._engine = db_engine
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        self._fernet = Fernet(fernet_key.encode())
        self._http_client: httpx.AsyncClient | None = None
        self._use_celery = use_celery

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=25.0, pool=30.0))
        return self._http_client

    async def dispatch_event(
        self,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        run_id: uuid.UUID | None = None,
        retain_payload: bool = False,
        team_id: uuid.UUID | None = None,
    ) -> list[DispatchResult]:
        """Dispatch a notification event to subscribed endpoints.

        When ``use_celery`` is True, enqueues a Celery task and returns
        a placeholder result immediately. Falls back to inline dispatch if
        the Celery broker is unreachable.

        When ``team_id`` is provided, dispatches to team-specific endpoints
        first, falling back to org-wide (team_id IS NULL) endpoints if no
        team-specific endpoints are configured for the event type.

        When ``team_id`` is None, dispatches only to org-wide endpoints.

        Returns a list of DispatchResult, one per endpoint.
        """
        try:
            if self._use_celery:
                return await self._dispatch_via_celery(
                    org_id, event_type, payload, run_id=run_id, retain_payload=retain_payload, team_id=team_id
                )

            return await self._dispatch_inline(org_id, event_type, payload, run_id, retain_payload, team_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("notifier.dispatch_failed", extra={"event_type": event_type, "org_id": str(org_id)})
            return []

    async def _dispatch_via_celery(
        self,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        run_id: uuid.UUID | None = None,
        retain_payload: bool = False,
        team_id: uuid.UUID | None = None,
    ) -> list[DispatchResult]:
        """Enqueue dispatch to Celery; fall back to inline on failure."""
        try:
            from modulo.core.notifier.celery_tasks import enqueue_dispatch

            results = await enqueue_dispatch(
                org_id,
                event_type,
                payload,
                run_id=run_id,
                retain_payload=retain_payload,
                team_id=team_id,
            )
            return [
                DispatchResult(
                    endpoint_id=uuid.UUID(r["endpoint_id"]) if r["endpoint_id"] != "celery-enqueued" else uuid.uuid4(),
                    status=r["status"],
                    attempt_count=r["attempt_count"],
                    response_code=r["response_code"],
                    last_error=r["last_error"],
                )
                for r in results
            ]
        except Exception as exc:
            _log.warning(
                "notifier.celery_fallback",
                extra={"event_type": event_type, "org_id": str(org_id), "error": str(exc)},
            )
            return await self._dispatch_inline(org_id, event_type, payload, run_id, retain_payload, team_id)

    async def _get_subscribed_endpoints(
        self,
        org_id: uuid.UUID,
        event_type: str,
        *,
        team_id: uuid.UUID | None = None,
    ) -> list[NotificationEndpoint]:
        """Return active endpoints subscribed to ``event_type``.

        When ``team_id`` is provided, first queries endpoints matching the
        team. If none match, falls back to org-wide (team_id IS NULL)
        endpoints.

        When ``team_id`` is None, returns only org-wide endpoints.
        """
        async with self._session_factory() as session:
            if team_id is not None:
                stmt = select(NotificationEndpoint).where(
                    NotificationEndpoint.organisation_id == org_id,
                    NotificationEndpoint.team_id == team_id,
                    NotificationEndpoint.auto_disabled.is_(False),
                )
                result = await session.execute(stmt)
                all_endpoints = list(result.scalars())
                if not all_endpoints:
                    stmt = select(NotificationEndpoint).where(
                        NotificationEndpoint.organisation_id == org_id,
                        NotificationEndpoint.team_id.is_(None),
                        NotificationEndpoint.auto_disabled.is_(False),
                    )
                    result = await session.execute(stmt)
                    all_endpoints = list(result.scalars())
            else:
                result = await session.execute(
                    select(NotificationEndpoint).where(
                        NotificationEndpoint.organisation_id == org_id,
                        NotificationEndpoint.team_id.is_(None),
                        NotificationEndpoint.auto_disabled.is_(False),
                    )
                )
                all_endpoints = list(result.scalars())
        subscribed = []
        for ep in all_endpoints:
            try:
                events_list = json.loads(ep.events)
            except (json.JSONDecodeError, TypeError):
                _log.warning(
                    "notifier.unparseable_events_json",
                    extra={"endpoint_id": str(ep.id), "org_id": str(org_id)},
                )
                continue
            if event_type in events_list:
                subscribed.append(ep)
        return subscribed

    async def _dispatch_inline(
        self,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        run_id: uuid.UUID | None,
        retain_payload: bool,
        team_id: uuid.UUID | None = None,
    ) -> list[DispatchResult]:
        endpoints = await self._get_subscribed_endpoints(org_id, event_type, team_id=team_id)
        if not endpoints:
            _log.debug("notifier.no_subscribers", extra={"event_type": event_type, "org_id": str(org_id)})
            return []
        http_client = await self._get_client()
        results: list[DispatchResult] = []
        for ep in endpoints:
            result = await self._dispatch_to_endpoint(http_client, ep, event_type, payload, run_id, retain_payload)
            results.append(result)
        return results

    async def _dispatch_to_endpoint(
        self,
        client: httpx.AsyncClient,
        endpoint: NotificationEndpoint,
        event_type: str,
        payload: dict[str, Any],
        run_id: uuid.UUID | None,
        retain_payload: bool,
    ) -> DispatchResult:
        """Send a single notification to one endpoint with retry logic."""
        body = json.dumps(
            {
                "event": event_type,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": payload,
            },
            default=str,
            separators=(",", ":"),
        ).encode()

        signature = await self._sign_payload(body, endpoint)

        last_error: str | None = None
        response_code: int | None = None
        succeeded = False
        attempt_count = 0

        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempt_count = attempt
            try:
                resp = await client.post(
                    endpoint.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Modulo-Signature": signature,
                        "User-Agent": "Modulo-Notifier/1.0",
                    },
                )
                response_code = resp.status_code
                if resp.is_success:
                    succeeded = True
                    break
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.RequestError as exc:
                last_error = f"RequestError: {exc}"
                response_code = None

            if attempt < MAX_ATTEMPTS:
                _log.warning(
                    "notifier.delivery_attempt_failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": MAX_ATTEMPTS,
                        "endpoint_id": str(endpoint.id),
                        "last_error": last_error,
                    },
                )
                delay_idx = min(attempt - 1, len(RETRY_DELAYS) - 1)
                await asyncio.sleep(RETRY_DELAYS[delay_idx])

        status = "delivered" if succeeded else "dead_lettered"

        payload_ciphertext: bytes | None = None
        if retain_payload:
            try:
                payload_ciphertext = self._fernet.encrypt(body)
            except Exception:
                _log.exception("notifier.encrypt_failed", extra={"endpoint_id": str(endpoint.id)})

        await self._record_delivery(
            endpoint,
            event_type,
            run_id,
            status,
            attempt_count,
            response_code,
            last_error,
            payload_ciphertext,
        )

        if status == "dead_lettered":
            await self._increment_dead_letter(endpoint)
        else:
            await self._reset_dead_letter(endpoint)

        return DispatchResult(
            endpoint_id=endpoint.id,
            status=status,
            attempt_count=attempt_count,
            response_code=response_code,
            last_error=last_error,
        )

    async def _sign_payload(self, body: bytes, endpoint: NotificationEndpoint) -> str:
        """Build HMAC-SHA256 signature over the JSON body.
        Returns empty string if the endpoint has no secret configured.
        """
        if endpoint.secret_ciphertext is None:
            return ""
        try:
            raw_secret = self._fernet.decrypt(endpoint.secret_ciphertext)
        except InvalidToken:
            _log.error(
                "notifier.decrypt_failed",
                extra={"endpoint_id": str(endpoint.id), "org_id": str(endpoint.organisation_id)},
            )
            return ""
        sig = hmac.new(raw_secret, body, hashlib.sha256).hexdigest()
        return f"sha256={sig}"

    async def _record_delivery(
        self,
        endpoint: NotificationEndpoint,
        event_type: str,
        run_id: uuid.UUID | None,
        status: str,
        attempt_count: int,
        response_code: int | None,
        last_error: str | None,
        payload_ciphertext: bytes | None,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                log_entry = NotificationDeliveryLog(
                    organisation_id=endpoint.organisation_id,
                    event_type=event_type,
                    endpoint_id=endpoint.id,
                    run_id=run_id,
                    status=status,
                    attempt_count=attempt_count,
                    response_code=response_code,
                    last_error=last_error,
                    payload_ciphertext=payload_ciphertext,
                )
                session.add(log_entry)
        except Exception:
            _log.exception(
                "notifier.record_delivery_failed",
                extra={
                    "endpoint_id": str(endpoint.id),
                    "event_type": event_type,
                    "status": status,
                    "attempt_count": attempt_count,
                },
            )

    async def _increment_dead_letter(self, endpoint: NotificationEndpoint) -> None:
        """Increment dead-letter counter and auto-disable if threshold exceeded."""
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(NotificationEndpoint)
                    .where(NotificationEndpoint.id == endpoint.id)
                    .values(
                        consecutive_dead_letter_count=(NotificationEndpoint.consecutive_dead_letter_count + 1),
                    )
                    .returning(NotificationEndpoint.consecutive_dead_letter_count)
                )
                new_count = result.scalar_one()

                if new_count >= MAX_DEAD_LETTERS:
                    await session.execute(
                        update(NotificationEndpoint)
                        .where(NotificationEndpoint.id == endpoint.id)
                        .values(
                            auto_disabled=True,
                            disabled_at=datetime.now(UTC),
                        )
                    )
                    _log.warning(
                        "notifier.auto_disabled",
                        extra={"endpoint_id": str(endpoint.id), "dead_letter_count": new_count},
                    )
        except Exception:
            _log.exception(
                "notifier.increment_dead_letter_failed",
                extra={"endpoint_id": str(endpoint.id)},
            )

    async def _reset_dead_letter(self, endpoint: NotificationEndpoint) -> None:
        """Reset consecutive dead-letter counter to 0 on successful delivery."""
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    update(NotificationEndpoint)
                    .where(
                        NotificationEndpoint.id == endpoint.id,
                        NotificationEndpoint.consecutive_dead_letter_count > 0,
                    )
                    .values(consecutive_dead_letter_count=0)
                )
        except Exception:
            _log.exception(
                "notifier.reset_dead_letter_failed",
                extra={"endpoint_id": str(endpoint.id)},
            )

    async def close(self) -> None:
        """Close the underlying HTTP client, if one was created."""
        client = self._http_client
        if client is not None and not client.is_closed:
            self._http_client = None
            try:
                await client.aclose()
            except Exception:
                _log.exception("notifier.http_client_close_failed")
            else:
                _log.debug("notifier.http_client_closed")
