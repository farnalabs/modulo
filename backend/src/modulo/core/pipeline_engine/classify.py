"""Run-outcome classification persisted at terminalization (FAR-189).

Stage 2 of the ongoing-trigger no-delivery auto-deactivation feature. FAR-188
added the raw-output retention markers (JSONB keyed by attempt_key, each
marker carrying ``pr_url`` — today ``run_node_outputs`` marker ROWS, the
reassembly reader's store leg; until migration 0215 dropped the legacy runs
columns they lived inside ``runs.raw_output_markers``); the streak engine
(FAR-190) will query classification records instead of raw run status.
THIS module computes and persists a classification record when a run reaches
a terminal status.

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
* The reassembled raw-output markers (``run_node_outputs`` marker rows
  — the store leg since B2c, reads via the reassembly reader) supply the
  FAR-188 ``pr_url`` per attempt_key —
  a pr_url recovered from ANY attempt key is a valid delivery signal
  (first-attempt PRs created before a sandbox stall/retry are real deliveries).

Decision table (spec, keyed on status — never prose):

| status          | outcome                                        |
|-----------------|------------------------------------------------|
| cancelled       | ``excluded`` (operator/HITL-cancelled — never countable, even with an unparseable |
|                 | reason); reason ``hitl_timeout`` when collected by the FAR-1257 review-window sweep |
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

Record shape: ``{value, reason, delivered_pr_urls, computed_at, work_intact,
declared_success_nodes, pr_url_provenance, delivery_confidence}``. The URLs in
``delivered_pr_urls`` are **self-reported and unverified** (FAR-1336): they are
harvested from the run's own output (the node's structured return, the node
telemetry value, the FAR-188 raw-output markers) and NOTHING cross-checks that
the run actually created the PR it names — the platform deliberately does not
verify deliveries against an SCM of record. ``pr_url_provenance`` records,
per URL, how it was harvested (``declared`` = the run's own output contract
asserted it; ``matched`` = it merely appears in emitted output), and
``delivery_confidence`` states plainly that every record written today is
``self_reported``. Neither key changes the verdict: a ``matched``-only URL
still classifies ``delivered`` (the delivery rule is unchanged). The eight-key
shape is forward-only: rows written before this change are six-key, are never
backfilled, and readers must treat an absent ``pr_url_provenance`` /
``delivery_confidence`` key on an older row as legacy/unknown, not an error.

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
# FAR-1257: a ``cancelled`` run the HITL review-window terminalizer collected
# (dispatcher_reconcile, ``error_code``/``cancel_reason`` =
# ``hitl_review_expired`` -> canonical ``hitl.review_expired``). Still the SAME
# ``excluded`` verdict — never countable, never a delivery — but with its own
# reason so reporting can tell "a human let the review window lapse" apart from
# "an operator deliberately cancelled". No new ``runs.status`` was added for
# this; status stays ``cancelled``.
REASON_HITL_TIMEOUT = "hitl_timeout"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_COMPENSATION_FAILED = "compensation_failed"
REASON_ROUTER_NO_MATCH = "router_no_match"
REASON_DELIVERED = "pr_delivered"
REASON_DELIVERED_EMAIL = "email_delivered"
REASON_UNCLASSIFIED = "classifier_error"

# --- delivery-signal provenance vocabulary (FAR-1336) -----------------------
#
# ``runs.run_classification`` is the platform's authoritative "did this run
# deliver?" record, but its ``delivered_pr_urls`` are harvested from
# self-reported sources and nothing cross-checks them against the SCM. These
# vocabularies stop the record from overstating what it knows (FAR-1336) —
# they are ADDITIVE metadata only: they never change the verdict.

#: ``pr_url_provenance`` vocabulary — HOW each URL in ``delivered_pr_urls``
#: entered the record (one value per URL; see ``ClassificationResult``).
#:
#: * ``declared`` — the URL came from the node's structured RETURN
#:   (``outputs_json``): the run's own output contract asserting delivery.
#: * ``matched`` — the URL was found in the node telemetry VALUE or in a
#:   FAR-188 raw-output marker: it appears in emitted output, but the run
#:   never asserted it as a delivery.
PR_URL_PROVENANCE_DECLARED = "declared"
PR_URL_PROVENANCE_MATCHED = "matched"
#: The closed set of provenance values — a future route adds its value here.
PR_URL_PROVENANCE_VALUES: frozenset[str] = frozenset({PR_URL_PROVENANCE_DECLARED, PR_URL_PROVENANCE_MATCHED})

#: ``delivery_confidence`` vocabulary — how much of the record has been
#: confirmed against a source of truth (FAR-1336).
#:
#: * ``self_reported`` — the entire record is self-reported by the run's own
#:   output; NOTHING in it has been verified against a source of truth. Every
#:   record written today carries this value: the platform deliberately does
#:   NOT verify deliveries against GitHub/GitLab/etc. (that would require
#:   platform-side SCM integration for a signal only the agent knows).
#: * ``verified`` — reserved for a future value whose URLs have been confirmed
#:   against the source of truth. Declared here so the vocabulary has a home
#:   for it; no code path emits it yet.
DELIVERY_CONFIDENCE_SELF_REPORTED = "self_reported"
DELIVERY_CONFIDENCE_VERIFIED = "verified"
#: The closed set of delivery-confidence values.
DELIVERY_CONFIDENCE_VALUES: frozenset[str] = frozenset(
    {DELIVERY_CONFIDENCE_SELF_REPORTED, DELIVERY_CONFIDENCE_VERIFIED}
)

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
_COUNTABLE_NO_DELIVERY_STATUSES: frozenset[str] = frozenset({"failed", "eval_failed", "stalled", "compensation_failed"})
#: The deliverable verdict bucket — the ONLY status that may produce
#: ``delivered``. Named (not a raw ``status == "complete"`` literal) so the
#: decision table routes through a shared status set, matching the
#: ``raw-status-complete`` semgrep rule's intent.
_DELIVERABLE_STATUSES: frozenset[str] = frozenset({"complete"})

#: Hard statement timeout for the classification persist + sweep re-reads
#: (FAR-188 precedent): a hung DB must never block terminalization indefinitely.
#:
#: This is a STALL bound, not a performance budget. 5s was too tight on a
#: loaded CI runner: aiosqlite dispatches every statement through its own
#: worker thread, and when the runner is oversubscribed (the unit suite runs
#: under ``-n auto``) the ``connect`` / ``execute`` / ``close`` round-trips for
#: a trivial write can each be delayed for tens of seconds. The write itself
#: never hangs — it is queued behind the runner's CPU contention — so a tight
#: bound converts pure scheduling jitter into a spurious ``persist_timeout``
#: (and, in the reconcile sweep, a run that is left unclassified for that
#: tick). Raised to 30s so only a genuinely wedged DB trips it; the
#: terminalization path also waits on this bound, so it stays finite.
_CLASSIFICATION_WRITE_TIMEOUT_SECONDS = 30.0


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

    FAR-1336 provenance metadata (additive; the verdict is unaffected):

    ``pr_url_provenance`` maps every URL in ``delivered_pr_urls`` to
    ``PR_URL_PROVENANCE_DECLARED`` (it came from the node's structured RETURN
    — the run's own output contract asserting delivery) or
    ``PR_URL_PROVENANCE_MATCHED`` (it was found in the telemetry value or a
    FAR-188 raw-output marker — it appears in emitted output but was never
    asserted as a delivery). A URL reachable by both routes is ``declared``
    (the stronger provenance wins). Empty when ``delivered_pr_urls`` is empty.

    ``delivery_confidence`` is ``"self_reported"`` on every record written
    today: the URLs above are harvested from the run's own output and nothing
    cross-checks that the run actually created the PR it names — see
    ``DELIVERY_CONFIDENCE_VALUES`` for the vocabulary.
    """

    value: RunClassificationValue
    reason: str
    delivered_pr_urls: tuple[str, ...] = ()
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    work_intact: bool | None = None
    declared_success_nodes: int = 0
    pr_url_provenance: dict[str, str] = field(default_factory=dict)
    delivery_confidence: str = DELIVERY_CONFIDENCE_SELF_REPORTED

    def to_dict(self) -> dict[str, Any]:
        """The persisted record shape ``{value, reason, delivered_pr_urls,
        computed_at, work_intact, declared_success_nodes, pr_url_provenance,
        delivery_confidence}``.

        ``delivered_pr_urls`` and ``pr_url_provenance`` are self-reported and
        UNVERIFIED (FAR-1336) — ``delivery_confidence`` states that plainly.
        """
        return {
            "value": self.value.value,
            "reason": self.reason,
            "delivered_pr_urls": list(self.delivered_pr_urls),
            "computed_at": self.computed_at.isoformat(),
            "work_intact": self.work_intact,
            "declared_success_nodes": self.declared_success_nodes,
            "pr_url_provenance": dict(self.pr_url_provenance),
            "delivery_confidence": self.delivery_confidence,
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


def _note_provenance(provenance: dict[str, str], url: str, route: str) -> None:
    """Record how *url* entered the record — the STRONGER provenance wins.

    ``declared`` (the run's structured return asserted the URL) outranks
    ``matched`` (the URL merely appears in emitted output), so a URL found by
    BOTH routes keeps ``declared`` regardless of which route is seen first.
    Never raises; has no effect on URL collection or ordering.
    """
    if route == PR_URL_PROVENANCE_DECLARED or url not in provenance:
        provenance[url] = route


def _collect_node_run_pr_urls(
    outputs_json: Any,
    telemetry_json: Any,
    seen: set[str],
    urls: list[str],
    provenance: dict[str, str],
) -> None:
    """Collect valid pr_urls from each node's stored return + telemetry value.

    The node RETURN (``outputs_json``) is the run's own output contract, so a
    URL found there is ``declared``; the telemetry VALUE only carries the URL
    as emitted output, so it is ``matched``. Collection order (and therefore
    ``delivered_pr_urls`` ordering) is unchanged from the pre-FAR-1336 code.
    """
    for node_id in sorted(_node_id_union(outputs_json, telemetry_json)):
        url = _extract_pr_url_from_node(node_return(outputs_json, telemetry_json, node_id))
        if url:
            _note_provenance(provenance, url, PR_URL_PROVENANCE_DECLARED)
            if url not in seen:
                seen.add(url)
                urls.append(url)
        # KNOWN LIMITATION (FAR-1336, conservative): the accessor falls back to
        # the legacy INNER OUTPUT of ``outputs_json`` when the node has no
        # telemetry entry, so a URL only reachable down this leg gets labelled
        # ``matched`` even though its source is the node's return (the field
        # defines ``declared`` = came from the structured return). Attributing
        # by source would need the accessor's dispatch rule re-derived here (a
        # second source of truth that can drift) or a source tag from
        # ``node_output_split`` — the direction understates provenance, never
        # overstates it, so it is left as-is.
        telemetry_value = node_telemetry(telemetry_json, outputs_json, node_id)
        if telemetry_value is not None:
            telemetry_url = _extract_pr_url_from_node(telemetry_value)
            if telemetry_url:
                _note_provenance(provenance, telemetry_url, PR_URL_PROVENANCE_MATCHED)
                if telemetry_url not in seen:
                    seen.add(telemetry_url)
                    urls.append(telemetry_url)


def _collect_marker_pr_url(marker: Any, seen: set[str], urls: list[str], provenance: dict[str, str]) -> None:
    """Collect a single FAR-188 raw-output marker's ``pr_url`` if valid + unseen.

    A marker is emitted output the run never asserted as a delivery, so the
    URL it carries is ``matched`` (FAR-1336 provenance; ordering unchanged).
    """
    if not isinstance(marker, dict):
        return
    marker_url = marker.get("pr_url")
    if not isinstance(marker_url, str) or not marker_url.strip():
        return
    stripped = marker_url.strip()
    if not _is_valid_pr_url(stripped):
        return
    _note_provenance(provenance, stripped, PR_URL_PROVENANCE_MATCHED)
    if stripped not in seen:
        seen.add(stripped)
        urls.append(stripped)


def _collect_marker_pr_urls(
    raw_output_markers: Any, seen: set[str], urls: list[str], provenance: dict[str, str]
) -> None:
    """Collect valid pr_urls keyed by ANY attempt-key in the run's markers."""
    if not isinstance(raw_output_markers, dict):
        return
    for marker in raw_output_markers.values():
        _collect_marker_pr_url(marker, seen, urls, provenance)


def collect_pr_urls(
    outputs_json: Any,
    telemetry_json: Any,
    raw_output_markers: Any,
    provenance: dict[str, str] | None = None,
) -> list[str]:
    """Every valid pr_url across the run's delivery evidence, deduplicated.

    Sources: (1) each node's stored return (via ``node_output_split.node_return``
    — legacy-safe), (2) each node's telemetry VALUE (via
    ``node_output_split.node_telemetry`` — a pr_url carried only in
    ``node_telemetry_json`` is a real delivery signal), and (3) every FAR-188
    raw-output marker's ``pr_url`` field, keyed by ANY attempt_key (a
    first-attempt PR created before a sandbox stall/retry is a real delivery —
    FAR-189 addendum).

    FAR-1336: pass a *provenance* dict to have it filled, in the SAME single
    walk, with one entry per collected URL — ``declared`` (the node's
    structured RETURN asserted it) or ``matched`` (telemetry/marker output
    only). The map's keys are exactly the returned URL set — a caller-supplied
    dict is CLEARED first, so stale pre-seeded entries cannot survive — and
    collection order is identical whether or not *provenance* is given.
    """
    seen: set[str] = set()
    urls: list[str] = []
    prov: dict[str, str] = provenance if provenance is not None else {}
    # Make the documented invariant true: the map ends up keyed by EXACTLY the
    # returned URL set, so any pre-seeded entries a caller hands in are dropped
    # before the walk fills it (the sole production caller passes a fresh {}).
    prov.clear()

    _collect_node_run_pr_urls(outputs_json, telemetry_json, seen, urls, prov)
    _collect_marker_pr_urls(raw_output_markers, seen, urls, prov)
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


def _is_hitl_timeout(error_code: str | None, cancel_reason: str | None) -> bool:
    """True when a ``cancelled`` run was collected by the review-window sweep.

    FAR-1257. Either of the two stamps the terminalizer writes is sufficient,
    because a legacy row may carry only one of them:

    * ``runs.cancel_reason`` (the FAR-1233 WHY stamp) — a CLOSED vocabulary
      (``ck_runs_cancel_reason``), so it is compared against the literal
      ``hitl_review_expired`` directly. It is NOT an error code, and routing it
      through the error-code registry would emit a spurious
      ``harness.unknown.fallback`` signal for every ordinary
      ``user_requested``/``agent_requested`` cancel.
    * ``runs.error_code`` — canonicalised through ``error_codes`` to
      ``hitl.review_expired`` (both the raw ``hitl_review_expired`` alias and
      the canonical dotted spelling match). A ``"hitl"`` pre-filter keeps the
      registry call off the common operator-cancel codes entirely, so this
      reason never perturbs the unmapped-fallback signal stream.

    ``map_legacy_code`` (the code-level mapping) is used rather than
    ``class_for``: ``class_for`` returns the *error class* tag (``"hitl"``),
    not a canonical code, so it cannot express "this exact code" — and only the
    exact canonical code keeps a future sibling ``hitl.*`` code from being
    bucketed as a timeout.

    A registry failure degrades to "not a timeout" — the pre-FAR-1257
    ``operator_or_hitl_cancelled`` reason, never an exception out of the pure
    classifier.
    """
    if cancel_reason is not None and cancel_reason == "hitl_review_expired":
        return True
    if not error_code or "hitl" not in error_code.lower():
        return False
    try:
        from modulo.core.pipeline_engine.error_codes import map_legacy_code

        return map_legacy_code(error_code) == "hitl.review_expired"
    except Exception:
        return False


def classify_run(
    status: str,
    error_code: str | None,
    *,
    outputs_json: Any = None,
    telemetry_json: Any = None,
    raw_output_markers: Any = None,
    work_intact: bool | None = None,
    cancel_reason: str | None = None,
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

    ``cancel_reason`` (FAR-1257) refines ONLY the ``cancelled`` reason: a run
    the review-window terminalizer collected gets ``hitl_timeout`` instead of
    ``operator_or_hitl_cancelled``. The verdict stays ``excluded`` either way.
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
            # FAR-1257: discriminate a review-window timeout from a deliberate
            # operator/HITL cancel on the run's OWN terminalization facts (the
            # reason never changes the excluded verdict — see FAR-415's
            # sibling: one status, its own reason, no cross-bucket leakage).
            reason = REASON_HITL_TIMEOUT if _is_hitl_timeout(error_code, cancel_reason) else REASON_CANCELLED
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

    # failed / eval_failed / stalled / compensation_failed -> COUNTABLE
    # no_delivery. An infra/sandbox crash elevated to failed (e.g.
    # error_code=node_cancelled) COUNTS (PO decision). compensation_failed is a
    # genuine delivery failure (the watched node AND its compensation path both
    # failed) — explicit reason, counted as no_delivery (never fail-safe).
    if status in _COUNTABLE_NO_DELIVERY_STATUSES:
        reason = (
            REASON_COMPENSATION_FAILED
            if status == "compensation_failed"
            else _derive_no_delivery_reason(error_code, raw_output_markers)
        )
        return ClassificationResult(
            RunClassificationValue.no_delivery,
            reason,
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
        # FAR-1336: the SAME collection walk fills the per-URL provenance map —
        # a URL found ONLY in telemetry/markers is ``matched`` — and the
        # verdict is UNCHANGED: matched-only still classifies ``delivered``
        # (deliberate; the delivery rule is not tightened by provenance).
        pr_url_provenance: dict[str, str] = {}
        pr_urls = collect_pr_urls(outputs_json, telemetry_json, raw_output_markers, pr_url_provenance)
        if pr_urls:
            return ClassificationResult(
                RunClassificationValue.delivered,
                REASON_DELIVERED,
                delivered_pr_urls=tuple(pr_urls),
                pr_url_provenance=pr_url_provenance,
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
    """The persisted ``unclassified`` record shape — the fail-closed marker.

    Carries the same keys as :meth:`ClassificationResult.to_dict` (including
    the FAR-1336 provenance/confidence keys) so every terminal run's record
    has one shape regardless of which writer produced it.
    """
    return {
        "value": RunClassificationValue.unclassified.value,
        "reason": reason,
        "delivered_pr_urls": [],
        "computed_at": datetime.now(UTC).isoformat(),
        "work_intact": None,
        "declared_success_nodes": 0,
        "pr_url_provenance": {},
        "delivery_confidence": DELIVERY_CONFIDENCE_SELF_REPORTED,
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
        # ``asyncio.wait_for`` raises TimeoutError (== the builtin, since 3.11)
        # when the bound fires — a genuine DB stall. ``TimeoutError`` subclasses
        # ``OSError``, so it must be caught BEFORE the broad handler to keep the
        # distinct ``persist_timeout`` failure counter.
        _log.warning("classification.persist_timeout run=%s", run.id)
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
    (status, error_code, the reassembled outputs/telemetry/markers blobs,
    work_intact) and persists it atomically in the caller's transaction. The
    pure classify computation runs OFF the event loop
    (``asyncio.to_thread``) so a fat-output run cannot stall the whole
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
        # FAR-583 read-switch: the blobs reassemble from run_node_outputs via
        # the repo reader (new-table-only since B2c) inside the
        # caller's SAME transaction — the dual-write leg has already mirrored
        # this terminalization's outputs/telemetry, and every caller reads
        # flushed/committed state (update_run_status flushes before this hook;
        # the fenced / work-intact / reconcile paths re-read with
        # populate_existing or FOR UPDATE), so no in-session unsaved state is
        # relied on. ONE batched repo query — never per-node lazy loads.
        from modulo.db.crud.run_node_outputs import read_run_blobs

        blobs = await read_run_blobs(session, run_id=run.id, organisation_id=run.organisation_id)
        result = await asyncio.to_thread(
            classify_run,
            run.status,
            run.error_code,
            outputs_json=blobs.outputs,
            telemetry_json=blobs.telemetry,
            raw_output_markers=blobs.markers,
            work_intact=run.work_intact,
            # FAR-1233 WHY stamp: lets the cancelled branch tell a review-window
            # timeout (hitl_timeout) from a deliberate cancel. ``getattr`` keeps
            # this resilient to stand-in run rows (tests, partial mocks) built
            # before the column existed.
            cancel_reason=getattr(run, "cancel_reason", None),
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
