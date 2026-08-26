"""Slack ``app_mention`` trigger — fire a pipeline when a bot is @-mentioned.

Receives Slack Events API ``app_mention`` payloads, verifies the Slack signing
secret (Slack Signs your requests: ``X-Slack-Signature`` is a HMAC-SHA256 over
``v0:<timestamp>:<raw_body>`` with the signing secret, compared in constant
time), parses the mention fields, dedupes by Slack ``event_id``, and maps the
mention into pipeline input via the existing ``payload_mapping`` mechanism.

Trigger ``config_json`` structure::

    {
        "signing_secret": "<slack app signing secret>",  # required (masked in API responses)
        "payload_mapping": {                              # optional, dot-notation
            "text": "text",
            "channel": "channel",
            "thread_ts": "thread_ts",
            "user": "user",
        },
    }

Processing pipeline (mirrors ``TriggerEngine.handle_webhook``):

  1. Load trigger config (with advisory lock)
  2. ``X-Slack-Request-Timestamp`` replay window check (±300s)
  3. HMAC-SHA256 validation over ``v0:<timestamp>:<body>`` (constant-time)
  4. Parse the Events API envelope (``event.type == 'app_mention'``)
  5. Deduplication by Slack ``event_id`` (reuses the ``WebhookDedupHash`` table)
  6. Flood protection (concurrent run count vs. trigger.max_concurrent_runs)
  7. Payload mapping (dot-notation path -> input_payload key)
  8. Create Run + TriggerEvent in one transaction
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import decode_stored_secret
from modulo.core.exceptions import RateLimitConflictError
from modulo.core.trigger_engine import (
    DuplicateWebhookError,
    PipelineRateLimitError,
    TriggerBusyError,
    TriggerEngine,
    _apply_payload_mapping,
    _extract_work_item_refs,
    _uuid_to_lock_keys,
    verify_timestamp,
)
from modulo.db.crud.run import create_run
from modulo.db.models.run import Run
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.webhook import WebhookPayload
from modulo.db.rls import set_rls_execution_context
from modulo.db.settings_resolver import ensure_triggers_resumable

_log = logging.getLogger(__name__)

_SLACK_SIGNATURE_VERSION = "v0"
_DEDUP_TTL_SECONDS = 300  # 5 minutes — Slack redelivers undelivered events within ~1h
_MAX_RATE_LIMIT_WINDOW_FALLBACK = 3600


class SlackSignatureError(PermissionError):
    """Raised when the Slack ``X-Slack-Signature`` is missing or invalid."""


class SlackTimestampExpiredError(PermissionError):
    """Raised when ``X-Slack-Request-Timestamp`` is outside the replay window."""


class SlackEventTypeError(ValueError):
    """Raised when the payload envelope is not a supported event (e.g. not app_mention)."""


class SlackAppMentionParseError(ValueError):
    """Raised when the app_mention payload is malformed or missing required fields."""


class SlackChallengeNotFoundError(ValueError):
    """Raised when a ``url_verification`` payload lacks the ``challenge`` field."""


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_slack_signature(
    raw_body: bytes,
    signing_secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
) -> bool:
    """Return True if the ``X-Slack-Signature`` header matches ``v0:<ts>:<body>``.

    Slack Signs your requests: the expected signature is
    ``v0=<HMAC-SHA256(signing_secret, f"v0:{timestamp}:{body}")>``. The
    comparison uses ``hmac.compare_digest`` (constant-time). Returns False
    (never raises) when any input is missing/malformed so callers can map the
    failure to a 401.
    """
    if not signing_secret or not timestamp_header or not signature_header:
        return False
    try:
        ts = str(timestamp_header)
    except (ValueError, TypeError):
        return False
    base = f"{_SLACK_SIGNATURE_VERSION}:{ts}:".encode() + raw_body
    expected = f"{_SLACK_SIGNATURE_VERSION}=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def verify_slack_timestamp(timestamp_header: str | None) -> int:
    """Validate and return the Unix timestamp from ``X-Slack-Request-Timestamp``.

    Slack recommends rejecting requests older than ~5 minutes. Reuses the
    webhook engine's ±300s replay-window check; raises
    ``SlackTimestampExpiredError`` on a missing/malformed/stale timestamp.
    """
    try:
        return verify_timestamp(timestamp_header)
    except Exception as exc:
        raise SlackTimestampExpiredError(
            f"X-Slack-Request-Timestamp is missing or outside the replay window: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def parse_app_mention_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Parse a Slack Events API envelope and return the app_mention fields.

    Expects the top-level ``type == 'event_callback'`` with
    ``event.type == 'app_mention'``. Returns a dict with the fields mapped
    into pipeline input::

        {
            "event_id": str,
            "team_id": str | None,
            "type": "app_mention",
            "text": str,
            "channel": str,
            "thread_ts": str | None,
            "user": str,
            "ts": str,
            "bot_id": str | None,
        }

    Raises ``SlackEventTypeError`` when the envelope/event type is not
    ``app_mention`` and ``SlackAppMentionParseError`` when required fields
    (``event_id``) are missing.
    """
    if not isinstance(raw_payload, dict):
        raise SlackAppMentionParseError("Payload must be a JSON object")

    top_type = raw_payload.get("type")
    if top_type == "url_verification":
        raise SlackChallengeNotFoundError("URL verification payload received — handle the challenge at the route layer")
    if top_type not in (None, "event_callback"):
        raise SlackEventTypeError(f"Unsupported Slack event envelope type: {top_type!r} (expected 'event_callback')")

    event = raw_payload.get("event")
    if not isinstance(event, dict):
        raise SlackAppMentionParseError("Payload is missing the 'event' object")

    event_type = event.get("type")
    if event_type != "app_mention":
        raise SlackEventTypeError(f"Event type {event_type!r} is not 'app_mention' — not a trigger event")

    event_id = raw_payload.get("event_id") or event.get("event_id")
    if not event_id:
        raise SlackAppMentionParseError("Payload is missing 'event_id'")

    team_id = raw_payload.get("team_id")
    return {
        "event_id": str(event_id),
        "team_id": str(team_id) if team_id is not None else None,
        "type": "app_mention",
        "text": event.get("text", "") if isinstance(event.get("text"), str) else "",
        "channel": event.get("channel"),
        "thread_ts": event.get("thread_ts"),
        "user": event.get("user"),
        "ts": event.get("ts"),
        "bot_id": event.get("bot_id"),
    }


def extract_challenge(raw_payload: dict[str, Any]) -> str:
    """Return the ``challenge`` value from a ``url_verification`` payload.

    Slack sends ``{"type": "url_verification", "challenge": "..."}`` when the
    Events API request URL is configured. Raises ``SlackChallengeNotFoundError``
    if the payload is not a verification payload or lacks ``challenge``.
    """
    if not isinstance(raw_payload, dict):
        raise SlackChallengeNotFoundError("URL verification payload must be a JSON object")
    if raw_payload.get("type") != "url_verification":
        raise SlackChallengeNotFoundError(f"Not a url_verification payload (type={raw_payload.get('type')!r})")
    challenge = raw_payload.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise SlackChallengeNotFoundError("URL verification payload is missing the 'challenge' field")
    return challenge


def _slack_dedup_hash(event_id: str) -> str:
    """Dedup hash for a Slack event: namespace the event_id so it cannot
    collide with a webhook payload hash for the same trigger."""
    return hashlib.sha256(f"slack_app_mention:{event_id}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Engine handler
# ---------------------------------------------------------------------------


def _trigger_failure_hash(trigger_id: uuid.UUID) -> str:
    """Dedup hash used for pre-dedup audit events (namespaced by trigger)."""
    return _slack_dedup_hash(str(trigger_id))


async def _resolve_signing_secret(trigger_id: uuid.UUID, cfg: dict[str, Any]) -> str:
    """Decode the trigger's Slack signing secret, falling back to the raw
    value if decryption fails."""
    signing_secret_raw = cfg.get("signing_secret")
    if not signing_secret_raw:
        raise SlackSignatureError("Slack signing secret is not configured for this trigger")
    try:
        from modulo.settings import get_settings as _get_settings

        return decode_stored_secret(signing_secret_raw, _get_settings().fernet_key)
    except Exception:
        _log.exception("slack_app_mention.signing_secret_decrypt_failed trigger=%s", trigger_id)
        return str(signing_secret_raw)


async def _parse_mention(
    session: AsyncSession,
    *,
    engine: TriggerEngine,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    raw_payload: dict[str, Any],
) -> dict[str, Any]:
    """Parse the app_mention envelope, logging and re-raising typed errors.

    Non-app_mention events and malformed payloads are audited (with a
    TriggerEvent) and re-raised so the caller can map them to the right status.
    A ``SlackChallengeNotFoundError`` (a routing error) is re-raised untouched.
    """
    try:
        return parse_app_mention_payload(raw_payload)
    except SlackChallengeNotFoundError:
        # A url_verification payload reaching the engine is a routing error —
        # the route layer handles challenges before calling the engine.
        raise
    except SlackEventTypeError as exc:
        _log.info("Slack event not an app_mention for trigger %s: %s", trigger_id, exc)
        await engine._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=_trigger_failure_hash(trigger_id),
            result="event_type_not_accepted",
            error_detail=str(exc)[:200],
        )
        raise
    except SlackAppMentionParseError as exc:
        _log.warning("Slack app_mention payload parse failed for trigger %s: %s", trigger_id, exc)
        await engine._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=_trigger_failure_hash(trigger_id),
            result="parse_failed",
            error_detail=str(exc)[:200],
        )
        raise


async def _load_pipeline_rate_limit(session: AsyncSession, pipeline_id: uuid.UUID) -> dict[str, Any] | None:
    """Look up the pipeline's rate-limit config when none is set on the trigger.

    Uses the RLS execution hatch so a team-private pipeline's config is
    readable in the org-only (no user principal) Slack automation context.
    """
    from modulo.db.models.pipeline import Pipeline

    await set_rls_execution_context(session)
    pipe_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = pipe_result.scalar_one_or_none()
    return pipeline.rate_limit_config if pipeline is not None else None


async def _check_rate_limit(
    session: AsyncSession,
    *,
    engine: TriggerEngine,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    input_payload: dict[str, Any],
    cfg: dict[str, Any],
    dedup_hash: str,
) -> tuple[str | None, int | None, int | None]:
    """Resolve and enforce the rate limit.

    Returns ``(rate_limit_key, max_triggers, window_seconds)``. When no rate
    limit applies, returns ``(None, None, None)``. Raises
    ``PipelineRateLimitError`` when the rate limit is exceeded.
    """
    pipeline_rate_limit = cfg.get("rate_limit")
    if pipeline_rate_limit is None:
        pipeline_rate_limit = await _load_pipeline_rate_limit(session, pipeline_id)
    if not pipeline_rate_limit or not pipeline_rate_limit.get("max_triggers"):
        return None, None, None

    max_triggers = int(pipeline_rate_limit["max_triggers"])
    window_seconds = int(pipeline_rate_limit.get("window_seconds", _MAX_RATE_LIMIT_WINDOW_FALLBACK))
    rate_limit_key = engine._compute_rate_limit_key(input_payload, pipeline_rate_limit)
    recent_count = await engine._count_recent_rate_limited(session, pipeline_id, rate_limit_key, window_seconds)
    if recent_count >= max_triggers:
        _log.warning(
            "Rate limit exceeded for pipeline %s: %d >= %d for key %s",
            pipeline_id,
            recent_count,
            max_triggers,
            rate_limit_key,
        )
        await engine._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=dedup_hash,
            result="rate_limited",
        )
        raise PipelineRateLimitError(pipeline_id, rate_limit_key, max_triggers, window_seconds)
    return rate_limit_key, max_triggers, window_seconds


async def _create_run(
    session: AsyncSession,
    *,
    engine: TriggerEngine,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    input_payload: dict[str, Any],
    cfg: dict[str, Any],
    rate_limit_key: str | None,
    dedup_hash: str,
    max_triggers: int | None,
    window_seconds: int | None,
) -> Run:
    """Create the Run, mapping the rate-limit conflict to ``PipelineRateLimitError``."""
    refs = _extract_work_item_refs(input_payload, cfg.get("work_item_ref_paths"))
    try:
        return await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="slack_app_mention",
            input_payload=input_payload,
            trigger_id=trigger_id,
            rate_limit_key=rate_limit_key,
            work_item_refs=refs,
        )
    except RateLimitConflictError as exc:
        _log.warning("Rate limit conflict for pipeline %s: %s", pipeline_id, exc.rate_limit_key)
        await engine._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=dedup_hash,
            result="rate_limited",
        )
        raise PipelineRateLimitError(
            pipeline_id,
            exc.rate_limit_key,
            max_triggers if max_triggers is not None else 0,
            window_seconds if window_seconds is not None else 0,
        ) from exc


async def handle_app_mention(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    raw_body: bytes,
    raw_payload: dict[str, Any],
    slack_signature: str | None,
    slack_timestamp: str | None,
    snapshot_id: uuid.UUID,
) -> tuple[Run, TriggerEvent, dict[str, Any]]:
    """Process an incoming Slack ``app_mention`` event.

    Mirrors ``TriggerEngine.handle_webhook``: signs, parses, dedupes by Slack
    ``event_id``, applies ``payload_mapping``, and creates a Run +
    TriggerEvent. Returns ``(Run, TriggerEvent, input_payload)`` on success.

    Validation failures raise typed exceptions (``SlackSignatureError``,
    ``SlackTimestampExpiredError``, ``SlackEventTypeError``,
    ``DuplicateWebhookError``, ...). A TriggerEvent is always written (pass or
    fail) so every delivery attempt is audited. The caller must have already
    set RLS context on the session.
    """
    engine = TriggerEngine()

    key1, key2 = _uuid_to_lock_keys(trigger_id)
    lock_result = await session.execute(
        text("SELECT pg_try_advisory_lock(:key1, :key2)"),
        {"key1": key1, "key2": key2},
    )
    if not lock_result.scalar_one():
        raise TriggerBusyError(trigger_id)
    try:
        trigger = await engine._load_trigger(session, trigger_id, org_id)
        cfg = trigger.config_json or {}

        signing_secret = await _resolve_signing_secret(trigger_id, cfg)

        # X-Slack-Request-Timestamp replay window check
        verify_slack_timestamp(slack_timestamp)

        # Signature validation (constant-time)
        if not verify_slack_signature(raw_body, signing_secret, slack_timestamp, slack_signature):
            _log.warning("Slack signature validation failed for trigger %s", trigger_id)
            await engine._log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                payload_hash=_trigger_failure_hash(trigger_id),
                result="hmac_failed",
            )
            raise SlackSignatureError("Slack X-Slack-Signature is missing or invalid")

        # Org-wide pause kill-switch — before the dedup insert so a paused
        # delivery does not consume a dedup slot. Read failures propagate.
        await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type="slack_app_mention")

        mention = await _parse_mention(
            session,
            engine=engine,
            trigger=trigger,
            org_id=org_id,
            trigger_id=trigger_id,
            raw_payload=raw_payload,
        )

        # Deduplication by Slack event_id
        dedup_hash = _slack_dedup_hash(mention["event_id"])
        is_new = await engine._try_insert_dedup(session, trigger_id, org_id, dedup_hash)
        if not is_new:
            _log.warning(
                "Slack app_mention deduplicated for trigger %s (event_id=%s)",
                trigger_id,
                mention["event_id"],
            )
            await engine._log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                payload_hash=dedup_hash,
                result="deduplicated",
            )
            raise DuplicateWebhookError(dedup_hash)

        # Flood / concurrency protection — accept and queue instead of rejecting.
        active_count = await engine._count_active_runs(session, trigger.id)
        if active_count >= trigger.max_concurrent_runs:
            _log.warning(
                "Slack app_mention concurrency limit reached for trigger %s (%d active >= %d limit) — queuing anyway",
                trigger_id,
                active_count,
                trigger.max_concurrent_runs,
            )
            await engine._log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                payload_hash=dedup_hash,
                result="concurrency_limit_reached",
            )

        # Payload mapping (against the parsed mention fields)
        mapping: dict[str, str] = cfg.get("payload_mapping", {})
        input_payload = _apply_payload_mapping(mention, mapping)

        rate_limit_key, max_triggers, window_seconds = await _check_rate_limit(
            session,
            engine=engine,
            trigger=trigger,
            org_id=org_id,
            trigger_id=trigger_id,
            pipeline_id=trigger.pipeline_id,
            input_payload=input_payload,
            cfg=cfg,
            dedup_hash=dedup_hash,
        )

        run = await _create_run(
            session,
            engine=engine,
            trigger=trigger,
            org_id=org_id,
            trigger_id=trigger_id,
            pipeline_id=trigger.pipeline_id,
            snapshot_id=snapshot_id,
            input_payload=input_payload,
            cfg=cfg,
            rate_limit_key=rate_limit_key,
            dedup_hash=dedup_hash,
            max_triggers=max_triggers,
            window_seconds=window_seconds,
        )

        # Audit log
        trigger_event = await engine._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            payload_hash=dedup_hash,
            result="accepted",
            run_id=run.id,
        )
        _log.info(
            "Slack app_mention accepted for trigger %s → run %s (event_id=%s)",
            trigger_id,
            run.id,
            mention["event_id"],
        )

        # Store raw payload for replay
        await _store_raw_payload(
            session,
            trigger_event_id=trigger_event.id,
            raw_body=raw_body,
            raw_payload=raw_payload,
            org_id=org_id,
        )

        return run, trigger_event, input_payload
    finally:
        await session.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )


async def _store_raw_payload(
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
