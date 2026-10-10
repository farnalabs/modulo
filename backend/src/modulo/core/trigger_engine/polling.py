"""Polling triggers — periodically polls external sources and fires on schedule.

The fire logic lives in ONE place: ``cron_helpers.fire_polling_trigger``,
enqueued as a per-item SAQ fire job by ``fire_due_triggers`` (system cron).
This module keeps the connector builder + JMESPath condition evaluation
(imported by other code) and re-exports the fire function so legacy callers
and tests keep importing ``polling.fire_polling_trigger``. The old duplicated
~200-line copy was removed (M2 drift fix); the legacy beat path (Celery) was
removed in PR C of the Celery->SAQ migration.
"""

import asyncio
import datetime as dt
import hashlib
import inspect
import logging
import uuid
from decimal import Decimal
from typing import Any

import jmespath
import jmespath.exceptions
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.connectors.base import (
    ConnectorACL,
    ConnectorBase,
    ConnectorPermissionError,
    ConnectorResult,
)
from modulo.db.models.run import ACTIVE_RUN_STATUSES, Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent

_log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ACTIVE_RUN_STATUSES


# ---------------------------------------------------------------------------
# Connector builder (standalone copy to avoid circular imports)
# ---------------------------------------------------------------------------


def _build_polling_connector(
    type_id: str,
    config: dict[str, Any],
    creds: dict[str, Any],
    *,
    redis_client: Any = None,
    tenant_id: str | None = None,
) -> ConnectorBase:
    """Build a one-shot connector for polling queries.

    Mirrors ``modulo.core.connector_hub._build_connector()`` but does not
    wrap in a ``_TracedConnector`` since polling runs outside a normal run context.

    When ``redis_client`` and ``tenant_id`` (the organisation id) are supplied the
    REST connector is wired to the SHARED fleet-wide per-destination rate budget
    (FAR-442): trigger-invoked REST connectors enforce the SAME budget as
    run-executor connectors, and each org gets its own budget (no cross-tenant
    ``"default"`` key). Without them the connector stays on the per-process local
    bucket (single-worker dev / no-fleet), which is correct when no shared budget
    exists to multiply.
    """
    from modulo.connectors.filesystem import FilesystemConnector
    from modulo.connectors.github import GitHubConnector
    from modulo.connectors.gitlab import GitLabConnector
    from modulo.connectors.jira import JiraConnector
    from modulo.connectors.linear import LinearConnector
    from modulo.connectors.rest import RestConnector
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
        case "rest":
            from modulo.core.connector_hub import _core_security_guard

            return RestConnector(
                config=config,
                creds=creds,
                security_guard=_core_security_guard(),
                redis_client=redis_client,
                tenant_id=tenant_id,
            )
        case _:
            raise ValueError(f"Unsupported connector type for polling: {type_id!r}")


def enforce_polling_read_acl(connector_instance: Any) -> None:
    """Enforce the instance's ``read`` ACL before any polling query (FAR-1583).

    Polling runs outside a normal run context, so
    :func:`_build_polling_connector` deliberately does NOT wrap the connector
    in ``_TracedConnector`` — and that wrapper is where the executor's
    per-operation ACL gate lives. Without this gate a scheduled poll could read
    through an instance whose non-empty ``allowed_operations`` excludes
    ``read``, while every other ACL call site in the product denies it.

    The check itself is delegated to :class:`ConnectorACL` so the polling path
    makes EXACTLY the same decision as the rest of the product (FAR-1564):
    ``None``/``[]`` are UNRESTRICTED and pass, a non-empty list must list
    ``read``, and a malformed value is restricted to nothing (fail closed).

    Polling is a system-level operation with no caller visibility scope, so an
    absent/garbage ``visibility`` is normalised to ``org`` rather than raising
    a ``ValueError`` the callers would surface as a generic build failure.
    Visibility no longer narrows an ACL check at all (FAR-1618 removed the
    team-scoped run-gate, and this path never passed one), but
    :class:`ConnectorACL` still validates the value in its constructor — the
    normalisation keeps a malformed column from failing the poll build.

    Raises :class:`ConnectorPermissionError` when the read is not permitted.
    """
    visibility = getattr(connector_instance, "visibility", None)
    if visibility not in ("org", "team"):
        visibility = "org"
    ConnectorACL(
        visibility=visibility,
        allowed_operations=getattr(connector_instance, "allowed_operations", None),
    ).check("read")


async def enforce_polling_team_scope(
    session: AsyncSession,
    connector_instance: Any,
    org_id: uuid.UUID | str,
    pipeline_id: uuid.UUID | None,
) -> None:
    """Enforce the instance's TEAM scope against the polling trigger's pipeline (FAR-1595).

    A trigger's ``connector_instance_id`` was never team-validated: the fire
    job reads the connector row TEAM-BLIND (``app.execution_context`` is set,
    ``cron_helpers._set_rls_org``) and selects it without a team predicate, so
    a team-A trigger naming a team-B connector id polled it — reading
    team-private credentials with only the allowlist gate applied. This is the
    defence-in-depth half; the save-time half lives in
    ``api/routes/triggers.py`` (REST) and ``api/mcp_server.py`` (the MCP
    trigger tools), both through the shared
    ``_validate_connector_instance_team_scope`` gate.

    Scope rule: a TEAM-PRIVATE connector is only usable by a pipeline owned by
    the SAME team — the shared
    :func:`modulo.core.team_visibility.connector_team_mismatch` predicate, so
    this gate cannot drift from the graph-save rule. An ORG-visible connector
    never mismatches here: polling is a system-level read with no caller
    visibility scope (``enforce_polling_read_acl`` deliberately normalises
    ``visibility`` to ``org`` for exactly that reason), so an org-wide
    connector is usable by any team's pipeline the same way it is everywhere
    else. There is no reverse direction to enforce either (FAR-1618): an
    org-visibility connector is shared across the organisation, so the
    predicate never reports one and neither the save-time gate nor any
    invocation-time gate refuses it.

    Runs BEFORE any credential is decrypted (same ordering contract as
    :func:`enforce_polling_read_acl`), so a denied instance never exposes its
    secrets to this path.

    *pipeline_id* is the owning trigger's pipeline. When the caller supplies
    NONE there is no team context to judge against — DENIED, on exactly the
    same rule as an id that does not resolve in *org_id*: a read whose team
    scope we cannot verify must not proceed. Every production caller has it
    (the SAQ fire job passes the trigger's ``pipeline_id``, which is NOT NULL,
    and ``TriggerEngine.evaluate_condition`` passes its own trigger's), so a
    missing context is a wiring bug rather than an expected state — reporting
    it as a denial is what stops the next forgotten argument from silently
    reopening this gate. The gate is defence-in-depth on top of the save-time
    validation and the request session's team RLS, never the sole control.

    Raises :class:`ConnectorPermissionError` when the read must not proceed.
    """
    visibility = getattr(connector_instance, "visibility", None)
    if (visibility or "org") != "team":
        # Org-visible connector: usable by every team (see docstring). This
        # short-circuit also means no pipeline read is issued at all.
        return
    if pipeline_id is None:
        raise ConnectorPermissionError(
            f"Team-private connector {getattr(connector_instance, 'id', '?')} was read with no "
            "pipeline context; the team scope of the connector read cannot be verified",
        )

    from modulo.core.team_visibility import connector_team_mismatch
    from modulo.db.models.pipeline import Pipeline

    org_uuid = org_id if isinstance(org_id, uuid.UUID) else uuid.UUID(str(org_id))
    pipeline = (
        await session.execute(
            select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.organisation_id == org_uuid),
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise ConnectorPermissionError(
            f"Polling trigger pipeline {pipeline_id} does not resolve in this organisation; "
            "the team scope of the connector read cannot be verified",
        )
    connector_owner_team_id = getattr(connector_instance, "owner_team_id", None)
    if connector_team_mismatch(visibility, connector_owner_team_id, pipeline.owner_team_id):
        raise ConnectorPermissionError(
            f"Team-private connector {getattr(connector_instance, 'id', '?')} (owner team "
            f"{connector_owner_team_id}) is not usable by pipeline {pipeline_id} "
            f"(owner team {pipeline.owner_team_id})",
        )


async def _build_polling_connector_from_instance(
    session: AsyncSession,
    connector_instance: Any,
    org_id: uuid.UUID | str,
    *,
    pipeline_id: uuid.UUID | None = None,
) -> tuple[ConnectorBase, Any]:
    """Wire a polling REST connector to the shared fleet-wide rate budget (FAR-1583 ACL).

    The FIRST things this does are :func:`enforce_polling_read_acl` and
    :func:`enforce_polling_team_scope` — every poll read in the product flows
    through here (the SAQ cron fire job via ``cron_helpers._build_polling_connector``
    and the one-off ``TriggerEngine.evaluate_condition``), so this is the single
    gate that keeps a non-empty ``allowed_operations`` allowlist from being
    bypassed by the polling/cron read path, and the defence-in-depth team-scope
    gate for the same path (FAR-1595). BOTH run BEFORE any credential is
    decrypted, so a denied instance never exposes its secrets.

    *pipeline_id* is the owning trigger's pipeline — ALWAYS pass it when the
    caller has one (both production callers do). Without it a TEAM-PRIVATE
    instance is denied outright rather than read unchecked; see
    :func:`enforce_polling_team_scope` for the rule and its rationale.

    Shared by the cron fire path (``cron_helpers``) and the sync one-off
    ``TriggerEngine.evaluate_condition`` so the three-step wiring
    (``create_secrets_backend`` -> ``get_secret`` -> ``resolve_shared_rate_limit_redis``
    -> ``_build_polling_connector``) can never fork again (FAR-442).

    Returns ``(connector, redis_client)``. The caller OWNS both and MUST release
    them via :func:`_close_polling_resources` in a ``finally`` — otherwise a
    fresh ``Redis.from_url`` is built and left unclosed on every fire (FAR-442
    client leak). ``redis_client`` may be ``None`` (Redis genuinely not
    configured / a non-tenant probe), in which case the connector stays on its
    per-process local bucket — correct when no shared budget exists to multiply.

    Raises :class:`ConnectorPermissionError` (fail-closed) when the instance's
    allowlist excludes ``read`` — see :func:`enforce_polling_read_acl` — or when
    its team scope does not cover *pipeline_id* — see
    :func:`enforce_polling_team_scope`.

    Raises :class:`SharedBudgetUnavailableError` (fail-closed) when the shared
    budget is configured but unresolvable; the caller surfaces that per its own
    contract rather than degrading to the per-process bucket (which would
    reconstruct the fleet-wide ``N x burst`` fail-open FAR-439 removed).
    """
    import json

    # FAR-1583 / FAR-1595: gate FIRST — before any credential is decrypted, so
    # a denied instance never exposes its secrets to this path.
    enforce_polling_read_acl(connector_instance)
    await enforce_polling_team_scope(session, connector_instance, org_id, pipeline_id)

    from modulo.core.connector_hub import resolve_shared_rate_limit_redis
    from modulo.core.secrets_backend import create_secrets_backend
    from modulo.settings import get_settings

    settings = get_settings()
    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    raw_creds = await secrets_backend.get_secret(str(connector_instance.id))
    creds: dict[str, Any] = json.loads(raw_creds)
    redis_client = resolve_shared_rate_limit_redis(str(org_id))
    try:
        connector = _build_polling_connector(
            connector_instance.connector_type_id,
            connector_instance.config_json,
            creds,
            redis_client=redis_client,
            tenant_id=str(org_id),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # The connector build raised (bad config, missing file, KeyError on
        # creds, unsupported type). ``redis_client`` was resolved before the
        # build and never reaches the caller, so merely re-raising would
        # ORPHAN the fresh ``Redis.from_url`` pool on every fire (FAR-442
        # client leak). Close it here so a misconfigured connector leaks nothing.
        if redis_client is not None:
            await redis_client.aclose()
        raise
    return connector, redis_client


async def _close_polling_resources(connector: ConnectorBase | None, redis_client: Any | None) -> None:
    """Release a polling connector's async resources + its shared Redis client.

    Mirrors ``ConnectorHub._close_connectors`` so the polling path never leaks a
    pooled ``httpx.AsyncClient`` or the fresh ``Redis.from_url`` built per fire
    (FAR-442). A failing ``close()``/``aclose()`` is logged, never raised — a
    teardown failure must not mask the query outcome.
    """
    if connector is not None:
        close = getattr(connector, "close", None)
        if close is not None:
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _log.warning("Failed to close polling connector", exc_info=True)
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception:
            _log.warning("Failed to close shared Redis client", exc_info=True)


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
    now = dt.datetime.now(dt.UTC)
    next_fire = now + dt.timedelta(seconds=interval)
    await session.execute(
        update(Trigger).where(Trigger.id == trigger.id).values(last_fired_at=now, next_fire_at=next_fire)
    )


async def _update_next_fire_no_last(session: AsyncSession, trigger: Trigger) -> None:
    """Advance next_fire_at without touching last_fired_at.
    Used when the condition was NOT met — the trigger didn't actually fire.
    """
    config = trigger.config_json or {}
    interval = max(int(config.get("poll_interval_seconds") or 60), 1)
    now = dt.datetime.now(dt.UTC)
    next_fire = now + dt.timedelta(seconds=interval)
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
