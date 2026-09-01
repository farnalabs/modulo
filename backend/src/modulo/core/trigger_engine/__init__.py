"""TriggerEngine - webhook validation, deduplication, flood protection, and run creation.

Webhook processing pipeline:
  1. Load trigger config from DB (with FOR UPDATE lock)
  2. X-Modulo-Timestamp replay window check (±300s)
  3. HMAC-SHA256 validation over timestamp.body (if hmac_secret configured)
  4. Event filtering — accepted_events presence check + optional event_filters
     value check (dotted payload path → allowed values); a non-matching payload
     is logged, records an ``event_type_not_accepted`` TriggerEvent, and raises
     without creating a run
  5. Pre-trigger guardrail pass (FAR-214) — at the trigger boundary, BEFORE the
     dedup insert. A ``block``-action guardrail reject-and-retries: a
     ``guardrail_blocked`` TriggerEvent is recorded, the raw payload is stored
     for replay, and ``GuardrailBlockedAtIntakeError`` is raised (maps to a 4xx
     at the route boundary) — no run, no dedup slot consumed. A ``redact``-action
     guardrail applies its masks at intake so the payload that proceeds is
     post-redaction. ``warn``/``observe`` are advisory (logged, delivery
     proceeds). Replays re-run the pass detection-only.
  6. Deduplication (WebhookDedupHash - canonical POST-guardrail payload hash,
     5-min TTL)
  7. Flood protection (concurrent run count vs. trigger.max_concurrent_runs)
  8. Payload mapping (dot-notation path → input_payload key)
  9. Create Run + TriggerEvent in one transaction
 10. Dedup hash committed with run (single atomic unit)

All outcomes (pass and fail) are recorded as a TriggerEvent row, with three
exceptions documented here for accuracy:
  * Paused deliveries (org-wide kill-switch) do NOT write a TriggerEvent in the
    engine — the route's in-transaction catch is the single writer and commits
    a ``validation_result='paused'`` row.
  * Other failure events (hmac_failed, timestamp_expired, ...) ARE written by
    the engine, but the caller's transaction may roll them back when it maps
    the typed exception to an HTTP response (a documented pre-existing
    limitation, out of scope for the pause feature).
  * Guardrail-blocked deliveries (``guardrail_blocked``) are written by the
    engine and COMMIT: the route catches ``GuardrailBlockedAtIntakeError``
    inside its transaction (mirroring the paused pattern), so the event and the
    stored raw payload survive the 4xx response.
The caller is responsible for background execution of the created run.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.connectors.base import ConnectorQuery
from modulo.core.connector_hub.locking import _uuid_to_lock_keys as _uuid_to_lock_keys
from modulo.core.exceptions import RateLimitConflictError
from modulo.core.release_channels import (
    is_routable_channel,
    resolve_channel_binding,
)
from modulo.db.crud.pipeline_snapshot_versioning import (
    resolve_snapshot_for_channel,
)
from modulo.db.crud.run import create_run
from modulo.db.lifecycle_refs import _RESERVED_INPUT_PAYLOAD_KEYS
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.run import ACTIVE_RUN_STATUSES, Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.webhook import WebhookDedupHash, WebhookPayload
from modulo.db.rls import set_rls_execution_context
from modulo.db.settings_resolver import ensure_triggers_resumable
from modulo.db.unique_violation import is_unique_violation

_log = logging.getLogger(__name__)

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
    def __init__(self, detail: str = "X-Modulo-Timestamp is outside the ±300s replay window") -> None:
        super().__init__(detail)


class DuplicateWebhookError(RuntimeError):
    def __init__(self, payload_hash: str) -> None:
        super().__init__(f"Duplicate webhook payload: {payload_hash}")
        self.payload_hash = payload_hash


class ConcurrentRunLimitError(RuntimeError):
    def __init__(self, trigger_id: uuid.UUID, limit: int) -> None:
        super().__init__(f"Trigger {trigger_id} already has {limit} concurrent run(s); limit reached")
        self.trigger_id = trigger_id
        self.limit = limit


class PipelineRateLimitError(RuntimeError):
    def __init__(self, pipeline_id: uuid.UUID, key: str | None, max_triggers: int, window_seconds: int) -> None:
        super().__init__(
            f"Pipeline {pipeline_id} rate limit exceeded: {max_triggers} triggers per {window_seconds}s for key {key}"
        )
        self.pipeline_id = pipeline_id
        self.key = key
        self.max_triggers = max_triggers
        self.window_seconds = window_seconds


class ReplayNotFoundError(KeyError):
    def __init__(self, event_id: uuid.UUID) -> None:
        super().__init__(str(event_id))
        self.event_id = event_id


class TriggerBusyError(RuntimeError):
    """Raised when a concurrent dispatch for the same trigger is already in progress."""

    def __init__(self, trigger_id: uuid.UUID) -> None:
        super().__init__(f"Trigger {trigger_id} is busy - another dispatch is in progress")
        self.trigger_id = trigger_id


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEDUP_TTL_SECONDS = 300  # 5 minutes
_REPLAY_WINDOW_SECONDS = 300  # ±300s for X-Modulo-Timestamp
# Active (non-terminal) run statuses — single-sourced from the canonical set
# in db.models.run (the never-entered ``waiting_for_lock`` sub-state was
# excised in migration 0074/0075).
_ACTIVE_STATUSES = ACTIVE_RUN_STATUSES


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes | None) -> str:
    if not isinstance(data, bytes):
        return ""
    return hashlib.sha256(data).hexdigest()


# Backward-compatible private aliases — legacy callers and tests import the
# underscore names. New code should use the public names.
_sha256_hex = sha256_hex


# Unique-violation detection lives in the neutral low-level module
# (modulo.db.unique_violation) so this dedup path and create_run's admission
# path share identical semantics. Legacy underscore name kept for
# backward-compatible imports.
def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return True if *exc* is a unique-constraint violation (not FK, NOT NULL, etc.)."""
    return is_unique_violation(exc)


def verify_timestamp(modulo_timestamp: str | None) -> int:
    """Validate and return the Unix timestamp from the X-Modulo-Timestamp header.

    Raises TimestampExpiredError if the header is missing, malformed, or outside
    the ±300s replay window, with a distinct message per failure mode.
    """
    if modulo_timestamp is None:
        raise TimestampExpiredError("X-Modulo-Timestamp header is missing")
    try:
        ts = int(modulo_timestamp)
    except (ValueError, TypeError):
        raise TimestampExpiredError(f"X-Modulo-Timestamp is not a valid integer: {modulo_timestamp!r}") from None
    now = time.time()
    if abs(now - ts) > _REPLAY_WINDOW_SECONDS:
        raise TimestampExpiredError("X-Modulo-Timestamp is outside the ±300s replay window")
    return ts


def verify_hmac(raw_body: bytes, secret: str, signature_header: str | None, timestamp: int | None = None) -> bool:
    """Return True if the HMAC-SHA256 signature matches ``timestamp.body``.

    When *timestamp* is provided, the HMAC is computed over
    ``f"{timestamp}.{raw_body}"`` (as UTF-8). If *timestamp* is None,
    falls back to body-only signing for backward compatibility.
    """
    if signature_header is None:
        return False
    payload = f"{timestamp}.".encode() + raw_body if timestamp is not None else raw_body
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# Backward-compatible private aliases — legacy callers and tests import the
# underscore names. New code should use the public names.
_verify_timestamp = verify_timestamp
_verify_hmac = verify_hmac


def _extract_field(payload: dict[str, Any], path: str) -> Any:
    """Extract a value from a nested dict using dot notation (e.g. 'pull_request.head.sha')."""
    parts = path.split(".")
    value: Any = payload
    for part in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches_event_filters(raw_payload: dict[str, Any], event_filters: Any) -> bool:
    """Return True when every configured dotted-path value filter matches.

    ``event_filters`` maps a dotted payload path (e.g. ``"review.state"``) to a
    list of allowed values. A missing key, a non-dict intermediate node, a
    resolved value absent from the allowlist, or a malformed (non-dict or
    non-list) filter config rejects the event (returns False) — value filters
    fail closed. Comparison uses the existing ``_extract_field`` dot-notation
    walk so filtering shares the payload-mapping path semantics.
    """
    if not isinstance(event_filters, dict):
        return False
    for path, allowed_values in event_filters.items():
        if not isinstance(allowed_values, (list, tuple, set)):
            return False
        value = _extract_field(raw_payload, path)
        if value not in allowed_values:
            return False
    return True


def _apply_payload_mapping(raw_payload: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Map raw webhook payload fields to input_payload using dot-notation paths.

    If mapping is empty, the raw payload is used as-is.

    Reserved input-payload keys (see ``_RESERVED_INPUT_PAYLOAD_KEYS``) can never
    be a mapping TARGET — a mapping that tries to write to a reserved key is a
    misconfiguration and is rejected with ``ValueError`` so a trigger cannot
    forge ``_work_item_id`` / ``_modulo.work_item`` / ``_feedback_correction``.
    """
    if not mapping:
        return dict(raw_payload)
    reserved = [k for k in mapping if k in _RESERVED_INPUT_PAYLOAD_KEYS]
    if reserved:
        raise ValueError(f"payload_mapping target key(s) {reserved} are reserved and cannot be mapped")
    return {target_key: _extract_field(raw_payload, src_path) for target_key, src_path in mapping.items()}


def _extract_work_item_refs(payload: dict[str, Any], ref_paths: Any) -> list[dict[str, Any]] | None:
    """Extract raw work-item refs from the (mapped) payload via dot-notation.

    ``ref_paths`` is a trigger-config list of ``{"kind": ..., "path": ...}``
    entries (mirrors ``payload_mapping``'s dot-notation via ``_extract_field``).
    Returns a list of raw ``{kind, ref, source: "derived"}`` entries
    (canonicalised + validated inside ``create_run``), or ``None`` when nothing
    is configured / found.
    """
    if not isinstance(ref_paths, list):
        return None
    entries: list[dict[str, Any]] = []
    for item in ref_paths:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        path = item.get("path")
        if not kind or not path:
            continue
        value = _extract_field(payload, path)
        if value is None or not str(value).strip():
            continue
        entries.append({"kind": str(kind), "ref": str(value), "source": "derived"})
    return entries or None


# ---------------------------------------------------------------------------
# Delivery context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _WebhookDelivery:
    """Immutable identity + payload state of a webhook delivery.

    Carries the delivery's identity and payload across the webhook / replay
    pipeline steps so step helpers take the context instead of long positional
    argument lists. ``trigger`` is the row loaded under the advisory lock.
    """

    org_id: uuid.UUID
    trigger: Trigger
    raw_body: bytes
    raw_payload: dict[str, Any]
    snapshot_id: uuid.UUID


@dataclass(frozen=True)
class _RateLimitState:
    """Resolved rate-limit state for a delivery.

    ``key`` is ``None`` when the pipeline has no rate-limit budget configured;
    ``max_triggers`` / ``window_seconds`` are populated only when one is set.
    """

    key: str | None
    max_triggers: int = 0
    window_seconds: int = 0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TriggerEngine:
    """Stateless service - pass a session per call."""

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
        A block-action guardrail rejection raises ``GuardrailBlockedAtIntakeError``
        (a ``guardrail_blocked`` TriggerEvent + stored raw payload are written
        first — the caller must keep the transaction so they survive the 4xx).
        The caller must have already set RLS context on the session.
        """
        key1, key2 = _uuid_to_lock_keys(trigger_id)
        lock_result = await session.execute(
            text("SELECT pg_try_advisory_lock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not lock_result.scalar_one():
            raise TriggerBusyError(trigger_id)
        try:
            trigger = await self._load_trigger(session, trigger_id, org_id)
            delivery = _WebhookDelivery(
                org_id=org_id,
                trigger=trigger,
                raw_body=raw_body,
                raw_payload=raw_payload,
                snapshot_id=snapshot_id,
            )
            # Pre-guardrail failure events (timestamp, HMAC, event filters) are
            # about the RAW delivery — they record the raw-body hash. The DEDUP
            # hash is computed after the pre-trigger guardrail pass below
            # (canonical POST-guardrail payload hash, FAR-214).
            payload_hash = sha256_hex(raw_body)

            # X-Modulo-Timestamp replay window check
            try:
                ts = verify_timestamp(modulo_timestamp)
            except TimestampExpiredError as exc:
                _log.warning("Webhook timestamp validation failed for trigger %s: %s", trigger_id, exc, exc_info=True)
                await self._log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    payload_hash=payload_hash,
                    result="timestamp_expired",
                )
                raise

            # HMAC validation (only if secret is configured)
            cfg = trigger.config_json or {}
            hmac_secret_raw: str | None = cfg.get("hmac_secret")
            hmac_secret: str | None = None
            if hmac_secret_raw is not None:
                try:
                    from modulo.settings import get_settings as _get_settings

                    hmac_secret = await decode_stored_secret_scoped(
                        session, hmac_secret_raw, _get_settings().fernet_key, org_id=org_id
                    )
                except Exception:
                    _log.exception("trigger_engine.hmac_secret_decrypt_failed trigger=%s", trigger_id)
                    hmac_secret = hmac_secret_raw
            if hmac_secret is not None and not verify_hmac(raw_body, hmac_secret, hmac_signature, timestamp=ts):
                _log.warning("Webhook HMAC validation failed for trigger %s", trigger_id)
                await self._log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    payload_hash=payload_hash,
                    result="hmac_failed",
                )
                raise HmacValidationError

            # Org-wide pause kill-switch. Checked AFTER timestamp+HMAC
            # validation (an unauthenticated delivery still gets its typed
            # error) but BEFORE the dedup insert — a paused delivery must not
            # consume a dedup slot. The engine writes NO TriggerEvent here; the
            # route's in-transaction catch is the single writer of the
            # ``paused`` event. Read failures propagate (never fabricate).
            await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type="webhook")

            # Event type + value filtering — skip the delivery unless it
            # satisfies the trigger's accepted-events presence check and any
            # configured dotted-path value filters.
            await self._enforce_event_acceptance(
                session,
                delivery,
                payload_hash=payload_hash,
                log_prefix="Webhook",
                payload_subject="webhook",
                use_dot_notation=False,
            )

            # Pre-trigger guardrail pass (FAR-214) — at the trigger boundary,
            # BEFORE the dedup insert so a guardrail-blocked delivery never
            # consumes a dedup slot. Reuses the T1 run-creation seam's engine
            # and row-loading semantics; detection is never reimplemented.
            post_guardrail_payload, dedup_hash = await self._run_pre_trigger_guardrail(
                session,
                delivery,
                payload_hash=payload_hash,
                is_replay=False,
            )

            # Deduplication
            is_new = await self._try_insert_dedup(session, trigger_id, org_id, dedup_hash)
            if not is_new:
                _log.warning("Webhook deduplicated for trigger %s (hash=%s)", trigger_id, dedup_hash[:16])
                await self._log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    payload_hash=dedup_hash,
                    result="deduplicated",
                )
                raise DuplicateWebhookError(dedup_hash)

            # Flood / concurrency protection — accept and queue instead of rejecting.
            # The run is created as pending and the executor queues it via
            # _check_capacity / _retry_pending, so webhooks never get 429s.
            active_count = await self._count_active_runs(session, trigger.id)
            if active_count >= trigger.max_concurrent_runs:
                _log.warning(
                    "Webhook concurrency limit reached for trigger %s (%d active >= %d limit) — queuing anyway",
                    trigger_id,
                    active_count,
                    trigger.max_concurrent_runs,
                )
                await self._log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    payload_hash=dedup_hash,
                    result="concurrency_limit_reached",
                )

            # Payload mapping — derived from the POST-guardrail payload so a
            # redact-action guardrail's masks are reflected in the mapped input.
            mapping: dict[str, str] = cfg.get("payload_mapping", {})
            input_payload = _apply_payload_mapping(post_guardrail_payload, mapping)

            # Rate limit check
            rate_limit = await self._resolve_rate_limit_state(
                session,
                delivery,
                input_payload=input_payload,
                payload_hash=dedup_hash,
            )

            # Create run + audit + store payload for replay
            run, trigger_event = await self._create_webhook_run(
                session,
                delivery,
                input_payload=input_payload,
                payload_hash=dedup_hash,
                rate_limit=rate_limit,
            )
            _log.info("Webhook accepted for trigger %s → run %s", trigger_id, run.id)

            return run, trigger_event, input_payload
        finally:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key1, :key2)"),
                {"key1": key1, "key2": key2},
            )

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

        # Load trigger with advisory lock
        key1, key2 = _uuid_to_lock_keys(event.trigger_id)
        lock_result = await session.execute(
            text("SELECT pg_try_advisory_lock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not lock_result.scalar_one():
            raise TriggerBusyError(event.trigger_id)
        try:
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

            # Org-wide pause kill-switch — a replay is a trigger-initiated run
            # and must honour the pause. No event write here; the route's
            # in-transaction catch writes the ``paused`` event.
            await ensure_triggers_resumable(session, org_id, trigger_id=event.trigger_id, trigger_type="webhook")

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

            # Run the rest of the pipeline (skip HMAC + timestamp validation).
            # Pre-guardrail failure events below record the raw-body hash; the
            # canonical POST-guardrail hash is computed after the detection-only
            # guardrail pass for the downstream (post-guardrail) events.
            raw_body_hash = sha256_hex(raw_body)

            delivery = _WebhookDelivery(
                org_id=org_id,
                trigger=trigger,
                raw_body=raw_body,
                raw_payload=raw_payload,
                snapshot_id=snapshot_id,
            )
            cfg = trigger.config_json or {}

            # No dedup check for replays - this is an intentional re-fire.
            # The original event already went through dedup validation.

            # Event type + value filtering — mirror the handle_webhook gate so a
            # re-fired event is held to the same filters as the original delivery.
            await self._enforce_event_acceptance(
                session,
                delivery,
                payload_hash=raw_body_hash,
                log_prefix="Replay",
                payload_subject="replayed webhook",
                use_dot_notation=True,
            )

            # Pre-trigger guardrail pass on replay — re-runs the pass
            # DETECTION-ONLY (consistent with the run-creation seam's
            # ``is_replay=True`` handling): no block decision, no redaction act.
            # A replay bypasses dedup but must still be guardrail-checked (the
            # payload may have been fixed since the original delivery). The
            # canonical POST-guardrail hash feeds the post-guardrail events.
            post_guardrail_payload, payload_hash = await self._run_pre_trigger_guardrail(
                session,
                delivery,
                payload_hash=raw_body_hash,
                is_replay=True,
            )

            # Flood protection
            active_count = await self._count_active_runs(session, trigger.id)
            if active_count >= trigger.max_concurrent_runs:
                await self._log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    payload_hash=payload_hash,
                    result="concurrency_limit_reached",
                )
                raise ConcurrentRunLimitError(trigger.id, trigger.max_concurrent_runs)

            # Payload mapping — derived from the POST-guardrail payload (a
            # detection-only replay leaves the payload unchanged, but the
            # mapping source is kept consistent with handle_webhook).
            mapping: dict[str, str] = cfg.get("payload_mapping", {})
            input_payload = _apply_payload_mapping(post_guardrail_payload, mapping)

            # Rate limit check
            rate_limit = await self._resolve_rate_limit_state(
                session,
                delivery,
                input_payload=input_payload,
                payload_hash=payload_hash,
            )

            # Create run (a replay is flagged via is_replay so downstream
            # consumers can distinguish re-fires from original deliveries).
            run, trigger_event = await self._create_webhook_run(
                session,
                delivery,
                input_payload=input_payload,
                payload_hash=payload_hash,
                rate_limit=rate_limit,
                is_replay=True,
            )

            return run, trigger_event, input_payload
        finally:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key1, :key2)"),
                {"key1": key1, "key2": key2},
            )

    # ------------------------------------------------------------------
    # Polling trigger
    # ------------------------------------------------------------------

    async def schedule_polling_trigger(
        self,
        session: AsyncSession,
        *,
        trigger: Trigger,
        _org_id: uuid.UUID,
    ) -> None:
        """Register/update a polling trigger's scheduled next fire.

        Computes ``next_fire_at`` from ``poll_interval_seconds`` and persists it
        on the trigger row. ``fire_due_triggers`` (system cron) picks it up on
        the next tick.
        """
        config = trigger.config_json or {}
        raw_interval = config.get("poll_interval_seconds")
        if raw_interval is None:
            raw_interval = 60
        elif not isinstance(raw_interval, (int, float)) or raw_interval < 1:
            raise ValueError(f"poll_interval_seconds must be >= 1, got {raw_interval!r}")
        interval = max(int(raw_interval), 1)
        now = datetime.now(UTC)
        trigger.next_fire_at = now + timedelta(seconds=interval)
        await session.flush()

    @staticmethod
    async def evaluate_condition(
        session: AsyncSession,
        *,
        _trigger: Trigger,
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
          - ``fail_closed``: ``True`` only on a shared-rate-budget outage (FAR-442)

        This is a sync-friendly evaluation meant for testing or manual one-off
        checks. For automatic scheduled evaluation use the SAQ fire job path.
        """
        from modulo.connectors._rate_bucket import SharedBudgetUnavailableError
        from modulo.core.trigger_engine.polling import (
            _build_polling_connector_from_instance,
            _close_polling_resources,
        )
        from modulo.core.trigger_engine.polling import (
            evaluate_condition as _evaluate_condition,
        )

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

        connector = None
        redis_client = None
        try:
            try:
                connector, redis_client = await _build_polling_connector_from_instance(session, instance, org_id)
            except asyncio.CancelledError:
                raise
            except SharedBudgetUnavailableError as exc:
                # Fail-closed (FAR-442): a configured-but-unresolvable shared rate
                # budget must not be downgraded to the per-process local bucket.
                # Return the error contract dict rather than raising — this is a
                # diagnostics path, and a raised failure would surface as a generic
                # 500 instead of an explicit, actionable budget error. The connector
                # is closed in the finally below.
                return {
                    "status": "error",
                    "error": f"shared rate-limit budget unavailable: {exc}",
                    "fail_closed": True,
                }
            except Exception as exc:
                return {"status": "error", "error": f"Connector init failed: {str(exc)[:200]}"}

            try:
                query = ConnectorQuery(resource=poll_query)
                query_result = await connector.query(query)
            except asyncio.CancelledError:
                raise
            except SharedBudgetUnavailableError as exc:
                # Fail-closed (FAR-442): the shared Redis rate budget is configured
                # but could not be charged during the query. Return the error dict
                # (the request was never sent on an unaccountable budget).
                return {
                    "status": "error",
                    "error": f"shared rate-limit budget unavailable: {exc}",
                    "fail_closed": True,
                }
            except Exception as exc:
                return {"status": "error", "error": f"Query failed: {str(exc)[:200]}"}

            try:
                matched = _evaluate_condition(query_result, condition_expression)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {"status": "error", "error": f"Condition evaluation failed: {str(exc)[:200]}"}

            return {
                "status": "condition_met" if matched else "no_match",
                "records": query_result.records,
                "total": query_result.total,
            }
        finally:
            # The connector + shared Redis client are fresh builds (FAR-442) — the
            # caller owns both and must release them regardless of outcome.
            await _close_polling_resources(connector, redis_client)

    # ------------------------------------------------------------------
    # Dedup cleanup
    # ------------------------------------------------------------------

    @staticmethod
    async def cleanup_expired_dedup_hashes(session: AsyncSession) -> int:
        """Delete expired webhook_dedup_hashes rows.

        Acquires a Postgres advisory lock (key=20250601) on PostgreSQL to prevent
        concurrent cleanup across workers. On other backends the lock is skipped.
        Returns the number of deleted rows.
        """
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
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

        await session.execute(delete(WebhookPayload).where(WebhookPayload.id.in_(expired_ids)))
        return len(expired_ids)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_rate_limit_key(input_payload: dict[str, Any], config: dict[str, Any]) -> str:
        """Extract key_fields from input_payload and serialize as sorted JSON.

        With match_mode="exact", the values of each field become part of the key.
        With match_mode="presence", only the presence (non-null) of each field matters.
        """
        key_fields: list[str] = config.get("key_fields", [])
        match_mode: str = config.get("match_mode", "exact")
        extracted: dict[str, Any] = {}
        for field_path in key_fields:
            value = _extract_field(input_payload, field_path)
            if match_mode == "presence":
                extracted[field_path] = "__present__" if value is not None else None
            else:
                extracted[field_path] = value
        return json.dumps(extracted, sort_keys=True)

    @staticmethod
    async def _count_recent_rate_limited(
        session: AsyncSession,
        pipeline_id: uuid.UUID,
        key: str,
        window_seconds: int,
    ) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
        result = await session.execute(
            select(func.count()).where(
                Run.pipeline_id == pipeline_id,
                Run.rate_limit_key == key,
                Run.created_at > cutoff,
            )
        )
        return int(result.scalar_one() or 0)

    async def _load_trigger(
        self,
        session: AsyncSession,
        trigger_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> Trigger:
        """Load trigger row (no FOR UPDATE - caller holds advisory lock if needed)."""
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == org_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise TriggerNotFoundError(trigger_id)
        if not trigger.active:
            raise TriggerInactiveError(trigger_id)
        return trigger

    async def _count_active_runs(self, session: AsyncSession, trigger_id: uuid.UUID) -> int:
        result = await session.execute(
            select(func.count()).where(
                Run.trigger_id == trigger_id,
                Run.status.in_(_ACTIVE_STATUSES),
                Run.cancellation_requested.is_(False),
            )
        )
        return int(result.scalar_one() or 0)

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
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                return False
            raise
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
        trigger_type: str | None = None,
    ) -> TriggerEvent:
        event = TriggerEvent(
            organisation_id=org_id,
            trigger_id=trigger.id,
            trigger_type=trigger_type or trigger.trigger_type,
            raw_payload_hash=payload_hash,
            validation_result=result,
            run_id=run_id,
            error_detail=error_detail,
        )
        session.add(event)
        await session.flush()
        return event

    async def _enforce_event_acceptance(
        self,
        session: AsyncSession,
        delivery: _WebhookDelivery,
        *,
        payload_hash: str,
        log_prefix: str,
        payload_subject: str,
        use_dot_notation: bool,
    ) -> None:
        """Enforce the accepted-events and value-filter gates; reject on mismatch.

        Records an ``event_type_not_accepted`` TriggerEvent and raises
        ``RuntimeError`` when the payload does not satisfy the trigger's event
        acceptance config. *log_prefix* / *payload_subject* adapt the log and
        error wording between webhook and replay delivery; *use_dot_notation*
        preserves the historical accepted-events lookup (top-level ``.get`` for
        webhooks vs dotted-path ``_extract_field`` for replays).
        """
        cfg = delivery.trigger.config_json or {}
        accepted_events: list[str] | None = cfg.get("accepted_events")
        if accepted_events:
            if use_dot_notation:
                has_accepted_event = any(
                    isinstance(_extract_field(delivery.raw_payload, event), dict) for event in accepted_events
                )
            else:
                has_accepted_event = any(isinstance(delivery.raw_payload.get(event), dict) for event in accepted_events)
            if not has_accepted_event:
                _log.info(
                    "%s event type not accepted for trigger %s (accepted=%s, payload_keys=%s)",
                    log_prefix,
                    delivery.trigger.id,
                    accepted_events,
                    list(delivery.raw_payload.keys()),
                )
                await self._log_event(
                    session,
                    trigger=delivery.trigger,
                    org_id=delivery.org_id,
                    payload_hash=payload_hash,
                    result="event_type_not_accepted",
                )
                raise RuntimeError(
                    f"Trigger {delivery.trigger.id}: none of the accepted event types {accepted_events} "
                    f"found in {payload_subject} payload (keys: {list(delivery.raw_payload.keys())})"
                )

        event_filters = cfg.get("event_filters")
        if event_filters and not _matches_event_filters(delivery.raw_payload, event_filters):
            _log.info(
                "%s event value filter not accepted for trigger %s (event_filters=%s, payload_keys=%s)",
                log_prefix,
                delivery.trigger.id,
                event_filters,
                list(delivery.raw_payload.keys()),
            )
            await self._log_event(
                session,
                trigger=delivery.trigger,
                org_id=delivery.org_id,
                payload_hash=payload_hash,
                result="event_type_not_accepted",
            )
            raise RuntimeError(
                f"Trigger {delivery.trigger.id}: event value filters {event_filters} "
                f"not satisfied by {payload_subject} payload (keys: {list(delivery.raw_payload.keys())})"
            )

    async def _run_pre_trigger_guardrail(
        self,
        session: AsyncSession,
        delivery: _WebhookDelivery,
        *,
        payload_hash: str,
        is_replay: bool,
    ) -> tuple[dict[str, Any], str]:
        """Run the pre-trigger guardrail pass; return (post-guardrail payload, canonical hash).

        A block outcome (webhook path only — replays are detection-only and
        never block) records a ``guardrail_blocked`` TriggerEvent, stores the
        raw payload for replay, and raises ``GuardrailBlockedAtIntakeError``.
        The canonical POST-guardrail payload hash is the dedup key (FAR-214).
        """
        from modulo.core.trigger_engine.pre_guardrail import (
            GuardrailBlockedAtIntakeError,
            canonical_payload_hash,
            run_pre_trigger_guardrail_pass,
        )

        outcome = await run_pre_trigger_guardrail_pass(
            session,
            org_id=delivery.org_id,
            pipeline_id=delivery.trigger.pipeline_id,
            raw_payload=delivery.raw_payload,
            detection_only=is_replay,
        )
        if outcome.blocked:
            block_event = await self._log_event(
                session,
                trigger=delivery.trigger,
                org_id=delivery.org_id,
                payload_hash=payload_hash,
                result="guardrail_blocked",
                error_detail=outcome.block_message[:2000],
            )
            # Store the raw payload for replay so the sender/provider can
            # retry after fixing — the delivery is reject-and-retry, NOT
            # acked-as-accepted.
            await self._store_raw_payload(
                session,
                trigger_event_id=block_event.id,
                raw_body=delivery.raw_body,
                raw_payload=delivery.raw_payload,
                org_id=delivery.org_id,
            )
            raise GuardrailBlockedAtIntakeError(
                outcome.block_message,
                guardrail_name=outcome.blocking_eval_name,
            )
        if is_replay:
            _log.info(
                "guardrails.pre_trigger replay detection evaluated=%d for trigger %s pipeline %s",
                outcome.evaluated_count,
                delivery.trigger.id,
                delivery.trigger.pipeline_id,
            )
        else:
            _log.info(
                "guardrails.pre_trigger evaluated=%d redactions=%d for trigger %s pipeline %s",
                outcome.evaluated_count,
                len(outcome.redactions),
                delivery.trigger.id,
                delivery.trigger.pipeline_id,
            )
        return outcome.payload, canonical_payload_hash(outcome.payload)

    async def _resolve_rate_limit_state(
        self,
        session: AsyncSession,
        delivery: _WebhookDelivery,
        *,
        input_payload: dict[str, Any],
        payload_hash: str,
    ) -> _RateLimitState:
        """Resolve the pipeline rate limit and raise when the budget is exhausted.

        The rate-limit config comes from the trigger's own ``rate_limit`` config
        (falling back to the pipeline's ``rate_limit_config`` when unset). When
        no budget is configured the returned state carries ``key=None``.
        """
        cfg = delivery.trigger.config_json or {}
        pipeline_rate_limit = cfg.get("rate_limit")
        if pipeline_rate_limit is None:
            from modulo.db.models.pipeline import Pipeline

            # Org-only context (webhook automation has no user principal) — the
            # execution hatch lets the rate-limit fallback read a team-private
            # pipeline's config.
            await set_rls_execution_context(session)
            pipe_result = await session.execute(select(Pipeline).where(Pipeline.id == delivery.trigger.pipeline_id))
            pipeline = pipe_result.scalar_one_or_none()
            if pipeline is not None:
                pipeline_rate_limit = pipeline.rate_limit_config

        if not (pipeline_rate_limit and pipeline_rate_limit.get("max_triggers")):
            return _RateLimitState(key=None)

        max_triggers = int(pipeline_rate_limit["max_triggers"])
        window_seconds = int(pipeline_rate_limit.get("window_seconds", 3600))
        rate_limit_key = self._compute_rate_limit_key(input_payload, pipeline_rate_limit)
        recent_count = await self._count_recent_rate_limited(
            session, delivery.trigger.pipeline_id, rate_limit_key, window_seconds
        )
        if recent_count >= max_triggers:
            _log.warning(
                "Rate limit exceeded for pipeline %s: %d >= %d for key %s",
                delivery.trigger.pipeline_id,
                recent_count,
                max_triggers,
                rate_limit_key,
            )
            await self._log_event(
                session,
                trigger=delivery.trigger,
                org_id=delivery.org_id,
                payload_hash=payload_hash,
                result="rate_limited",
            )
            raise PipelineRateLimitError(delivery.trigger.pipeline_id, rate_limit_key, max_triggers, window_seconds)
        return _RateLimitState(key=rate_limit_key, max_triggers=max_triggers, window_seconds=window_seconds)

    async def resolve_snapshot_id_for_trigger(
        self,
        session: AsyncSession,
        *,
        trigger: Trigger,
    ) -> uuid.UUID | None:
        """Resolve a trigger's channel binding to a concrete snapshot id.

        FAR-402 P6 release-channel hook: a trigger whose ``config_json`` binds a
        ``release_channel`` (``stable``/``canary``) resolves to the LATEST
        snapshot created under that channel so it executes the newest channel
        version rather than the live graph. An unbound trigger (``none``) or a
        channel with no snapshot returns ``None`` — the caller then pins the live
        graph, preserving current behaviour. The hook is deliberately minimal
        (a resolver, not a promotion/rollback controller).
        """
        if trigger is None:
            return None
        channel = resolve_channel_binding(trigger.config_json)
        if not is_routable_channel(channel):
            return None
        snapshot = await resolve_snapshot_for_channel(
            session,
            pipeline_id=trigger.pipeline_id,
            channel=channel,
            organisation_id=trigger.organisation_id,
        )
        return snapshot.id if snapshot is not None else None

    async def _create_webhook_run(
        self,
        session: AsyncSession,
        delivery: _WebhookDelivery,
        *,
        input_payload: dict[str, Any],
        payload_hash: str,
        rate_limit: _RateLimitState,
        is_replay: bool | None = None,
    ) -> tuple[Run, TriggerEvent]:
        """Create the run for a webhook/replay delivery and audit + store payload.

        Maps a ``RateLimitConflictError`` from ``create_run`` onto
        ``PipelineRateLimitError`` (recording a ``rate_limited`` event first),
        then records the ``accepted`` event and stores the raw payload for
        replay. ``is_replay`` is forwarded verbatim so downstream consumers can
        distinguish re-fires from original deliveries.
        """
        cfg = delivery.trigger.config_json or {}
        refs = _extract_work_item_refs(input_payload, cfg.get("work_item_ref_paths"))
        try:
            run = await create_run(
                session,
                org_id=delivery.org_id,
                pipeline_id=delivery.trigger.pipeline_id,
                snapshot_id=delivery.snapshot_id,
                trigger_type="webhook",
                input_payload=input_payload,
                trigger_id=delivery.trigger.id,
                rate_limit_key=rate_limit.key,
                work_item_refs=refs,
                is_replay=is_replay,
            )
        except RateLimitConflictError as exc:
            _log.warning(
                "Rate limit conflict for pipeline %s: %s",
                delivery.trigger.pipeline_id,
                exc.rate_limit_key,
            )
            await self._log_event(
                session,
                trigger=delivery.trigger,
                org_id=delivery.org_id,
                payload_hash=payload_hash,
                result="rate_limited",
            )
            raise PipelineRateLimitError(
                delivery.trigger.pipeline_id,
                exc.rate_limit_key,
                rate_limit.max_triggers,
                rate_limit.window_seconds,
            ) from exc

        # Audit log + store raw payload for replay (re-replay support)
        trigger_event = await self._log_event(
            session,
            trigger=delivery.trigger,
            org_id=delivery.org_id,
            payload_hash=payload_hash,
            result="accepted",
            run_id=run.id,
        )
        await self._store_raw_payload(
            session,
            trigger_event_id=trigger_event.id,
            raw_body=delivery.raw_body,
            raw_payload=delivery.raw_payload,
            org_id=delivery.org_id,
        )
        return run, trigger_event


# ---------------------------------------------------------------------------
# Dependent-trigger suppression for guardrail-blocked runs (FAR-213)
# ---------------------------------------------------------------------------


async def is_guardrail_blocked_run(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """True when *run_id* is a guardrail-blocked terminal run.

    A guardrail block is terminal ``eval_failed`` with ``error_code``
    ``eval_blocked``. Dependent triggers (e.g. agent_signal children fired on a
    source node's completion) must never fire as a consequence of such a run —
    its side effects are compensated, not published. Read-only; org-scoped via
    the caller's RLS context.
    """
    result = await session.execute(
        select(func.count()).where(
            Run.id == run_id,
            Run.status == "eval_failed",
            Run.error_code == "eval_blocked",
        )
    )
    return int(result.scalar_one() or 0) > 0


async def record_dependent_suppressed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    trigger_count: int,
) -> None:
    """Audit a dependent-trigger suppression (best-effort, guard-the-guard).

    Summary-only payload — never raw run/trigger content.
    """
    try:
        from modulo.core.audit_logger import append_audit_event

        await append_audit_event(
            session,
            org_id=org_id,
            event_type="guardrail.dependent_suppressed",
            resource_type="run",
            resource_id=run_id,
            payload_json={"trigger_count": trigger_count, "reason": "source_run_guardrail_blocked"},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("trigger_engine.dependent_suppressed_audit_failed run=%s", run_id)
