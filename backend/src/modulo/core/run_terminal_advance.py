"""Shared post-terminalise advance orchestration (FAR-604 F4).

Both the legacy stale-run sweep (``pipeline_execution.stale_run_recovery_sweep``)
and the FAR-604 slot-reconciliation sweep (``run_admission.reconcile_pipeline_slots``)
terminalise runs with raw UPDATEs that never run ``finalize_cost`` — without a
compensating advance the swept runs' journeys would never move (FAR-143
follow-up) and they would be invisible to the analytics failure/stall
dimensions (FAR-162, P6'). This module owns the ONE shared advance sequence so
the two sweeps cannot drift:

* journeys advance from the run's CREATE-STAMPED refs (``runs.work_item_refs``)
  — skipped when the run carries no refs (nothing to advance);
* the compensating daily fact runs UNCONDITIONALLY (a refs-less run still gets
  its fact — a no-refs early return here would silently drop the sweep's
  analytics row, the F3 bug the pre-extraction ``run_admission`` copy had).

Dependencies are deliberately minimal (``db.rls``, ``db.crud.run``,
``lifecycle_map.advancement``, ``core.analytics``) and langgraph-free:
``cron_helpers`` (reached from the API layer via the health route and the
triggers routes) imports ``run_admission``, so everything this module pulls in
transitively must keep the ``api-does-not-import-langgraph-directly``
import-linter contract green.

Every step is fail-open per run: one run's advance failure must never fail the
sweep that already committed its terminal UPDATE.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.run import get_run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)


async def advance_journeys_from_stored_refs(
    async_engine: AsyncEngine,
    run_id: str | uuid.UUID,
    org_id: str | uuid.UUID,
    status: str = "failed",
) -> None:
    """Advance journeys from a run's stored refs, fail-open (FAR-143).

    ``mark_complete`` / ``fail_run_terminal`` / the stale-run sweeps write the
    terminal status with a raw ``text()`` UPDATE on a connection and never run
    ``finalize_cost`` (so they never parse outputs or persist them). Runs
    carrying CREATE-STAMPED refs would therefore never advance their journeys
    through those paths. This helper opens its OWN session/transaction AFTER
    the raw write succeeds (the write is committed before it runs) and
    advances journeys from the stored refs only — no self-report parse here
    (the raw writers have no merged outputs).

    FAIL-OPEN: a journey-write failure is logged and swallowed — it must never
    roll back or fail the already-committed terminal write.
    """
    try:
        from modulo.core.lifecycle_map.advancement import (
            _canonicalise_and_dedupe,
            advance_journeys,
            agent_minting_enabled,
            confirm_reported_refs,
        )
        from modulo.core.lifecycle_map.reconcile import (
            record_refs_agent_mint_budget_exceeded,
            record_refs_agent_mint_suppressed_by_flag,
            record_refs_agent_minted,
        )
        from modulo.core.runtime_config.mint_budget import consume_agent_mint_budget

        factory = async_sessionmaker(async_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, uuid.UUID(str(org_id)))
            run = await get_run(session, uuid.UUID(str(run_id)))
            if run is None:
                _log.warning("run_terminal_advance.journeys_run_missing run=%s", run_id)
                return
            refs = list(run.work_item_refs or [])
            # FAR-795 slice B: agent-sourced stored refs advance/mint ONLY
            # behind the org flag (fail-closed). The raw writers have no
            # finalize hook, so without this gate an agent-sourced entry on a
            # RAW-TERMINALISED run would mint/advance its journey unconditioned.
            agent_minting = await agent_minting_enabled(session, run.organisation_id)
            # Canonicalise agent entries once (the accounting must key on the
            # canonical (kind, ref) the journey rows are keyed on).
            canonical_agent = [
                c
                for c in _canonicalise_and_dedupe(
                    [e for e in refs if isinstance(e, dict) and e.get("source") == "agent"]
                )
                if c.get("source") == "agent"
            ]
            if not agent_minting:
                if canonical_agent:
                    record_refs_agent_mint_suppressed_by_flag(len(canonical_agent))
                refs = [e for e in refs if not (isinstance(e, dict) and e.get("source") == "agent")]
                if not refs:
                    # Nothing to advance — but NOT a reason to skip the facts
                    # half (the orchestrator runs it unconditionally).
                    return
            elif canonical_agent:
                # Flag ON mirrors the finalize/hydrate accounting: only the
                # agent entries with NO pre-existing journey row count as
                # fresh agent mints (the row-existence check is fail-open).
                try:
                    confirmed, _unmatched = await confirm_reported_refs(session, run.organisation_id, canonical_agent)
                except Exception:
                    _log.warning("run_terminal_advance.agent_mint_probe_failed run=%s", run_id, exc_info=True)
                    confirmed = []
                fresh_agent_mints = len(canonical_agent) - len(confirmed)
                if fresh_agent_mints > 0:
                    # FAR-795 budget cap: agent mints must clear the per-org
                    # budget before they are advanced. consume_agent_mint_budget
                    # is fail-open (errors/guard-outage allow), so a genuine
                    # within-window over-spend denies and the agent refs are
                    # suppressed (not advanced) exactly like the flag-OFF path.
                    budget_ok = await consume_agent_mint_budget(session, run.organisation_id, n=fresh_agent_mints)
                    if not budget_ok:
                        record_refs_agent_mint_budget_exceeded(fresh_agent_mints)
                        # Drop the agent refs from the advance set so they are
                        # neither minted nor advanced this run.
                        refs = [e for e in refs if not (isinstance(e, dict) and e.get("source") == "agent")]
                    else:
                        record_refs_agent_minted(fresh_agent_mints)
            if not refs:
                return
            await advance_journeys(
                session,
                run.organisation_id,
                run_id=run.id,
                pipeline_id=run.pipeline_id,
                refs=refs,
                status=status,
                completed_at=run.completed_at,
                run_created_at=run.created_at,
                is_replay=bool(run.is_replay),
                variant_group_id=run.variant_group_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("run_terminal_advance.journey_advance_failed run=%s", run_id, exc_info=True)


async def record_terminal_failed_fact(
    async_engine: AsyncEngine,
    run_id: str | uuid.UUID,
    org_id: str | uuid.UUID,
) -> None:
    """Best-effort daily-fact write for a run terminalised by a raw writer (P6').

    The raw writers (``fail_run_terminal`` / the stale-run sweeps) never run
    ``finalize_cost``, so those runs would never appear in ``run_daily_facts``
    (invisible in the analytics failure/stall dimensions). This helper opens
    its OWN session/transaction AFTER the raw terminal UPDATE commits, sets
    the RLS org context, re-selects the Run ORM (a pre-update entity would
    record ``status='running'`` with a NULL ``completed_at``), and records the
    daily fact via the shared
    :func:`modulo.core.analytics.record_fact_for_terminal_failed_run` wrapper.

    None-guarded and fail-open: any failure logs and is swallowed — it must
    never roll back or fail the already-committed terminal write. There is
    deliberately NO refs guard here: a refs-less run still gets its fact.
    """
    try:
        from modulo.core.analytics import record_fact_for_terminal_failed_run

        factory = async_sessionmaker(async_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, uuid.UUID(str(org_id)))
            run = await get_run(session, uuid.UUID(str(run_id)))
            if run is None:
                _log.warning("run_terminal_advance.facts_run_missing run=%s", run_id)
                return
            await record_fact_for_terminal_failed_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("run_terminal_advance.facts_advance_failed run=%s", run_id, exc_info=True)


async def advance_terminalised_run(async_engine: AsyncEngine, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Advance a terminalised sweep run's journeys and record its daily fact.

    The single orchestration shared by ``pipeline_execution``'s legacy
    stale-run sweep and ``run_admission``'s FAR-604 slot-reconciliation sweep
    (previously duplicated line-for-line — F4). Journeys advance first
    (guarded by the refs check inside
    :func:`advance_journeys_from_stored_refs`), then the daily fact runs
    UNCONDITIONALLY (:func:`record_terminal_failed_fact`) — the
    compensating-fact contract (FAR-162, P6') does not depend on the run
    carrying work-item refs (F3). Each half is fail-open per run.
    """
    await advance_journeys_from_stored_refs(async_engine, str(run_id), str(org_id), "failed")
    try:
        await record_terminal_failed_fact(async_engine, str(run_id), str(org_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("run_terminal_advance.terminal_facts_failed run=%s", run_id, exc_info=True)
