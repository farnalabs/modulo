"""Rollback threshold evaluator for script-mode sandbox_agent runs (FAR-296 Phase 5b).

Periodically (wired into dispatcher_reconcile._run_reconcile_sweeps) evaluates
script-mode run outcomes against volume-gated thresholds. When unexpected
outcome rates exceed the threshold, emits a structured WARNING log so the
human on-call can review and decide whether to disable the feature for the
affected org.

This module is DETECTION + NOTIFICATION only -- it never auto-flips or
auto-disables. The human on-call decides the response.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run

_log = logging.getLogger(__name__)

# Script-mode error codes that indicate an anomalous outcome. Script-mode
# sandboxes killed by the platform-side runtime killer surface as
# ``script.budget_killed`` (see pipeline_engine/error_codes.py) — NOT a
# phantom ``timeout.kill`` which would match nothing and silently disable the
# claim_without_marker anomaly.
_SCRIPT_ANOMALY_ERROR_CODES = frozenset({"script.side_effect_unknown", "script.budget_killed"})

# Script-mode error code for unexpected side_effect_unknown.
_SCRIPT_SIDE_EFFECT_UNKNOWN = "script.side_effect_unknown"
_LOG_ANOMALY_DETECTED = "rollback_threshold.anomaly_detected"


def _graph_has_script_mode_node(graph_json: dict[str, Any] | None) -> bool:
    """Check if a snapshot graph contains a sandbox_agent node with mode='script'."""
    if not isinstance(graph_json, dict):
        return False
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(
        isinstance(n, dict) and n.get("node_type") == "sandbox_agent" and n.get("mode") == "script" for n in nodes
    )


def _flag_anomaly(
    *,
    org_id: uuid.UUID,
    total: int,
    window_hours: int,
    anomaly_type: str,
    anomaly_count: int,
    flagged_orgs: list[str],
) -> bool:
    """Log a detected anomaly and register the org as flagged.

    Returns True when an anomaly was detected (so the caller can increment its
    anomaly counter), False otherwise. Appends ``str(org_id)`` to ``flagged_orgs``
    at most once.
    """
    if anomaly_count <= 0:
        return False
    if str(org_id) not in flagged_orgs:
        flagged_orgs.append(str(org_id))
    _log.warning(
        _LOG_ANOMALY_DETECTED,
        extra={
            "org_id": str(org_id),
            "total_runs": total,
            "anomaly_type": anomaly_type,
            "anomaly_count": anomaly_count,
            "threshold_rate": f"{anomaly_count}/{total}",
            "window_hours": window_hours,
        },
    )
    return True


def _node_config_has_budget(node_def: dict[str, Any] | None) -> bool:
    """Check if a node definition has timeout_seconds or wallclock_budget_seconds."""
    if not isinstance(node_def, dict):
        return False
    return "timeout_seconds" in node_def or "wallclock_budget_seconds" in node_def


async def _count_script_runs_by_graph(
    session: AsyncSession,
    org_id: uuid.UUID,
    window_start: Any,
) -> int:
    """Count terminal runs whose snapshot graph contains a script-mode sandbox_agent node."""
    result = await session.execute(
        select(PipelineSnapshot.graph_json)
        .join(Run, Run.snapshot_id == PipelineSnapshot.id)
        .where(
            Run.organisation_id == org_id,
            Run.status.in_(sorted(TERMINAL_STATUSES)),
            Run.created_at >= window_start,
        )
    )
    return sum(1 for graph_json in result.scalars() if _graph_has_script_mode_node(graph_json))


async def _count_claim_without_marker(
    session: AsyncSession,
    org_id: uuid.UUID,
    window_start: Any,
) -> int:
    """Count runs with sandbox_dispatch_state set but ended with anomaly error codes."""
    result = await session.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.organisation_id == org_id,
            Run.sandbox_dispatch_state.isnot(None),
            Run.error_code.in_(sorted(_SCRIPT_ANOMALY_ERROR_CODES)),
            Run.created_at >= window_start,
        )
    )
    return int(result.scalar_one() or 0)


async def _count_contract_violation_delivered(
    session: AsyncSession,
    org_id: uuid.UUID,
    window_start: Any,
) -> int:
    """Count runs classified as 'delivered' but with work_intact=False."""
    result = await session.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.organisation_id == org_id,
            Run.run_classification.isnot(None),
            Run.run_classification["value"].as_string() == "delivered",
            Run.run_classification["work_intact"].as_string() == "false",
            Run.created_at >= window_start,
        )
    )
    return int(result.scalar_one() or 0)


async def _count_unexpected_side_effect_unknown(
    session: AsyncSession,
    org_id: uuid.UUID,
    window_start: Any,
) -> int:
    """Count side_effect_unknown runs with no timeout/budget configured."""
    # Query runs with the error code, then join to snapshot to check node config.
    result = await session.execute(
        select(Run.id, PipelineSnapshot.graph_json)
        .join(PipelineSnapshot, Run.snapshot_id == PipelineSnapshot.id)
        .where(
            Run.organisation_id == org_id,
            Run.error_code == _SCRIPT_SIDE_EFFECT_UNKNOWN,
            Run.created_at >= window_start,
        )
    )
    count = 0
    for _run_id, graph_json in result.all():
        if not isinstance(graph_json, dict):
            continue
        nodes = graph_json.get("nodes", [])
        if not isinstance(nodes, list):
            continue
        # Check if ANY sandbox_agent node in the graph lacks timeout/budget.
        for node in nodes:
            if (
                isinstance(node, dict)
                and node.get("node_type") == "sandbox_agent"
                and not _node_config_has_budget(node)
            ):
                count += 1
                break
    return count


async def _resolve_org_ids(
    session_factory: Callable[..., Any],
    org_ids: list[uuid.UUID] | None,
) -> list[uuid.UUID]:
    """Return the explicit org id list, or all org ids when None is given."""
    if org_ids is not None:
        return org_ids
    async with session_factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        return list(result.scalars())


async def _evaluate_org(
    session_factory: Callable[..., Any],
    org_id: uuid.UUID,
    window_start: Any,
    window_hours: int,
    min_runs: int,
) -> tuple[int, int, list[str]]:
    """Evaluate a single org against the rollback thresholds.

    Returns ``(orgs_checked, anomalies_found, flagged_orgs)``. ``orgs_checked``
    is 1 only when the org had at least ``min_runs`` script-mode runs (otherwise
    0); ``flagged_orgs`` contains ``str(org_id)`` at most once.
    """
    async with session_factory() as session, session.begin():
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)

        total = await _count_script_runs_by_graph(session, org_id, window_start)
        if total < min_runs:
            return 0, 0, []

        flagged_orgs: list[str] = []
        anomalies_found = 0

        claim_without_marker = await _count_claim_without_marker(session, org_id, window_start)
        contract_violation = await _count_contract_violation_delivered(session, org_id, window_start)
        unexpected_side_effect = await _count_unexpected_side_effect_unknown(session, org_id, window_start)

        if _flag_anomaly(
            org_id=org_id,
            total=total,
            window_hours=window_hours,
            anomaly_type="claim_without_marker",
            anomaly_count=claim_without_marker,
            flagged_orgs=flagged_orgs,
        ):
            anomalies_found += 1
        if _flag_anomaly(
            org_id=org_id,
            total=total,
            window_hours=window_hours,
            anomaly_type="contract_violation_delivered",
            anomaly_count=contract_violation,
            flagged_orgs=flagged_orgs,
        ):
            anomalies_found += 1
        if _flag_anomaly(
            org_id=org_id,
            total=total,
            window_hours=window_hours,
            anomaly_type="unexpected_side_effect_unknown",
            anomaly_count=unexpected_side_effect,
            flagged_orgs=flagged_orgs,
        ):
            anomalies_found += 1

    return 1, anomalies_found, flagged_orgs


async def evaluate_rollback_thresholds(
    session_factory: Callable[..., Any],
    *,
    org_ids: list[uuid.UUID] | None = None,
    min_runs: int = 30,
    window_hours: int = 24,
    budget_seconds: float = 15.0,
) -> dict[str, Any]:
    """Evaluate script-mode run outcomes against rollback thresholds.

    Volume-gated: only flags anomalies when the org has >= ``min_runs``
    terminal runs in the ``window_hours`` window. Returns
    ``{"orgs_checked", "anomalies_found", "flagged_orgs"}``.
    """
    deadline = time.monotonic() + budget_seconds
    from datetime import UTC, datetime, timedelta

    window_start = datetime.now(UTC) - timedelta(hours=window_hours)

    orgs_checked = 0
    anomalies_found = 0
    flagged_orgs: list[str] = []

    try:
        org_ids = await _resolve_org_ids(session_factory, org_ids)

        for org_id in org_ids:
            if time.monotonic() > deadline:
                break
            try:
                checked, found, flagged = await _evaluate_org(
                    session_factory, org_id, window_start, window_hours, min_runs
                )
                orgs_checked += checked
                anomalies_found += found
                for org in flagged:
                    if org not in flagged_orgs:
                        flagged_orgs.append(org)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning(
                    "rollback_threshold.org_check_failed",
                    extra={"org_id": str(org_id)},
                    exc_info=True,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("rollback_threshold.evaluation_failed", exc_info=True)

    return {
        "orgs_checked": orgs_checked,
        "anomalies_found": anomalies_found,
        "flagged_orgs": flagged_orgs,
    }
