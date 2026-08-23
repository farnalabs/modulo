"""Polling triggers — periodically polls external sources and fires on schedule.

The fire logic lives in ONE place: ``cron_helpers.fire_polling_trigger``,
enqueued as a per-item SAQ fire job by ``fire_due_triggers`` (system cron).
This module keeps the connector builder + JMESPath condition evaluation
(imported by other code) and re-exports the fire function so legacy callers
and tests keep importing ``polling.fire_polling_trigger``. The old duplicated
~200-line copy was removed (M2 drift fix); the legacy beat path (Celery) was
removed in PR C of the Celery->SAQ migration.
"""

import datetime
import hashlib
import logging
import uuid
from decimal import Decimal
from typing import Any

import jmespath
import jmespath.exceptions
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.connectors.base import ConnectorBase, ConnectorResult
from modulo.db.models.run import ACTIVE_RUN_STATUSES, Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent

_log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ACTIVE_RUN_STATUSES


# ---------------------------------------------------------------------------
# Connector builder (standalone copy to avoid circular imports)
# ---------------------------------------------------------------------------


def _build_polling_connector(type_id: str, config: dict[str, Any], creds: dict[str, Any]) -> ConnectorBase:
    """Build a one-shot connector for polling queries.

    Mirrors ``modulo.core.connector_hub._build_connector()`` but does not
    wrap in a ``_TracedConnector`` since polling runs outside a normal run context.
    """
    from modulo.connectors.filesystem import FilesystemConnector
    from modulo.connectors.github import GitHubConnector
    from modulo.connectors.gitlab import GitLabConnector
    from modulo.connectors.jira import JiraConnector
    from modulo.connectors.linear import LinearConnector
    from modulo.connectors.slack import SlackConnector

    match type_id:
        case "filesystem":
            base_path = config.get("base_path")
            if not base_path:
                raise ValueError("FilesystemConnector requires 'base_path' in config_json")
            return FilesystemConnector(base_path=base_path)
        case "github":
            return GitHubConnector(token=creds["token"])
        case "gitlab":
            base_url = config.get("base_url", "https://gitlab.com/api/v4")
            return GitLabConnector(token=creds["token"], base_url=base_url)
        case "jira":
            instance = config.get("instance", "")
            base_url = config.get("base_url")
            if not instance and not base_url:
                raise ValueError("JiraConnector requires 'instance' or 'base_url' in config_json")
            return JiraConnector(
                instance=instance,
                creds=creds,
                base_url=base_url,
                api_version=config.get("api_version", 3),
            )
        case "slack":
            return SlackConnector(bot_token=creds["bot_token"])
        case "linear":
            return LinearConnector(token=creds["token"])
        case _:
            raise ValueError(f"Unsupported connector type for polling: {type_id!r}")


# ---------------------------------------------------------------------------
# JMESPath condition evaluation
# ---------------------------------------------------------------------------


def evaluate_condition(
    result: ConnectorResult,
    condition_expression: str | None,
) -> bool:
    """Evaluate a JMESPath *condition_expression* against a connector result.

    If *condition_expression* is ``None`` or empty, any non-empty result set
    is treated as a match.

    The expression is evaluated against the ``records`` list of the result.
    Returns ``True`` if the expression yields a truthy value (non-empty list,
    non-zero number, ``True`` boolean, or a non-null value).
    """
    if not condition_expression:
        return bool(result.records)

    try:
        compiled = jmespath.compile(condition_expression)
    except jmespath.exceptions.JMESPathError as exc:
        raise ValueError(f"Invalid JMESPath expression: {exc}") from exc

    value = compiled.search(result.records)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, dict)):
        return bool(value)
    if isinstance(value, str):
        return bool(value)
    return True


# ---------------------------------------------------------------------------
# Fire logic — single implementation in cron_helpers (per-item SAQ fire job)
# ---------------------------------------------------------------------------

# Re-export the single implementation. The historical duplicate copy in this
# module drifted (spend-limit check, SATimeoutError vs builtin TimeoutError);
# the live path has always been ``saq_worker -> cron_helpers.fire_polling_trigger``.
from modulo.core.cron_helpers import fire_polling_trigger  # noqa: E402

# Backward-compatible alias — tests import the old private name.
_fire_polling_trigger = fire_polling_trigger


async def _update_next_fire(session: AsyncSession, trigger: Trigger) -> None:
    """Compute and persist the next fire time based on poll_interval_seconds,
    also updating last_fired_at to now. Only call this when a run was actually created.
    """
    config = trigger.config_json or {}
    interval = max(int(config.get("poll_interval_seconds") or 60), 1)
    now = datetime.datetime.now(datetime.UTC)
    next_fire = now + datetime.timedelta(seconds=interval)
    await session.execute(
        update(Trigger).where(Trigger.id == trigger.id).values(last_fired_at=now, next_fire_at=next_fire)
    )


async def _update_next_fire_no_last(session: AsyncSession, trigger: Trigger) -> None:
    """Advance next_fire_at without touching last_fired_at.
    Used when the condition was NOT met — the trigger didn't actually fire.
    """
    config = trigger.config_json or {}
    interval = max(int(config.get("poll_interval_seconds") or 60), 1)
    now = datetime.datetime.now(datetime.UTC)
    next_fire = now + datetime.timedelta(seconds=interval)
    await session.execute(update(Trigger).where(Trigger.id == trigger.id).values(next_fire_at=next_fire))


# ---------------------------------------------------------------------------
# RLS + helpers (standalone copies, same pattern as cron_helpers.py)
# ---------------------------------------------------------------------------


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Set org-scoped RLS context for a polling-trigger transaction."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.organisation_id', :val, true)"),
            {"val": str(org_id)},
        )
        await session.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
    else:
        session.info["org_id"] = org_id


async def _count_active_runs(session: AsyncSession, trigger_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).where(
            Run.trigger_id == trigger_id,
            Run.status.in_(_ACTIVE_STATUSES),
            Run.cancellation_requested.is_(False),
        )
    )
    return int(result.scalar_one() or 0)


async def _daily_spend_limit_reached(
    session: AsyncSession,
    trigger: Trigger,
    org_id: uuid.UUID,
) -> Decimal | None:
    """Return today's total run cost when the trigger's daily spend limit is exceeded.

    Returns ``None`` when no limit is configured or the limit has not been
    reached yet — the trigger may fire.
    """
    limit = trigger.daily_spend_limit
    if limit is None:
        return None
    from modulo.core.cost_controller import created_at_day_start

    today_start = created_at_day_start()
    cost_result = await session.execute(
        select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
            Run.trigger_id == trigger.id,
            Run.organisation_id == org_id,
            Run.created_at >= today_start,
        )
    )
    today_cost = cost_result.scalar_one()
    if today_cost is not None and today_cost >= limit:
        return today_cost
    return None


async def _log_poll_event(
    session: AsyncSession,
    *,
    trigger: Trigger,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> TriggerEvent:
    payload_hash = hashlib.sha256(f"polling:{trigger.id}:{result}".encode()).hexdigest()
    event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type="polling",
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=error_detail,
    )
    session.add(event)
    await session.flush()
    return event
