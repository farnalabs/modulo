"""Run-outcome classification persisted at terminalization (FAR-189).

Stage 2 of the ongoing-trigger no-delivery auto-deactivation feature. FAR-188
added ``runs.raw_output_markers`` (JSONB keyed by attempt_key, each marker
carrying ``pr_url``); the streak engine (FAR-190) will query classification
records instead of raw run status. THIS module computes and persists a
classification record when a run reaches a terminal status.

The classifier is a pure function over EXISTING terminalization facts — it
never re-implements or re-scans anything:

* ``work_intact`` (FAR-152, computed at terminalization via
  ``evidence.compute_work_intact`` and stored on ``runs.work_intact``) is
  consumed as an input and recorded as metadata (the decision table does not
  depend on it — the spec'd (status, error_code) table is authoritative).
* node-return accessors (``node_output_split.node_return`` /
  ``node_telemetry``) read the stored per-node returns legacy-safe, so the
  ``pr_url`` of a delivered run is recovered from real node output.
* ``evidence._inner_declared_success`` counts declared-success nodes (recorded
  as metadata) without re-deriving the split/legacy shapes.
* ``runs.raw_output_markers`` supplies the FAR-188 ``pr_url`` per attempt_key —
  a pr_url recovered from ANY attempt key is a valid delivery signal
  (first-attempt PRs created before a sandbox stall/retry are real deliveries).

Decision table (spec, keyed on status — never prose):

| status          | outcome                                        |
|-----------------|------------------------------------------------|
| cancelled       | ``excluded`` (operator/HITL-cancelled — never countable, even with an unparseable reason) |
| budget_exceeded | ``excluded`` (and breaks the FAR-190 walk)      |
| failed / eval_failed / stalled | ``no_delivery`` (COUNTABLE — infra/sandbox crash elevated to failed counts, PO) |
| complete        | ``delivered`` iff >= 1 valid ``pr_url``; else COUNTABLE ``no_delivery`` (empty-backlog, PO) |
| (non-terminal)  | ``excluded`` guard (the hook only fires for terminal statuses) |

Persistence: a JSONB column on ``runs`` (``run_classification``) written in the
SAME transaction as the terminal status write. ``run_id`` is the runs PK, so the
record is UNIQUE(run_id) by construction; the write is a refresh (upsert) so a
re-terminalization (retry policy re-flips a classified run back to pending then
re-runs) overwrites the stale verdict with fresh evidence. The hook is
best-effort and NEVER raises: a classifier or persist failure writes an
``unclassified`` marker instead — a terminal run with NO record breaks the
FAR-190 walk (fail-closed against deactivation), so the marker (never a skip) is
what keeps the walk alive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.node_output_split import node_return, node_telemetry
from modulo.core.pipeline_engine.evidence import _inner_declared_success
from modulo.db.models.run import TERMINAL_STATUSES, Run

_log = logging.getLogger(__name__)

#: Reasons stored on the record (spec §"Store the reason too").
REASON_NO_WORK = "no_work"
REASON_NEEDS_HUMAN = "needs_human"
REASON_SOURCE_ERROR = "source_error"
REASON_PARSE_ERROR = "parse_error"
REASON_NO_DELIVERY = "no_delivery"
REASON_CANCELLED = "operator_or_hitl_cancelled"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_DELIVERED = "pr_delivered"
REASON_UNCLASSIFIED = "classifier_error"

#: Bounded scan depth when unwrapping a node return looking for ``pr_url``
#: (direct output_json, nested ``output``/``output_json``/``artifacts``).
_MAX_PR_URL_SCAN_DEPTH = 4

#: error_code substrings that mark a run needing a human (HITL) to progress.
_NEEDS_HUMAN_CODE_SUBSTRINGS: tuple[str, ...] = ("hitl", "human")

#: Explicit error codes whose failure means a human (HITL) decision/action was
#: required and the run could not deliver without it.
_NEEDS_HUMAN_CODES: frozenset[str] = frozenset({"harness.gate_creation_failed"})

#: error classes whose failure is a source/infra problem (elevated to failed).
_SOURCE_ERROR_CLASSES: frozenset[str] = frozenset(
    {
        "sandbox",
        "harness",
        "node",
        "connector",
        "capacity",
        "config",
        "contract",
        "run",
        "eval",
    }
)

#: Decision-table status buckets (FAR-189 spec §6), expressed as named sets so
#: the classifier never compares against raw status literals (the
#: ``raw-status-complete`` semgrep rule routes status checks through the shared
#: status sets until the FAR-146 success-predicate lands).
_EXCLUDED_STATUSES: frozenset[str] = frozenset({"cancelled", "budget_exceeded"})
_COUNTABLE_NO_DELIVERY_STATUSES: frozenset[str] = frozenset({"failed", "eval_failed", "stalled"})


class RunClassificationValue(StrEnum):
    """The run-outcome classification values (FAR-189 spec §7)."""

    delivered = "delivered"
    no_delivery = "no_delivery"
    excluded = "excluded"
    unclassified = "unclassified"


@dataclass(frozen=True)
class ClassificationResult:
    """One classification verdict + its supporting evidence.

    ``delivered_pr_urls`` is the deduplicated, validated set of PR urls found
    in node returns and/or raw-output markers. ``work_intact`` and
    ``declared_success_nodes`` are recorded as metadata so the record surfaces
    the terminalization facts the verdict derives from (FAR-189 spec §1).
    """

    value: RunClassificationValue
    reason: str
    delivered_pr_urls: tuple[str, ...] = ()
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    work_intact: bool | None = None
    declared_success_nodes: int = 0

    def to_json(self) -> str:
        """The persisted record shape ``{value, reason, delivered_pr_urls,
        computed_at, work_intact, declared_success_nodes}``."""
        return json.dumps(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value.value,
            "reason": self.reason,
            "delivered_pr_urls": list(self.delivered_pr_urls),
            "computed_at": self.computed_at.isoformat(),
            "work_intact": self.work_intact,
            "declared_success_nodes": self.declared_success_nodes,
        }


# --- pr_url extraction -----------------------------------------------------


def _is_valid_pr_url(url: str) -> bool:
    """Spec validity: ``urlsplit`` parses it with scheme http/https AND a
    non-empty netloc. ``https://`` (empty netloc) and ``ftp://`` are invalid.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _extract_pr_url_from_node(node_value: Any) -> str:
    """The first VALID ``pr_url`` anywhere in a node's stored return.

    Reuses the node-return accessor value (``node_output_split.node_return``)
    and walks the envelope shapes it can carry — direct output_json
    (sandbox_agent P1 rows), the legacy ``{"output": ...}`` envelope, and
    ``artifacts[*].output[.output_json]`` — to depth ``_MAX_PR_URL_SCAN_DEPTH``.
    Invalid strings under a ``pr_url`` key are skipped (a run is only
    delivered by a url that parses).
    """
    if not isinstance(node_value, dict):
        return ""
    stack: list[dict[str, Any]] = [node_value]
    seen: set[int] = set()
    for _ in range(_MAX_PR_URL_SCAN_DEPTH):
        nxt: list[dict[str, Any]] = []
        for item in stack:
            if id(item) in seen:
                continue
            seen.add(id(item))
            raw = item.get("pr_url")
            if isinstance(raw, str) and _is_valid_pr_url(raw):
                return raw.strip()
            for _key, value in item.items():
                if isinstance(value, dict):
                    nxt.append(value)
                elif isinstance(value, list):
                    nxt.extend(v for v in value if isinstance(v, dict))
        if not nxt:
            break
        stack = nxt
    return ""


def collect_pr_urls(
    outputs_json: Any,
    telemetry_json: Any,
    raw_output_markers: Any,
    node_ids: Iterable[str] | None = None,
) -> list[str]:
    """Every valid pr_url across the run's delivery evidence, deduplicated.

    Sources: (1) each node's stored return (via ``node_output_split.node_return``
    — legacy-safe), and (2) every FAR-188 raw-output marker's ``pr_url`` field,
    keyed by ANY attempt_key (a first-attempt PR created before a sandbox
    stall/retry is a real delivery — FAR-189 addendum).
    """
    seen: set[str] = set()
    urls: list[str] = []
    node_id_set: set[str] = set()
    if isinstance(outputs_json, dict):
        node_id_set.update(str(k) for k in outputs_json)
    if isinstance(telemetry_json, dict):
        node_id_set.update(str(k) for k in telemetry_json)
    if node_ids:
        node_id_set.update(str(n) for n in node_ids)

    for node_id in sorted(node_id_set):
        url = _extract_pr_url_from_node(node_return(outputs_json, telemetry_json, node_id))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    if isinstance(raw_output_markers, dict):
        for marker in raw_output_markers.values():
            if not isinstance(marker, dict):
                continue
            marker_url = marker.get("pr_url")
            if isinstance(marker_url, str) and marker_url.strip() and marker_url.strip() not in seen:
                if not _is_valid_pr_url(marker_url):
                    continue
                seen.add(marker_url.strip())
                urls.append(marker_url.strip())
    return urls


# --- terminalization-fact reuse --------------------------------------------


def _count_declared_success_nodes(telemetry_json: Any, outputs_json: Any) -> int:
    """How many stored nodes declared ``completed``/``success`` — the
    ``evidence._inner_declared_success`` gate over the legacy-safe
    ``node_telemetry`` accessor (mirrors ``evidence._declared_success_nodes``).
    """
    node_ids: set[str] = set()
    if isinstance(outputs_json, dict):
        node_ids.update(str(k) for k in outputs_json)
    if isinstance(telemetry_json, dict):
        node_ids.update(str(k) for k in telemetry_json)
    return sum(1 for nid in node_ids if _inner_declared_success(node_telemetry(telemetry_json, outputs_json, nid)))


def _any_marker_parse_error(raw_output_markers: Any) -> bool:
    """True when any FAR-188 marker carries a non-empty ``parse_error``
    (the run's output.json failed to parse — a parse-error no-delivery)."""
    if not isinstance(raw_output_markers, dict):
        return False
    for marker in raw_output_markers.values():
        if not isinstance(marker, dict):
            continue
        parse_error = marker.get("parse_error")
        if isinstance(parse_error, str) and parse_error:
            return True
    return False


def _derive_no_delivery_reason(
    error_code: str | None,
    raw_output_markers: Any,
) -> str:
    """Reason for a ``no_delivery`` verdict: ``parse_error`` / ``needs_human`` /
    ``source_error`` when derivable, else ``no_delivery`` (spec §7). Only called
    for the countable statuses (failed/eval_failed/stalled) — the complete-no-PR
    verdict sets ``no_work`` directly in :func:`classify_run`.
    """
    if _any_marker_parse_error(raw_output_markers):
        return REASON_PARSE_ERROR
    code = (error_code or "").strip().lower()
    if code and (code in _NEEDS_HUMAN_CODES or any(marker in code for marker in _NEEDS_HUMAN_CODE_SUBSTRINGS)):
        return REASON_NEEDS_HUMAN
    if code:
        try:
            from modulo.core.pipeline_engine.error_codes import class_for

            error_class = class_for(code)
        except Exception:
            error_class = None
        if error_class in _SOURCE_ERROR_CLASSES:
            return REASON_SOURCE_ERROR
    return REASON_NO_DELIVERY


# --- the pure classifier ----------------------------------------------------


def classify_run(
    status: str,
    error_code: str | None,
    *,
    outputs_json: Any = None,
    telemetry_json: Any = None,
    raw_output_markers: Any = None,
    node_ids: Iterable[str] | None = None,
    work_intact: bool | None = None,
) -> ClassificationResult:
    """The decision table (FAR-189 spec §6) — pure and unit-testable.

    Keyed on ``status``, never prose. ``error_code`` only refines the reason.
    """
    computed_at = datetime.now(UTC)
    declared_success_nodes = _count_declared_success_nodes(telemetry_json, outputs_json)

    # operator/HITL-cancelled + budget_exceeded -> EXCLUDED. A cancelled run is
    # never countable, even with an unparseable reason; budget_exceeded is
    # excluded and breaks the FAR-190 walk.
    if status in _EXCLUDED_STATUSES:
        reason = REASON_CANCELLED if status == "cancelled" else REASON_BUDGET_EXCEEDED
        return ClassificationResult(
            RunClassificationValue.excluded,
            reason,
            computed_at=computed_at,
            work_intact=work_intact,
            declared_success_nodes=declared_success_nodes,
        )

    # failed / eval_failed / stalled -> COUNTABLE no_delivery. An infra/sandbox
    # crash elevated to failed (e.g. error_code=node_cancelled) COUNTS (PO
    # decision).
    if status in _COUNTABLE_NO_DELIVERY_STATUSES:
        return ClassificationResult(
            RunClassificationValue.no_delivery,
            _derive_no_delivery_reason(error_code, raw_output_markers),
            computed_at=computed_at,
            work_intact=work_intact,
            declared_success_nodes=declared_success_nodes,
        )

    # complete -> delivered iff >= 1 valid pr_url (from node returns or
    # raw_output_markers); else COUNTABLE no_delivery (empty-backlog, PO).
    # This is the DELIVERABLE fall-through: the only terminal status left in the
    # ck_runs_status CHECK set after the excluded/countable buckets is
    # ``complete`` — so the path never needs a literal status comparison.
    if status not in TERMINAL_STATUSES:
        # Non-terminal / unrecognized status — guard. The hook only fires for
        # terminal statuses, so this protects against a mis-wired caller.
        return ClassificationResult(
            RunClassificationValue.excluded,
            f"unrecognized_status:{status}",
            computed_at=computed_at,
            work_intact=work_intact,
            declared_success_nodes=declared_success_nodes,
        )
    pr_urls = collect_pr_urls(outputs_json, telemetry_json, raw_output_markers, node_ids)
    if pr_urls:
        return ClassificationResult(
            RunClassificationValue.delivered,
            REASON_DELIVERED,
            delivered_pr_urls=tuple(pr_urls),
            computed_at=computed_at,
            work_intact=work_intact,
            declared_success_nodes=declared_success_nodes,
        )
    return ClassificationResult(
        RunClassificationValue.no_delivery,
        REASON_NO_WORK,
        computed_at=computed_at,
        work_intact=work_intact,
        declared_success_nodes=declared_success_nodes,
    )


# --- persistence ------------------------------------------------------------


async def persist_classification(
    session: AsyncSession,
    run: Any,
    result: ClassificationResult,
) -> bool:
    """Upsert the classification record for a run — UNIQUE(run_id) + refresh.

    ``run_id`` is the runs primary key, so the record can never duplicate. The
    write is a refresh (upsert semantics): a re-terminalization (retry policy
    re-flips a classified run back to pending, then re-runs with new evidence)
    overwrites the stale verdict with the freshly-computed one.

    Best-effort and NEVER raises: the write runs in a nested savepoint so a
    failure rolls back ONLY the classification write and never the caller's
    terminal status transition (spec: classifier failure must never block
    terminalization). Returns True when the record landed. Uses an ORM
    ``update`` statement (not raw text) so the ``Uuid`` PK and JSON column type
    conversions apply on every backend (a raw ``str(uuid)`` bind silently
    matches 0 rows on SQLite's CHAR(32) storage). Note the write deliberately
    bypasses the ORM identity map, so callers that need the fresh value must
    re-read the column explicitly (``await session.refresh(run, ["run_classification"])``).
    """
    try:
        async with session.begin_nested():
            await session.execute(update(Run).where(Run.id == run.id).values(run_classification=result.to_dict()))
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("classification.persist_failed run=%s", run.id)
        return False


async def classify_and_persist_run(
    session: AsyncSession,
    run: Any,
) -> bool:
    """Best-effort classification hook for a terminal write — NEVER raises.

    Computes the verdict from the run row's EXISTING terminalization facts
    (status, error_code, outputs_json, node_telemetry_json,
    raw_output_markers, work_intact) and persists it atomically in the
    caller's transaction. On ANY classifier failure an ``unclassified`` marker
    is written instead — the record is NEVER skipped, so the FAR-190 walk
    stays fail-closed (a missing record breaks the walk; the marker is what
    keeps it alive). Returns True when a record is present afterwards.
    """
    if run.status not in TERMINAL_STATUSES:
        return False
    try:
        result = classify_run(
            run.status,
            run.error_code,
            outputs_json=run.outputs_json,
            telemetry_json=run.node_telemetry_json,
            raw_output_markers=run.raw_output_markers,
            node_ids=None,
            # work_intact is a DB column written by the executor via a fenced
            # raw UPDATE (migration 0091) and NOT mapped on the ORM model —
            # read it defensively and record it as metadata when present.
            work_intact=getattr(run, "work_intact", None),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("classification.classify_failed run=%s", run.id)
        result = ClassificationResult(
            RunClassificationValue.unclassified,
            REASON_UNCLASSIFIED,
            computed_at=datetime.now(UTC),
        )
    try:
        return await persist_classification(session, run, result)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Defense-in-depth: persist_classification already swallows its own
        # errors, but a misbehaving write must STILL never block terminalization.
        _log.exception("classification.persist_failed run=%s", run.id)
        return False


# --- reconciliation sweep ---------------------------------------------------


async def reconcile_missing_classifications(
    session_factory: Callable[[], AsyncSession],
    *,
    org_ids: Iterable[UUID] | None = None,
    max_runs: int = 50,
    budget_seconds: float = 30.0,
) -> dict[str, int]:
    """Bounded backfill for terminal runs that missed the inline hook.

    Belt-and-braces for the terminalizers that write ``status='failed'``
    directly (cron_helpers dispatcher_reconcile / stale-run sweep, the SAQ
    task_failure writer) and for the crash-after-commit window. Mirrors
    ``evidence.reconcile_noop_evidence``: opportunistic, bounded, never raises
    on a single run.

    RLS: with *org_ids* the sweep processes each org under its own RLS context
    (cross-org). With None it runs in the caller's context (a single-org caller,
    or a privileged/owner-role factory that bypasses RLS — the evidence-sweep
    precedent).

    Returns ``{"scanned", "classified", "unclassified", "errors"}``.
    """
    from sqlalchemy import select

    from modulo.db.models.run import Run
    from modulo.db.rls import set_rls_org

    summary: dict[str, int] = {"scanned": 0, "classified": 0, "unclassified": 0, "errors": 0}
    deadline = time.monotonic() + budget_seconds
    scopes: Iterable[UUID | None] = [None] if org_ids is None else list(org_ids)

    for org_id in scopes:
        if time.monotonic() > deadline:
            break
        async with session_factory() as session, session.begin():
            if org_id is not None:
                await set_rls_org(session, org_id)
            result = await session.execute(
                select(Run)
                .where(Run.status.in_(sorted(TERMINAL_STATUSES)), Run.run_classification.is_(None))
                .order_by(Run.completed_at.desc())
                .limit(max_runs)
            )
            runs = list(result.scalars().all())

        for run in runs:
            summary["scanned"] += 1
            if time.monotonic() > deadline:
                break
            try:
                async with session_factory() as session, session.begin():
                    if org_id is not None:
                        await set_rls_org(session, org_id)
                    fresh = (
                        await session.execute(
                            select(Run).where(Run.id == run.id).execution_options(populate_existing=True)
                        )
                    ).scalar_one_or_none()
                    if fresh is None or fresh.run_classification is not None:
                        # Already classified (or row gone) — idempotent skip.
                        continue
                    await classify_and_persist_run(session, fresh)
                    # The classification write bypasses the ORM identity map
                    # (a separate UPDATE) — re-read the column to count the verdict.
                    await session.refresh(fresh, ["run_classification"])
                    if fresh.run_classification is not None:
                        value = str(fresh.run_classification.get("value") or "unclassified")
                        if value == "unclassified":
                            summary["unclassified"] += 1
                        else:
                            # delivered / no_delivery / excluded — any real record.
                            summary["classified"] += 1
                    else:
                        summary["errors"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                summary["errors"] += 1
                _log.exception("classification.sweep_failed run=%s", run.id)
    return summary
