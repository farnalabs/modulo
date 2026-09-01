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
import contextlib
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

from modulo.core.ssrf import pinned_async_client
from modulo.db.crud.break_glass_deny import is_break_glass_denied, is_break_glass_live
from modulo.db.models.account import Account
from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.rls import set_rls_org

__all__ = [
    "EVENT_BUDGET_EXCEEDED",
    "EVENT_CIRCUIT_BREAKER_TRIPPED",
    "EVENT_CLAIM_EXPIRED",
    "EVENT_EVAL_BLOCKED",
    "EVENT_EVAL_REGRESSION",
    "EVENT_FEEDBACK_PENDING",
    "EVENT_GUARDRAIL_ENFORCEMENT_GAP",
    "EVENT_GUARDRAIL_KILL_SWITCH",
    "EVENT_GUARDRAIL_UNEXPECTED_SKIP",
    "EVENT_HITL_AWAITING",
    "EVENT_HITL_OVERDUE",
    "EVENT_RUN_FAILED",
    "EVENT_RUN_STALLED",
    "EVENT_SYSTEM_ANNOUNCEMENT",
    "EVENT_TRIGGER_DEACTIVATED",
    "MAX_ATTEMPTS",
    "MAX_DEAD_LETTERS",
    "RETRY_DELAYS",
    "DispatchResult",
    "Notifier",
    "endpoint_events_to_list",
]

_log = logging.getLogger(__name__)


def endpoint_events_to_list(raw_events: object) -> list[str]:
    """Normalise ``NotificationEndpoint.events`` (a list or JSON string) to a list of strings.

    Persisted values may be a JSON-encoded string or a native list; this single
    helper keeps the API read paths (notifications/admin_notifications) in sync.
    """
    if isinstance(raw_events, list) and not any(not isinstance(event, str) for event in raw_events):
        return raw_events
    if isinstance(raw_events, str):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(raw_events)
            if isinstance(parsed, list) and not any(not isinstance(event, str) for event in parsed):
                return parsed
    return []


# Lazy OTel counter — records the fail-closed owner-read drop so a DB blip that
# suppresses ALL webhook dispatch for an event is observable (review #657 obs 1).
_owner_read_failures_total: Any = None


def _record_owner_read_failure() -> None:
    """Increment the break-glass owner-read failure counter (no-op when OTel is absent)."""
    global _owner_read_failures_total
    if _owner_read_failures_total is None:
        try:
            from opentelemetry import metrics

            provider = metrics.get_meter_provider()
            if provider is None:
                return
            meter = provider.get_meter("modulo.notifier", version="0.1.0")
            _owner_read_failures_total = meter.create_counter(
                name="modulo_notifier_break_glass_owner_read_failures_total",
                description="Fail-closed webhook dispatch suppressions from break-glass owner-read DB errors",
                unit="1",
            )
        except Exception:
            _log.warning("notifier.metrics_owner_read_counter_failed")
            return
    _owner_read_failures_total.add(1)


MAX_ATTEMPTS = 4  # 1 initial + 3 retries
MAX_DEAD_LETTERS = 10
RETRY_DELAYS = [1.0, 5.0, 30.0]

# Event type constants — single source of truth
EVENT_HITL_AWAITING = "hitl_awaiting"
EVENT_RUN_FAILED = "run_failed"
EVENT_RUN_STALLED = "run_stalled"
EVENT_BUDGET_EXCEEDED = "budget_exceeded"
EVENT_CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"
EVENT_CLAIM_EXPIRED = "claim_expired"
EVENT_HITL_OVERDUE = "hitl_overdue"
EVENT_EVAL_REGRESSION = "eval_regression"
EVENT_EVAL_BLOCKED = "eval_blocked"
EVENT_FEEDBACK_PENDING = "feedback_pending"
EVENT_SYSTEM_ANNOUNCEMENT = "system_announcement"
# FAR-190 — an ongoing trigger auto-deactivated after N consecutive no-delivery
# runs. Payload is sanitised (identifiers/titles + allow-listed reason fields).
EVENT_TRIGGER_DEACTIVATED = "trigger_deactivated"
# FAR-223 — a pinned guardrail could not be evaluated (soft-deleted live row)
# or a block-action guardrail is non-conformant: the enforcement gap is a
# paging alert so the operator sees the control has silently stopped enforcing.
EVENT_GUARDRAIL_ENFORCEMENT_GAP = "guardrail_enforcement_gap"
# FAR-223 — the org's guardrails kill-switch was just enabled: all bound
# guardrails downgraded to observe (shadow-only). Alert on enable so the
# downgrade is never silent.
EVENT_GUARDRAIL_KILL_SWITCH = "guardrail_kill_switch"
# FAR-223 item 11 — a guardrail was skipped for a reason NOT explained by
# soft-deleted snapshot-pin state (an unexpected skip): the control silently
# stopped evaluating and the operator must be paged.
EVENT_GUARDRAIL_UNEXPECTED_SKIP = "guardrail_unexpected_skip"


@dataclass
class DispatchResult:
    endpoint_id: uuid.UUID
    status: str
    attempt_count: int
    response_code: int | None = None
    last_error: str | None = None


class Notifier:
    """Dispatch notifications to configured endpoints with retry and dead-letter."""

    def __init__(self, db_engine: AsyncEngine, fernet_key: str) -> None:
        self._engine = db_engine
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        try:
            self._fernet = Fernet(fernet_key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid Fernet key: {exc}") from exc
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._http_client_lock:
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=30.0)
                )
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

        When ``team_id`` is provided, dispatches to team-specific endpoints
        first, falling back to org-wide (team_id IS NULL) endpoints if no
        team-specific endpoints are configured for the event type.

        When ``team_id`` is None, dispatches only to org-wide endpoints.

        Returns a list of DispatchResult, one per endpoint.
        """
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
        team. If none match by subscription, falls back to org-wide
        (team_id IS NULL) endpoints.

        When ``team_id`` is None, returns only org-wide endpoints.
        """

        async def _query(team_filter: uuid.UUID | None) -> list[NotificationEndpoint]:
            stmt = select(NotificationEndpoint).where(
                NotificationEndpoint.organisation_id == org_id,
                NotificationEndpoint.team_id.is_(team_filter),
                NotificationEndpoint.auto_disabled.is_(False),
            )
            async with self._session_factory() as session:
                result = await session.execute(stmt)
                return self._filter_subscribed(list(result.scalars()), event_type)

        if team_id is not None:
            subscribed = await _query(team_id)
            if subscribed:
                return subscribed
            return await _query(None)

        return await _query(None)

    def _filter_subscribed(
        self,
        endpoints: list[NotificationEndpoint],
        event_type: str,
    ) -> list[NotificationEndpoint]:
        """Filter endpoints whose events JSON includes ``event_type``."""
        subscribed: list[NotificationEndpoint] = []
        for ep in endpoints:
            raw_events = ep.events
            if isinstance(raw_events, list):
                events_list = raw_events
            else:
                try:
                    events_list = json.loads(raw_events)
                except (json.JSONDecodeError, TypeError):
                    _log.warning(
                        "notifier.unparseable_events_json",
                        extra={"endpoint_id": str(ep.id), "org_id": str(ep.organisation_id)},
                    )
                    continue
            if event_type in events_list:
                subscribed.append(ep)
        return subscribed

    async def _reject_break_glass_owned(self, endpoints: list[NotificationEndpoint]) -> list[NotificationEndpoint]:
        """Return endpoints whose owning account is NOT a break-glass account.

        Use-time revalidation TRIM to webhook dispatch only (plan v17, API-key +
        long-lived deny). The mint-marker covers create/update/delete routes, but a
        webhook endpoint owned by a break-glass account can still exist — created
        before the deny was active, or via a raw-DB forgery. Re-check at dispatch
        time and skip those endpoints, fail-closed, with per-endpoint isolation (one
        bad endpoint never blocks the others).

        The owner rule is the shared ``is_break_glass_denied`` / ``is_break_glass_live``
        union from ``db.crud.break_glass_deny`` (single-sourced, never duplicated
        here). Owners are loaded in ONE batched query. An endpoint with no owner
        (``account_id IS NULL``) has nothing to deny and is kept — likewise an owner
        reference that is not a ``uuid.UUID`` cannot reference an ``accounts`` row and
        therefore cannot be a break-glass account. If a real owner cannot be resolved
        (DB read error, orphaned owner id), the endpoint is treated as denied and
        skipped — a DB blip must not fail-open a break-glass endpoint.
        """
        if not endpoints:
            return []
        owner_ids = {ep.account_id for ep in endpoints if isinstance(ep.account_id, uuid.UUID)}
        if not owner_ids:
            return list(endpoints)
        try:
            async with self._session_factory() as session:
                result = await session.execute(select(Account).where(Account.id.in_(owner_ids)))
                owners = {account.id: account for account in result.scalars()}
        except asyncio.CancelledError:
            raise
        except Exception:
            _record_owner_read_failure()
            _log.exception(
                "notifier.break_glass_owner_read_failed",
                extra={"endpoint_count": len(endpoints)},
            )
            return []

        now = datetime.now(UTC)
        kept: list[NotificationEndpoint] = []
        for ep in endpoints:
            owner = owners.get(ep.account_id) if isinstance(ep.account_id, uuid.UUID) else None
            if owner is None:
                if isinstance(ep.account_id, uuid.UUID):
                    _log.warning(
                        "notifier.break_glass_owner_missing",
                        extra={"endpoint_id": str(ep.id), "org_id": str(ep.organisation_id)},
                    )
                else:
                    kept.append(ep)
                continue
            # Review #657 obs 2: the shared builders already gate on
            # ``is_break_glass``, so the outer ``owner.is_break_glass is True and
            # (...`` guard is redundant — the deny decision is single-sourced.
            if is_break_glass_denied(
                is_break_glass=owner.is_break_glass,
                break_glass_expires_at=owner.break_glass_expires_at,
                break_glass_deactivated_at=owner.break_glass_deactivated_at,
                active=owner.active,
                now=now,
            ) or is_break_glass_live(
                is_break_glass=owner.is_break_glass,
                break_glass_expires_at=owner.break_glass_expires_at,
                break_glass_deactivated_at=owner.break_glass_deactivated_at,
                active=owner.active,
                now=now,
            ):
                _log.warning(
                    "notifier.break_glass_webhook_skipped",
                    extra={"endpoint_id": str(ep.id), "org_id": str(ep.organisation_id)},
                )
                continue
            kept.append(ep)
        return kept

    async def _dispatch_inline(
        self,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        run_id: uuid.UUID | None,
        retain_payload: bool,
        team_id: uuid.UUID | None = None,
    ) -> list[DispatchResult]:
        # hitl-gate-removal-guard-plan.md v19 §5: the early `if not endpoints:
        # return []` was a silent-loss bug — it made in-app Notification
        # creation unreachable whenever an org had zero webhook subscribers.
        # Webhook dispatch (a zero-iteration loop when ``endpoints`` is empty)
        # and in-app notification creation are two independent, always-executed
        # steps.
        endpoints = await self._get_subscribed_endpoints(org_id, event_type, team_id=team_id)
        endpoints = await self._reject_break_glass_owned(endpoints)
        if not endpoints:
            _log.debug("notifier.no_subscribers", extra={"event_type": event_type, "org_id": str(org_id)})
        results: list[DispatchResult] = []
        if endpoints:
            body = json.dumps(
                {
                    "event": event_type,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "payload": payload,
                },
                default=str,
                separators=(",", ":"),
            ).encode()
            for ep in endpoints:
                result = await self._dispatch_endpoint_pinned(ep, event_type, body, run_id, retain_payload)
                results.append(result)

        # Create in-app notification record alongside webhook dispatches
        try:
            from modulo.core.notifier.event_mapper import NotificationEventMapper

            mapper = NotificationEventMapper()
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await mapper.create_from_event(
                    session,
                    org_id=org_id,
                    event_type=event_type,
                    payload=payload,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "notifier.in_app_notification_failed", extra={"event_type": event_type, "org_id": str(org_id)}
            )

        return results

    async def _build_dispatch_client(self, url: str) -> httpx.AsyncClient:
        """Build the dispatch HTTP client for one endpoint URL, pinned to its
        resolved address.

        Each endpoint URL is validated at save time. A validate-at-save check
        alone leaves the DNS-rebinding window open (the URL can be re-resolved
        to an internal/metadata address between save and dispatch). Building the
        client via :func:`pinned_async_client` re-resolves and re-validates the
        URL at dispatch and pins the TCP connection to the validated address
        while keeping the original hostname for TLS SNI/cert. If the URL now
        resolves to a blocked/internal address, this raises ValueError and the
        dispatch fails CLOSED (no request is made to the unvalidated host). A
        fresh per-endpoint client is used so each URL gets its own pin.
        """
        client = await pinned_async_client(url)
        client.timeout = httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=30.0)
        return client

    async def _dispatch_endpoint_pinned(
        self,
        endpoint: NotificationEndpoint,
        event_type: str,
        body: bytes,
        run_id: uuid.UUID | None,
        retain_payload: bool,
    ) -> DispatchResult:
        """Send one notification to one endpoint via a pinned, re-validated client.

        Wraps the retry loop in :meth:`_dispatch_to_endpoint` with an SSRF-safe
        dispatch client. If re-validating the saved URL at dispatch time fails
        (the host now resolves to an internal/private address — the DNS-rebinding
        residual), the endpoint is dead-lettered and no request is made.
        """
        try:
            client = await self._build_dispatch_client(endpoint.url)
        except ValueError as exc:
            _log.warning(
                "notifier.dispatch_endpoint_ssrf_rejected",
                extra={
                    "endpoint_id": str(endpoint.id),
                    "org_id": str(endpoint.organisation_id),
                    "error": str(exc),
                },
            )
            await self._record_delivery(endpoint, event_type, run_id, "dead_lettered", 0, None, str(exc), None)
            await self._increment_dead_letter(endpoint)
            return DispatchResult(
                endpoint_id=endpoint.id,
                status="dead_lettered",
                attempt_count=0,
                last_error=str(exc),
            )
        try:
            return await self._dispatch_to_endpoint(client, endpoint, event_type, body, run_id, retain_payload)
        finally:
            await client.aclose()

    async def _dispatch_to_endpoint(
        self,
        client: httpx.AsyncClient,
        endpoint: NotificationEndpoint,
        event_type: str,
        body: bytes,
        run_id: uuid.UUID | None,
        retain_payload: bool,
    ) -> DispatchResult:
        """Send a single notification to one endpoint with retry logic."""
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
                        "X-Modulo-Timestamp": datetime.now(UTC).isoformat(),
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
                if response_code == 429 and resp is not None:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = min(float(retry_after), 60.0)
                        except (ValueError, TypeError):
                            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    else:
                        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                else:
                    delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)

        status = "delivered" if succeeded else "dead_lettered"

        payload_ciphertext: bytes | None = None
        if retain_payload:
            try:
                payload_ciphertext = self._fernet.encrypt(body)
            except asyncio.CancelledError:
                raise
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
        Returns empty string and logs an error if the secret cannot be decrypted.
        """
        if endpoint.secret_ciphertext is None:
            return ""
        try:
            raw_secret = self._fernet.decrypt(endpoint.secret_ciphertext)
        except InvalidToken:
            _log.exception(
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
                await set_rls_org(session, endpoint.organisation_id)
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
        except asyncio.CancelledError:
            raise
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
                await set_rls_org(session, endpoint.organisation_id)
                result = await session.execute(
                    update(NotificationEndpoint)
                    .where(
                        NotificationEndpoint.id == endpoint.id,
                        NotificationEndpoint.organisation_id == endpoint.organisation_id,
                    )
                    .values(
                        consecutive_dead_letter_count=(NotificationEndpoint.consecutive_dead_letter_count + 1),
                    )
                    .returning(NotificationEndpoint.consecutive_dead_letter_count)
                )
                new_count = result.scalar_one()

                if new_count >= MAX_DEAD_LETTERS:
                    await session.execute(
                        update(NotificationEndpoint)
                        .where(
                            NotificationEndpoint.id == endpoint.id,
                            NotificationEndpoint.organisation_id == endpoint.organisation_id,
                        )
                        .values(
                            auto_disabled=True,
                            disabled_at=datetime.now(UTC),
                        )
                    )
                    _log.warning(
                        "notifier.auto_disabled",
                        extra={"endpoint_id": str(endpoint.id), "dead_letter_count": new_count},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "notifier.increment_dead_letter_failed",
                extra={"endpoint_id": str(endpoint.id)},
            )

    async def _reset_dead_letter(self, endpoint: NotificationEndpoint) -> None:
        """Reset consecutive dead-letter counter to 0 on successful delivery."""
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, endpoint.organisation_id)
                await session.execute(
                    update(NotificationEndpoint)
                    .where(
                        NotificationEndpoint.id == endpoint.id,
                        NotificationEndpoint.organisation_id == endpoint.organisation_id,
                        NotificationEndpoint.consecutive_dead_letter_count > 0,
                    )
                    .values(consecutive_dead_letter_count=0)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "notifier.reset_dead_letter_failed",
                extra={"endpoint_id": str(endpoint.id)},
            )

    async def close(self) -> None:
        """Close the underlying HTTP client, if one was created."""
        client: httpx.AsyncClient | None = None
        async with self._http_client_lock:
            if self._http_client is not None and not self._http_client.is_closed:
                client = self._http_client
                self._http_client = None
        if client is not None:
            await client.aclose()
