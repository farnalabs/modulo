"""TriggerEngine — webhook validation, deduplication, flood protection, and run creation.

Webhook processing pipeline:
  1. Load trigger config from DB (with FOR UPDATE lock)
  2. X-Modulo-Timestamp replay window check (±300s)
  3. HMAC-SHA256 validation over timestamp.body (if hmac_secret configured)
  4. Deduplication (WebhookDedupHash — payload hash, 5-min TTL)
  5. Flood protection (concurrent run count vs. trigger.max_concurrent_runs)
  6. Payload mapping (dot-notation path → input_payload key)
  7. Create Run + TriggerEvent in one transaction
  8. Dedup hash committed with run (single atomic unit)

All outcomes (pass and fail) are recorded as a TriggerEvent row.
The caller is responsible for background execution of the created run.
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.connectors.base import ConnectorQuery
from modulo.core.secrets_backend import create_secrets_backend
from modulo.core.trigger_engine.polling import evaluate_condition as _evaluate_condition
from modulo.db.crud.run import create_run
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.webhook import WebhookDedupHash, WebhookPayload

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TriggerNotFoundError(KeyError):
    def __init__(self, trigger_id: uuid.UUID) -> None:
        super().__init__(str(trigger_id))
        self.trigger_id = trigger_id


class TriggerInactiveError(RuntimeError):
    def __init__(self, trigger_id: uuid.UUID) -> None:
        super().__init__(f"Trigger {trigger_id} is not active")
        self.trigger_id = trigger_id


class HmacValidationError(PermissionError):
    def __init__(self) -> None:
        super().__init__("HMAC-SHA256 signature is missing or invalid")


class TimestampExpiredError(PermissionError):
    def __init__(self) -> None:
        super().__init__("X-Modulo-Timestamp is outside the ±300s replay window")


class DuplicateWebhookError(RuntimeError):
    def __init__(self, payload_hash: str) -> None:
        super().__init__(f"Duplicate webhook payload: {payload_hash}")
        self.payload_hash = payload_hash


class ConcurrentRunLimitError(RuntimeError):
    def __init__(self, trigger_id: uuid.UUID, limit: int) -> None:
        super().__init__(f"Trigger {trigger_id} already has {limit} concurrent run(s); limit reached")
        self.trigger_id = trigger_id
        self.limit = limit


class ReplayNotFoundError(KeyError):
    def __init__(self, event_id: uuid.UUID) -> None:
        super().__init__(str(event_id))
        self.event_id = event_id


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEDUP_TTL_SECONDS = 300  # 5 minutes
_REPLAY_WINDOW_SECONDS = 300  # ±300s for X-Modulo-Timestamp
_ACTIVE_STATUSES = ("pending", "running", "awaiting_human", "claimed", "waiting_for_lock")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_timestamp(modulo_timestamp: str | None) -> int:
    """Validate and return the Unix timestamp from the X-Modulo-Timestamp header.

    Raises TimestampExpiredError if the header is missing or outside the
    ±300s replay window.
    """
    if modulo_timestamp is None:
        raise TimestampExpiredError()
    try:
        ts = int(modulo_timestamp)
    except (ValueError, TypeError):
        raise TimestampExpiredError() from None
    now = time.time()
    if abs(now - ts) > _REPLAY_WINDOW_SECONDS:
        raise TimestampExpiredError()
    return ts


def _verify_hmac(raw_body: bytes, secret: str, signature_header: str | None, timestamp: int | None = None) -> bool:
    """Return True if the HMAC-SHA256 signature matches ``timestamp.body``.

    When *timestamp* is provided, the HMAC is computed over
    ``f"{timestamp}.{raw_body}"`` (as UTF-8). If *timestamp* is None,
    falls back to body-only signing for backward compatibility.
    """
    if signature_header is None:
        return False
    if timestamp is not None:
        payload = f"{timestamp}.".encode() + raw_body
    else:
        payload = raw_body
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _extract_field(payload: dict[str, Any], path: str) -> Any:
    """Extract a value from a nested dict using dot notation (e.g. 'pull_request.head.sha')."""
    parts = path.split(".")
    value: Any = payload
    for part in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _apply_payload_mapping(raw_payload: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Map raw webhook payload fields to input_payload using dot-notation paths.

    If mapping is empty, the raw payload is used as-is.
    """
    if not mapping:
        return dict(raw_payload)
    return {target_key: _extract_field(raw_payload, src_path) for target_key, src_path in mapping.items()}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TriggerEngine:
    """Stateless service — pass a session per call."""

    async def handle_webhook(
        self,
        session: AsyncSession,
        *,
        trigger_id: uuid.UUID,
        org_id: uuid.UUID,
        raw_body: bytes,
        raw_payload: dict[str, Any],
        hmac_signature: str | None,
        modulo_timestamp: str | None = None,
        snapshot_id: uuid.UUID,
    ) -> tuple[Run, TriggerEvent, dict[str, Any]]:
        """Process an incoming webhook. Returns (Run, TriggerEvent, input_payload) on success.

        All validation failures are raised as typed exceptions. A TriggerEvent is
        always written (pass or fail) so every delivery attempt is audited.
        The caller must have already set RLS context on the session.
        """
        trigger = await self._load_trigger(session, trigger_id, org_id)
        payload_hash = _sha256_hex(raw_body)

        # X-Modulo-Timestamp replay window check
        try:
            ts = _verify_timestamp(modulo_timestamp)
        except TimestampExpiredError:
            await self._log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                payload_hash=payload_hash,
                result="timestamp_expired",
            )
            raise

        # HMAC validation (only if secret is configured)
        # HMAC is computed over timestamp.body for replay protection
        hmac_secret: str | None = trigger.config_json.get("hmac_secret")
        if hmac_secret:
            if not _verify_hmac(raw_body, hmac_secret, hmac_signature, timestamp=ts):
                await self._log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    payload_hash=payload_hash,
                    result="hmac_failed",
                )
                raise HmacValidationError()

        # Deduplication — insert dedup hash via savepoint; unique constraint handles races.
        is_new = await self._try_insert_dedup(session, trigger_id, org_id, payload_hash)
        if not is_new:
            await self._log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                payload_hash=payload_hash,
                result="deduplicated",
            )
            raise DuplicateWebhookError(payload_hash)

        # Flood / concurrency protection
        active_count = await self._count_active_runs(session, trigger.pipeline_id)
        if active_count >= trigger.max_concurrent_runs:
            await self._log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                payload_hash=payload_hash,
                result="concurrency_limit_reached",
            )
            raise ConcurrentRunLimitError(trigger_id, trigger.max_concurrent_runs)

        # Payload mapping → input payload for the run
        mapping: dict[str, str] = trigger.config_json.get("payload_mapping", {})
        input_payload = _apply_payload_mapping(raw_payload, mapping)

        # Create run
        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=trigger.pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="webhook",
            input_payload=input_payload,
            trigger_id=trigger_id,
        )

        # Audit log — success
        trigger_event = await self._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=payload_hash,
            result="accepted",
            run_id=run.id,
        )

        # Store raw payload for replay (link to trigger_event)
        await self._store_raw_payload(
            session,
            trigger_event_id=trigger_event.id,
            raw_body=raw_body,
            raw_payload=raw_payload,
            org_id=org_id,
        )

        return run, trigger_event, input_payload

    async def replay_event(
        self,
        session: AsyncSession,
        *,
        event_id: uuid.UUID,
        org_id: uuid.UUID,
        snapshot_id: uuid.UUID,
    ) -> tuple[Run, TriggerEvent, dict[str, Any]]:
        """Re-fire a webhook run from a previous TriggerEvent log entry.

        Loads the original raw payload and re-runs the webhook pipeline.
        """
        result = await session.execute(
            select(TriggerEvent).where(
                TriggerEvent.id == event_id,
                TriggerEvent.organisation_id == org_id,
                TriggerEvent.trigger_type == "webhook",
                TriggerEvent.validation_result.in_(["accepted"]),
            )
        )
        event = result.scalar_one_or_none()
        if event is None:
            raise ReplayNotFoundError(event_id)

        # Load trigger
        trigger_result = await session.execute(
            select(Trigger).where(
                Trigger.id == event.trigger_id,
                Trigger.organisation_id == org_id,
            )
        )
        trigger = trigger_result.scalar_one_or_none()
        if trigger is None:
            raise TriggerNotFoundError(event.trigger_id)
        if not trigger.active:
            raise TriggerInactiveError(event.trigger_id)

        # Load raw payload from WebhookPayload
        payload_result = await session.execute(
            select(WebhookPayload).where(
                WebhookPayload.trigger_event_id == event_id,
                WebhookPayload.organisation_id == org_id,
            )
        )
        stored = payload_result.scalar_one_or_none()
        if stored is None:
            raise ReplayNotFoundError(event_id)

        raw_payload = stored.raw_payload
        raw_body = stored.raw_body

        # Run the rest of the pipeline (skip HMAC + timestamp validation)
        payload_hash = _sha256_hex(raw_body)

        # Deduplication check (replay must skip the original event's dedup hash)
        is_new = await self._try_insert_dedup(session, trigger.id, org_id, payload_hash)
        if not is_new:
            raise DuplicateWebhookError(payload_hash)

        # Flood protection
        active_count = await self._count_active_runs(session, trigger.pipeline_id)
        if active_count >= trigger.max_concurrent_runs:
            raise ConcurrentRunLimitError(trigger.id, trigger.max_concurrent_runs)

        # Payload mapping
        mapping: dict[str, str] = trigger.config_json.get("payload_mapping", {})
        input_payload = _apply_payload_mapping(raw_payload, mapping)

        # Create run
        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=trigger.pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="webhook",
            input_payload=input_payload,
            trigger_id=trigger.id,
        )

        trigger_event = await self._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=payload_hash,
            result="accepted",
            run_id=run.id,
        )

        return run, trigger_event, input_payload

    # ------------------------------------------------------------------
    # Dedup cleanup
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Polling trigger
    # ------------------------------------------------------------------

    async def schedule_polling_trigger(
        self,
        session: AsyncSession,
        *,
        trigger: Trigger,
        org_id: uuid.UUID,
    ) -> None:
        """Register/update a polling trigger's Celery periodic schedule.

        Computes ``next_fire_at`` from ``poll_interval_seconds`` and persists it
        on the trigger row. The ``DatabasePollingScheduler`` will pick it up on
        the next beat tick — there is no in-memory scheduler to update directly.
        """
        config = trigger.config_json or {}
        interval = int(config.get("poll_interval_seconds", 60))
        now = datetime.now(UTC)
        trigger.next_fire_at = now + timedelta(seconds=interval)
        await session.flush()

    @staticmethod
    async def evaluate_condition(
        session: AsyncSession,
        *,
        trigger: Trigger,
        org_id: uuid.UUID,
        connector_instance_id: uuid.UUID,
        poll_query: str,
        condition_expression: str | None = None,
    ) -> dict[str, Any]:
        """Run *poll_query* via the connector and evaluate *condition_expression*.

        Returns a dict with keys:
          - ``status``: ``"condition_met"`` | ``"no_match"`` | ``"error"``
          - ``records``: query result records (only on success)
          - ``error``: error detail (only on error)

        This is a sync-friendly evaluation meant for testing or manual one-off
        checks. For automatic scheduled evaluation use the Celery task path.
        """
        from modulo.core.trigger_engine.polling import (
            _build_polling_connector,
        )
        from modulo.settings import get_settings

        settings = get_settings()

        conn_result = await session.execute(
            select(ConnectorInstance).where(
                ConnectorInstance.id == connector_instance_id,
                ConnectorInstance.organisation_id == org_id,
            )
        )
        instance = conn_result.scalar_one_or_none()
        if instance is None:
            return {
                "status": "error",
                "error": f"Connector instance {connector_instance_id} not found",
            }

        try:
            secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
            raw_creds = await secrets_backend.get_secret(str(instance.id))
            creds: dict[str, Any] = json.loads(raw_creds)
            connector = _build_polling_connector(
                instance.connector_type_id,
                instance.config_json,
                creds,
            )
        except Exception as exc:
            return {"status": "error", "error": f"Connector init failed: {exc}"}

        try:
            query = ConnectorQuery(resource=poll_query)
            query_result = await connector.query(query)
        except Exception as exc:
            return {"status": "error", "error": f"Query failed: {exc}"}

        try:
            matched = _evaluate_condition(query_result, condition_expression)
        except Exception as exc:
            return {"status": "error", "error": f"Condition evaluation failed: {exc}"}

        return {
            "status": "condition_met" if matched else "no_match",
            "records": query_result.records,
            "total": query_result.total,
        }

    # ------------------------------------------------------------------
    # Dedup cleanup
    # ------------------------------------------------------------------

    @staticmethod
    async def cleanup_expired_dedup_hashes(session: AsyncSession) -> int:
        """Delete expired webhook_dedup_hashes rows.

        Acquires a Postgres advisory lock (key=20250601) to prevent concurrent
        cleanup across workers. Returns the number of deleted rows.
        """
        lock_acquired = await session.execute(text("SELECT pg_try_advisory_xact_lock(20250601)"))
        if not lock_acquired.scalar_one():
            return 0

        now = datetime.now(UTC)
        result = await session.execute(select(WebhookDedupHash.id).where(WebhookDedupHash.expires_at <= now))
        expired_ids = result.scalars().all()
        if not expired_ids:
            return 0

        await session.execute(delete(WebhookDedupHash).where(WebhookDedupHash.id.in_(expired_ids)))
        return len(expired_ids)

    @staticmethod
    async def cleanup_expired_payloads(session: AsyncSession) -> int:
        """Delete expired webhook_payloads rows. Returns the number of deleted rows."""
        now = datetime.now(UTC)
        result = await session.execute(select(WebhookPayload.id).where(WebhookPayload.expires_at <= now))
        expired_ids = result.scalars().all()
        if not expired_ids:
            return 0

        await session.execute(
            text("DELETE FROM webhook_payloads WHERE id = ANY(:ids)"),
            {"ids": list(expired_ids)},
        )
        return len(expired_ids)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _load_trigger(
        self,
        session: AsyncSession,
        trigger_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> Trigger:
        """Load trigger with FOR UPDATE lock to serialise concurrent webhook requests."""
        result = await session.execute(
            select(Trigger)
            .where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == org_id,
            )
            .with_for_update()
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise TriggerNotFoundError(trigger_id)
        if not trigger.active:
            raise TriggerInactiveError(trigger_id)
        return trigger

    async def _count_active_runs(self, session: AsyncSession, pipeline_id: uuid.UUID) -> int:
        result = await session.execute(
            select(func.count()).where(
                Run.pipeline_id == pipeline_id,
                Run.status.in_(_ACTIVE_STATUSES),
            )
        )
        return result.scalar_one()

    async def _store_raw_payload(
        self,
        session: AsyncSession,
        *,
        trigger_event_id: uuid.UUID | None,
        raw_body: bytes,
        raw_payload: dict[str, Any],
        org_id: uuid.UUID,
    ) -> WebhookPayload:
        """Store raw payload for replay. Expires after the dedup TTL + 1 hour."""
        stored = WebhookPayload(
            organisation_id=org_id,
            trigger_event_id=trigger_event_id,
            raw_body=raw_body,
            raw_payload=raw_payload,
            expires_at=datetime.now(UTC) + timedelta(seconds=_DEDUP_TTL_SECONDS + 3600),
        )
        session.add(stored)
        await session.flush()
        return stored

    async def _try_insert_dedup(
        self,
        session: AsyncSession,
        trigger_id: uuid.UUID,
        org_id: uuid.UUID,
        payload_hash: str,
    ) -> bool:
        """Try to insert a dedup hash row. Return True if new, False if duplicate.

        Uses a savepoint so IntegrityError from a concurrent insert does not
        roll back the outer transaction.
        """
        now = datetime.now(UTC)

        existing = await session.execute(
            select(WebhookDedupHash).where(
                WebhookDedupHash.trigger_id == trigger_id,
                WebhookDedupHash.payload_hash == payload_hash,
                WebhookDedupHash.expires_at > now,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        await session.execute(
            delete(WebhookDedupHash).where(
                WebhookDedupHash.trigger_id == trigger_id,
                WebhookDedupHash.payload_hash == payload_hash,
                WebhookDedupHash.expires_at <= now,
            )
        )

        dedup = WebhookDedupHash(
            organisation_id=org_id,
            trigger_id=trigger_id,
            payload_hash=payload_hash,
            expires_at=now + timedelta(seconds=_DEDUP_TTL_SECONDS),
        )
        try:
            async with session.begin_nested():
                session.add(dedup)
                await session.flush()
        except IntegrityError:
            return False
        return True

    async def _log_event(
        self,
        session: AsyncSession,
        *,
        trigger: Trigger,
        org_id: uuid.UUID,
        payload_hash: str,
        result: str,
        run_id: uuid.UUID | None = None,
        error_detail: str | None = None,
    ) -> TriggerEvent:
        event = TriggerEvent(
            organisation_id=org_id,
            trigger_id=trigger.id,
            trigger_type="webhook",
            raw_payload_hash=payload_hash,
            validation_result=result,
            run_id=run_id,
            error_detail=error_detail,
        )
        session.add(event)
        await session.flush()
        return event
