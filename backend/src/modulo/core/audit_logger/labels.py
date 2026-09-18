"""Semantic actor labels and human-readable summaries for audit events.

The audit chain hashes each event's serialized fields (payload included) at
write time, so actor and summary MUST be composed at the emit site and carried
inside ``payload_json`` — never derived client-side and never back-filled.

Payload keys introduced here:
  - ``actor``   — display label for non-user actors (a trigger identity or the
    literal system actor); user-initiated events keep the canonical
    ``actor_user_id`` column instead
  - ``summary`` — one-line human-readable description of the event

Legacy events written before this module existed simply lack these keys, so
every consumer MUST fall back to its pre-label rendering when they are absent:
old and new rows render side by side and Verify Chain semantics are unchanged.
"""

from __future__ import annotations

import uuid

__all__ = [
    "MANUAL_TRIGGER_TYPE",
    "SYSTEM_ACTOR",
    "compose_run_started_summary",
    "compose_trigger_actor",
    "resolve_run_actor",
    "short_id",
]

SYSTEM_ACTOR = "system"
MANUAL_TRIGGER_TYPE = "manual"


def short_id(value: uuid.UUID | str | None) -> str | None:
    """First 8 characters of a UUID/str value (display shorthand), or None."""
    if value is None:
        return None
    return str(value)[:8]


def compose_trigger_actor(trigger_type: str | None, trigger_id: uuid.UUID | None) -> str:
    """Human-readable actor label for a trigger-started run.

    e.g. ``cron trigger (a1b2c3d4)`` / ``webhook trigger`` / ``system``.
    """
    base = f"{trigger_type} trigger" if trigger_type else SYSTEM_ACTOR
    tid = short_id(trigger_id)
    return f"{base} ({tid})" if tid else base


def resolve_run_actor(
    *,
    trigger_type: str | None,
    trigger_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, str | None]:
    """Resolve the semantic actor for a run lifecycle audit event.

    Returns ``(actor_user_id, actor_label)``. User-initiated (manual) runs
    resolve to the acting user's account id; trigger-started runs resolve to a
    trigger identity label (the trigger identity wins over any owner account
    stamped on the run); anything unidentifiable resolves to the literal
    system actor.
    """
    if trigger_type == MANUAL_TRIGGER_TYPE or trigger_type is None:
        if account_id is not None:
            return account_id, None
        return None, SYSTEM_ACTOR
    return None, compose_trigger_actor(trigger_type, trigger_id)


def compose_run_started_summary(
    pipeline_name: str | None,
    pipeline_id: uuid.UUID,
    trigger_type: str | None,
) -> str:
    """Descriptive one-line summary for a ``run_started`` event.

    e.g. ``Pipeline "PR Reviewer Agent" (d6b2c25b) run triggered by webhook``.
    Manual runs attribute the source to the user request (the actor column
    carries the acting user's id).
    """
    pid = short_id(pipeline_id) or "unknown"
    name_part = f'Pipeline "{pipeline_name}"' if pipeline_name else "Pipeline"
    source = "user request" if trigger_type == MANUAL_TRIGGER_TYPE else (trigger_type or "unknown")
    return f"{name_part} ({pid}) run triggered by {source}"
