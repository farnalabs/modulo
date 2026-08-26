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
  ``pr_url`` of a delivered run is recovered from real node output — from BOTH
  the node return (outputs_json) and the node telemetry VALUE (a pr_url buried
  in node_telemetry_json is a real delivery signal too).
* ``evidence._declared_success_nodes`` counts declared-success nodes (recorded
  as metadata) without re-deriving the split/legacy shapes.
* ``runs.raw_output_markers`` supplies the FAR-188 ``pr_url`` per attempt_key —
  a pr_url recovered from ANY attempt key is a valid delivery signal
  (first-attempt PRs created before a sandbox stall/retry are real deliveries).

Decision table (spec, keyed on status — never prose):

| status          | outcome                                        |
|-----------------|------------------------------------------------|
| cancelled       | ``excluded`` (operator/HITL-cancelled — never countable, even with an unparseable reason) |
| budget_exceeded | ``excluded`` (and breaks the FAR-190 walk)      |
| router_no_match | ``excluded`` (FAR-415 — its own reason, never budget_exceeded) |
| failed / eval_failed / stalled | ``no_delivery`` (COUNTABLE — infra/sandbox crash elevated to failed counts, PO) |
| complete        | ``delivered`` iff >= 1 valid ``pr_url`` OR any marker carries ``delivery_done`` (FAR-228 |
|                 | email sentinel); else COUNTABLE ``no_delivery`` (empty-backlog, PO) |
| (other terminal)| ``excluded`` FAIL-SAFE — a NEW terminal status added to ``TERMINAL_STATUSES``
|                 | hits this branch loudly instead of silently inheriting ``complete`` semantics |
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

Terminalizers that write ``status='failed'`` via RAW SQL (never touching the
crud/run.py hook) leave ``run_classification = NULL`` forever — those runs are
covered by the reconciliation sweep (:func:`reconcile_missing_classifications`),
which is WIRED into a periodic production path (cron_helpers'
``dispatcher_reconcile``, every 60s) so the gap closes within a minute.

Module shape: the pure classifier + types live in a DB-free section at the top
(unit-testable without a database); the persistence layer and the reconciliation
sweep scope their DB imports to the functions that need them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from modulo.core.node_output_split import node_return, node_telemetry
from modulo.core.pipeline_engine.evidence import _declared_success_nodes

_log = logging.getLogger(__name__)

#: Reasons stored on the record (spec §"Store the reason too").
REASON_NO_WORK = "no_work"
REASON_NEEDS_HUMAN = "needs_human"
REASON_SOURCE_ERROR = "source_error"
REASON_PARSE_ERROR = "parse_error"
REASON_NO_DELIVERY = "no_delivery"
REASON_CANCELLED = "operator_or_hitl_cancelled"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_ROUTER_NO_MATCH = "router_no_match"
REASON_DELIVERED = "pr_delivered"
REASON_DELIVERED_EMAIL = "email_delivered"
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
        "script",
        "harness",
        "node",
        "connector",
        "provider",
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
_EXCLUDED_STATUSES: frozenset[str] = frozenset({"cancelled", "budget_exceeded", "router_no_match"})
_COUNTABLE_NO_DELIVERY_STATUSES: frozenset[str] = frozenset({"failed", "eval_failed", "stalled"})
#: The deliverable verdict bucket — the ONLY status that may produce
#: ``delivered``. Named (not a raw ``status == "complete"`` literal) so the
#: decision table routes through a shared status set, matching the
#: ``raw-status-complete`` semgrep rule's intent.
_DELIVERABLE_STATUSES: frozenset[str] = frozenset({"complete"})

#: Hard statement timeout for the classification persist + sweep re-reads
#: (FAR-188 precedent): a hung DB must never block terminalization indefinitely.
_CLASSIFICATION_WRITE_TIMEOUT_SECONDS = 5.0


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

    def to_dict(self) -> dict[str, Any]:
        """The persisted record shape ``{value, reason, delivered_pr_urls,
        computed_at, work_intact, declared_success_nodes}``."""
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


def _child_dicts(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Every immediate dict-valued descendant of *item* (list elements included)."""
    nested: list[dict[str, Any]] = []
    for value in item.values():
        if isinstance(value, dict):
            nested.append(value)
        elif isinstance(value, list):
            nested.extend(v for v in value if isinstance(v, dict))
    return nested


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
            nxt.extend(_child_dicts(item))
        if not nxt:
            break
        stack = nxt
    return ""


def _node_id_union(outputs_json: Any, telemetry_json: Any) -> set[str]:
    """Every node id keyed in either per-node column (deduplicated)."""
    node_ids: set[str] = set()
    if isinstance(outputs_json, dict):
        node_ids.update(str(k) for k in outputs_json)
    if isinstance(telemetry_json, dict):
        node_ids.update(str(k) for k in telemetry_json)
    return node_ids


def _collect_node_run_pr_urls(
    outputs_json: Any,
    telemetry_json: Any,
    seen: set[str],
    urls: list[str],
) -> None:
    """Collect valid pr_urls from each node's stored return + telemetry value."""
    for node_id in sorted(_node_id_union(outputs_json, telemetry_json)):
        url = _extract_pr_url_from_node(node_return(outputs_json, telemetry_json, node_id))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
        telemetry_value = node_telemetry(telemetry_json, outputs_json, node_id)
        if telemetry_value is not None:
            telemetry_url = _extract_pr_url_from_node(telemetry_value)
            if telemetry_url and telemetry_url not in seen:
                seen.add(telemetry_url)
                urls.append(telemetry_url)


def _collect_marker_pr_url(marker: Any, seen: set[str], urls: list[str]) -> None:
    """Collect a single FAR-188 raw-output marker's ``pr_url`` if valid + unseen."""
    if not isinstance(marker, dict):
        return
    marker_url = marker.get("pr_url")
    if not isinstance(marker_url, str) or not marker_url.strip():
        return
    stripped = marker_url.strip()
    if not _is_valid_pr_url(stripped):
        return
    if stripped not in seen:
        seen.add(stripped)
        urls.append(stripped)


def _collect_marker_pr_urls(raw_output_markers: Any, seen: set[str], urls: list[str]) -> None:
    """Collect valid pr_urls keyed by ANY attempt-key in the run's markers."""
    if not isinstance(raw_output_markers, dict):
        return
    for marker in raw_output_markers.values():
        _collect_marker_pr_url(marker, seen, urls)


def collect_pr_urls(
    outputs_json: Any,
    telemetry_json: Any,
    raw_output_markers: Any,
) -> list[str]:
    """Every valid pr_url across the run's delivery evidence, deduplicated.

    Sources: (1) each node's stored return (via ``node_output_split.node_return``
    — legacy-safe), (2) each node's telemetry VALUE (via
    ``node_output_split.node_telemetry`` — a pr_url carried only in
    ``node_telemetry_json`` is a real delivery signal), and (3) every FAR-188
    raw-output marker's ``pr_url`` field, keyed by ANY attempt_key (a
    first-attempt PR created before a sandbox stall/retry is a real delivery —
    FAR-189 addendum).
    """
    seen: set[str] = set()
    urls: list[str] = []

    _collect_node_run_pr_urls(outputs_json, telemetry_json, seen, urls)
    _collect_marker_pr_urls(raw_output_markers, seen, urls)
    return urls


# --- terminalization-fact reuse --------------------------------------------


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


def _any_marker_delivery_done(raw_output_markers: Any) -> bool:
    """True when any FAR-228 marker carries ``delivery_done is True`` — the
    run's side-effecting delivery (e.g. an email) was made even though the node
    later failed/retried. Reads the marker column directly — never outputs_json
    subscripts (structurally wrong). Deliberately UNGATED by the kill-switch:
    classification records the delivered fact regardless of gate state."""
    if not isinstance(raw_output_markers, dict):
        return False
    for marker in raw_output_markers.values():
        if not isinstance(marker, dict):
            continue
        if marker.get("delivery_done") is True:
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
    raw_code = (error_code or "").strip()
    code = raw_code.lower()
    if not code:
        return REASON_NO_DELIVERY
    if code in _NEEDS_HUMAN_CODES or any(marker in code for marker in _NEEDS_HUMAN_CODE_SUBSTRINGS):
        return REASON_NEEDS_HUMAN
    try:
        from modulo.core.pipeline_engine.error_codes import class_for

        error_class = class_for(raw_code)
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
    work_intact: bool | None = None,
) -> ClassificationResult:
    """The decision table (FAR-189 spec §6) — pure and unit-testable.

    Keyed on ``status``, never prose. ``error_code`` only refines the reason.
    An explicit ``if status == "complete"`` branch owns the deliverable
    verdict; ANY terminal status outside the excluded/countable buckets AND not
    ``complete`` classifies as ``excluded`` — a new terminal status added to
    ``TERMINAL_STATUSES`` fails loudly here instead of silently inheriting
    ``complete`` semantics.

    For ``complete``: the run is ``delivered`` iff it has a valid ``pr_url`` OR
    any raw-output marker carries ``delivery_done`` (FAR-228 — a side-effecting
    delivery, e.g. an email, recorded even though the node later failed/retried).
    """
    from modulo.db.models.run import TERMINAL_STATUSES

    computed_at = datetime.now(UTC)
    declared_success_nodes = len(_declared_success_nodes(outputs_json, telemetry_json))

    # operator/HITL-cancelled + budget_exceeded + router_no_match -> EXCLUDED. A
    # cancelled run is never countable, even with an unparseable reason;
    # budget_exceeded is excluded and breaks the FAR-190 walk. Each excluded
    # status keeps its own reason so analytics/reporting never mislabels a
    # router_no_match run as a budget attribution (FAR-415).
    if status in _EXCLUDED_STATUSES:
        if status == "cancelled":
            reason = REASON_CANCELLED
        elif status == "router_no_match":
            reason = REASON_ROUTER_NO_MATCH
        else:
            reason = REASON_BUDGET_EXCEEDED
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

    # Non-terminal / unrecognized status — guard. The hook only fires for
    # terminal statuses, so this protects against a mis-wired caller.
    if status not in TERMINAL_STATUSES:
        return ClassificationResult(
            RunClassificationValue.excluded,
            f"unrecognized_status:{status}",
            computed_at=computed_at,
            work_intact=work_intact,
            declared_success_nodes=declared_success_nodes,
        )

    # complete -> delivered iff >= 1 valid pr_url (from node returns, node
    # telemetry values, or raw_output_markers) OR any raw-output marker carries
    # delivery_done (FAR-228 email sentinel); else COUNTABLE no_delivery
    # (empty-backlog, PO). Explicit branch — the deliverable must never be
    # reached via set-arithmetic fall-through.
    if status in _DELIVERABLE_STATUSES:
        pr_urls = collect_pr_urls(outputs_json, telemetry_json, raw_output_markers)
        if pr_urls:
            return ClassificationResult(
                RunClassificationValue.delivered,
                REASON_DELIVERED,
                delivered_pr_urls=tuple(pr_urls),
                computed_at=computed_at,
                work_intact=work_intact,
                declared_success_nodes=declared_success_nodes,
            )
        # FAR-228: a delivered fact recorded on the marker is a real delivery
        # even when the node then failed/retried (pr_url still wins above).
        if _any_marker_delivery_done(raw_output_markers):
            return ClassificationResult(
                RunClassificationValue.delivered,
                REASON_DELIVERED_EMAIL,
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

    # Fail-safe: a terminal status that is neither excluded, countable, nor
    # 'complete' (a NEW status added to TERMINAL_STATUSES) classifies as
    # excluded — loud in tests, never a silent complete.
    return ClassificationResult(
        RunClassificationValue.excluded,
        f"unrecognized_status:{status}",
        computed_at=computed_at,
        work_intact=work_intact,
        declared_success_nodes=declared_success_nodes,
    )


# --- persistence ------------------------------------------------------------
# DB imports are scoped to the persistence functions so the pure classifier
# section above stays database-free.


_classification_failures_counter: Any = None


def _record_classification_failure(failure: str) -> None:
    """Best-effort OTel counter for classification failures (FIX 11).

    Lazily registered and never raises: a systematic classify/persist failure
    must be dashboard-visible without ever breaking terminalization. The
    counter lives here (not in ``modulo.core.error_tracking.metrics``) so this
    module's failure rate is observable without coupling the two modules.
    """
    global _classification_failures_counter
    try:
        if _classification_failures_counter is None:
            from opentelemetry import metrics as _otel_metrics

            provider = _otel_metrics.get_meter_provider()
            if provider is None:
                return
            _classification_failures_counter = provider.get_meter(
                "modulo.pipeline_engine", version="0.1.0"
            ).create_counter(
                name="runs_classification_failures_total",
                description="Run-outcome classification failures, by failure type",
                unit="1",
            )
        _classification_failures_counter.add(1, {"failure": failure})
    except Exception:
        _log.debug("classification.metrics_unavailable", exc_info=True)


def _unclassified_marker_dict(reason: str = REASON_UNCLASSIFIED) -> dict[str, Any]:
    """The persisted ``unclassified`` record shape — the fail-closed marker."""
    return {
        "value": RunClassificationValue.unclassified.value,
        "reason": reason,
        "delivered_pr_urls": [],
        "computed_at": datetime.now(UTC).isoformat(),
        "work_intact": None,
        "declared_success_nodes": 0,
    }


async def _write_unclassified_marker(
    session: Any,
    run: Any,
    *,
    expected_status: str | None = None,
) -> bool:
    """Fail-closed fallback: write the ``unclassified`` marker directly.

    Used when the normal persist failed (exception, timeout, or 0 rows). The
    write is a simple guarded UPDATE — deliberately independent of the
    classifier — so a terminal run NEVER commits without a record (a missing
    record breaks the FAR-190 walk; the marker is what keeps it alive).
    Best-effort and NEVER raises. Returns True when a row was written.
    """
    from sqlalchemy import update

    from modulo.db.models.run import Run

    stmt = update(Run).where(Run.id == run.id)
    if expected_status is not None:
        stmt = stmt.where(Run.status == expected_status)

    async def _do() -> Any:
        async with session.begin_nested():
            return await session.execute(stmt.values(run_classification=_unclassified_marker_dict()))

    try:
        res = await asyncio.wait_for(_do(), timeout=_CLASSIFICATION_WRITE_TIMEOUT_SECONDS)
        return res.rowcount is not None and res.rowcount > 0
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("classification.marker_fallback_failed run=%s", run.id)
        _record_classification_failure("marker_fallback_failed")
        return False


async def persist_classification(
    session: Any,
    run: Any,
    result: ClassificationResult,
    *,
    expected_status: str | None = None,
) -> bool:
    """Upsert the classification record for a run — UNIQUE(run_id) + refresh.

    ``run_id`` is the runs primary key, so the record can never duplicate. The
    write is a refresh (upsert semantics): a re-terminalization (retry policy
    re-flips a classified run back to pending, then re-runs with new evidence)
    overwrites the stale verdict with the freshly-computed one.

    *expected_status* guards the write: when given, the UPDATE only matches a
    row whose ``status`` equals it, so a stale verdict (a sweep re-read a run
    whose status has since moved) can never overwrite a fresher record. When
    the guard rejects the write (0 rows) the method returns ``False`` — it
    NEVER reports success on a no-op.

    Best-effort and NEVER raises: the write runs in a nested savepoint so a
    failure rolls back ONLY the classification write and never the caller's
    terminal status transition (spec: classifier failure must never block
    terminalization). The statement is bounded by
    ``_CLASSIFICATION_WRITE_TIMEOUT_SECONDS`` so a hung DB cannot block
    terminalization indefinitely. Returns True only when exactly one row
    landed. Uses an ORM ``update`` statement (not raw text) so the ``Uuid`` PK
    and JSON column type conversions apply on every backend (a raw
    ``str(uuid)`` bind silently matches 0 rows on SQLite's CHAR(32) storage).
    Note the write deliberately bypasses the ORM identity map, so callers that
    need the fresh value must re-read the column explicitly
    (``await session.refresh(run, ["run_classification"])``).
    """
    from sqlalchemy import update

    from modulo.db.models.run import Run

    stmt = update(Run).where(Run.id == run.id)
    if expected_status is not None:
        stmt = stmt.where(Run.status == expected_status)

    async def _persist() -> Any:
        async with session.begin_nested():
            return await session.execute(stmt.values(run_classification=result.to_dict()))

    try:
        res = await asyncio.wait_for(_persist(), timeout=_CLASSIFICATION_WRITE_TIMEOUT_SECONDS)
        ok = res.rowcount is not None and res.rowcount > 0
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _log.exception("classification.persist_timeout run=%s", run.id)
        _record_classification_failure("persist_timeout")
        return False
    except Exception:
        _log.exception("classification.persist_failed run=%s", run.id)
        _record_classification_failure("persist_failed")
        return False
    if not ok:
        # 0 rows: the status guard rejected the write (concurrent
        # re-terminalization / RLS-filtered row) — never report success.
        _log.warning("classification.persist_zero_rows run=%s expected_status=%s", run.id, expected_status)
        _record_classification_failure("persist_zero_rows")
    return ok


async def classify_and_persist_run(
    session: Any,
    run: Any,
) -> bool:
    """Best-effort classification hook for a terminal write — NEVER raises.

    Computes the verdict from the run row's EXISTING terminalization facts
    (status, error_code, outputs_json, node_telemetry_json,
    raw_output_markers, work_intact) and persists it atomically in the
    caller's transaction. The pure classify computation runs OFF the event
    loop (``asyncio.to_thread``) so a fat-output run cannot stall the whole
    loop inside a terminalization transaction. On ANY classifier failure an
    ``unclassified`` marker is written instead — the record is NEVER skipped,
    so the FAR-190 walk stays fail-closed (a missing record breaks the walk;
    the marker is what keeps it alive). Returns True when a record is present
    afterwards.
    """
    from modulo.db.models.run import TERMINAL_STATUSES

    if run.status not in TERMINAL_STATUSES:
        return False
    try:
        result = await asyncio.to_thread(
            classify_run,
            run.status,
            run.error_code,
            outputs_json=run.outputs_json,
            telemetry_json=run.node_telemetry_json,
            raw_output_markers=run.raw_output_markers,
            work_intact=run.work_intact,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("classification.classify_failed run=%s", run.id)
        _record_classification_failure("classify_failed")
        result = ClassificationResult(
            RunClassificationValue.unclassified,
            REASON_UNCLASSIFIED,
            computed_at=datetime.now(UTC),
        )
    ok = await persist_classification(session, run, result, expected_status=run.status)
    if not ok:
        # Fail-closed: a terminal run must never commit with NO record. The
        # marker is a second, simpler write outside the failing path — if the
        # DB is genuinely unavailable it too fails and the ERROR is logged +
        # counted, but the terminal status write still commits.
        await _write_unclassified_marker(session, run, expected_status=run.status)
    return ok


# --- reconciliation sweep ---------------------------------------------------


async def _reconcile_classify_run(
    session_factory: Callable[[], Any],
    run: Any,
    org_id: UUID | None,
) -> str:
    """Re-read + classify a single terminal run, returning its summary bucket.

    Runs in a fresh transaction under the org's RLS context; locks the row
    (``with_for_update``) so a concurrent re-terminalization serializes behind
    the persist instead of racing it. Returns one of ``"classified"`` /
    ``"unclassified"`` / ``"errors"`` (or ``"skipped"`` when the row is gone or
    already classified) — matching ``reconcile_missing_classifications``'s
    summary keys.
    """
    from sqlalchemy import select

    from modulo.db.models.run import Run
    from modulo.db.rls import set_rls_org

    try:
        async with session_factory() as session, session.begin():
            if org_id is not None:
                await set_rls_org(session, org_id)
            fresh = (
                await asyncio.wait_for(
                    session.execute(
                        select(Run).where(Run.id == run.id).with_for_update().execution_options(populate_existing=True)
                    ),
                    timeout=_CLASSIFICATION_WRITE_TIMEOUT_SECONDS,
                )
            ).scalar_one_or_none()
            if fresh is None or fresh.run_classification is not None:
                # Already classified (or row gone) — idempotent skip.
                return "skipped"
            await classify_and_persist_run(session, fresh)
            # The classification write bypasses the ORM identity map (a separate
            # UPDATE) — re-read the column to count the verdict.
            await asyncio.wait_for(
                session.refresh(fresh, ["run_classification"]),
                timeout=_CLASSIFICATION_WRITE_TIMEOUT_SECONDS,
            )
            if fresh.run_classification is not None:
                value = str(fresh.run_classification.get("value") or RunClassificationValue.unclassified.value)
                if value == RunClassificationValue.unclassified.value:
                    return "unclassified"
                # delivered / no_delivery / excluded — any real record.
                return "classified"
            return "errors"
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _log.warning("classification.sweep_timeout run=%s", run.id)
        return "errors"
    except Exception:
        _log.exception("classification.sweep_failed run=%s", run.id)
        return "errors"


async def reconcile_missing_classifications(
    session_factory: Callable[[], Any],
    *,
    org_ids: Iterable[UUID] | None = None,
    max_runs: int = 50,
    budget_seconds: float = 30.0,
) -> dict[str, int]:
    """Bounded backfill for terminal runs that missed the inline hook.

    Belt-and-braces for the terminalizers that write ``status='failed'``
    directly (cron_helpers dispatcher_reconcile / stale-run sweeps, the SAQ
    task_failure writer, pipeline_execution) and for the crash-after-commit
    window. WIRED into ``cron_helpers.dispatcher_reconcile`` (every 60s) — this
    is the periodic production path that closes the gap within a minute.

    TOCTOU-safe: the per-run re-read locks the row (``with_for_update``) and
    the persist is status-guarded (``expected_status``), so a stale verdict can
    never overwrite a fresh record written by a concurrent re-terminalization.

    RLS: with *org_ids* the sweep processes each org under its own RLS context
    (cross-org). With None it runs in the caller's context (a single-org caller,
    or a modulo_system role factory that bypasses RLS — the dispatcher
    wiring runs system-scoped, modulo_system BYPASSRLS cross-org like the other
    system crons).

    Returns ``{"scanned", "classified", "unclassified", "errors"}``.
    """
    from sqlalchemy import select

    from modulo.db.models.run import TERMINAL_STATUSES, Run
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
            verdict = await _reconcile_classify_run(session_factory, run, org_id)
            if verdict in ("classified", "unclassified", "errors"):
                summary[verdict] += 1
    return summary
