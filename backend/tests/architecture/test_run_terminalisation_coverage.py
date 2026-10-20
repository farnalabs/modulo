"""Architecture gate: every in-flight run state has a wired terminalising path (FAR-1966).

The ``delivery-feedback`` 30-day baseline (2026-10-18) surfaced
``run-lifecycle-wedge`` — 18 findings across 4 PRs and 10 commits — with **no**
prose rule and **no** structural check (coverage verdict GAP). The observed
theme is a run that never terminalises: stranded ``pending``, a dead executor,
a nodeless dispatch window, a claim cap, backpressure demotion, a node-started
run whose executor died. Every fix added or widened a *terminaliser* — and the
next wedge shape still ships silently because nothing mechanical says "this
in-flight state has an owner".

Existing coverage this gate does NOT duplicate (all read and confirmed present
before this file was written):

* ``tests/unit/cron_helpers/test_far1803_node_started_executor_lost.py`` —
  the node-started + stale-heartbeat class (FAR-1803).
* ``tests/unit/cron_helpers/test_far1740_pipeline_nodeless_window.py`` — the
  per-pipeline claimed-but-nodeless window (FAR-1740).
* ``tests/unit/cron_helpers/test_node_started_redispatch_guard.py`` — the
  never-re-execute-a-node-started-run guard and the pending reclaim leg
  (FAR-1649).
* ``tests/unit/cron_helpers/test_nodeless_dispatch_phase.py`` — the nodeless
  terminal record naming its dispatch phase (FAR-1088).
* ``tests/architecture/test_background_audit_coverage.py`` — the audit-append
  ratchet for background paths (FAR-1549); a different invariant.

Those pin individual wedge *shapes*. What none of them asserts is the
composition invariant: **every non-terminal run state is classified against a
terminalising path that actually runs**, so a NEW state (or a terminaliser that
is declared but never wired) fails a gate instead of being found later by a
correction commit.

How this gate stays honest (the ``test_background_audit_coverage.py`` idiom):

* **Enumerate mechanically, never hand-list.** The non-terminal and terminal
  status sets are imported from the model; the model's closed status vocabulary
  is parsed from the ``ck_runs_status`` CHECK constraint; the durable dispatch
  phases are read from the ``dispatch_phases`` module's own ``__all__``; the
  terminaliser registry is read from ``cron_helpers._TERMINALIZERS`` /
  ``_BATCH_TERMINALIZER_SPECS``; and the wiring set is a forward call-graph walk
  from the SAQ worker's own periodic registrations (``_system_functions`` /
  ``_runs_functions`` / the ``CronJob(...)`` list). A NEW status, phase or
  terminaliser-spec fails the gate until someone classifies it.
* **Classify explicitly.** Each in-flight status is ``covered`` (naming one or
  more terminalising paths, each carrying a file + source marker that proves the
  path exists), ``exempt`` (a documented reason from a fixed vocabulary) or
  ``gap`` (a real hole this change did not close, carrying a note). Each
  dispatch phase is ``generic`` (collected by the generic ``running``
  terminalisers) or ``dedicated`` (needs a leg of its own). A classification is
  a decision someone can read; a gap is visible.
* **An exemption carries its reasoning, and a gap is never vacuous (FAR-1966b).**
  ``EXEMPT_REASONS`` is a closed vocabulary, and every exempt entry must
  additionally cite resolving ``*.py`` source anchors — so a status cannot be
  parked out of the gate as a bare marked-string
  (``test_exempt_statuses_carry_a_source_anchored_justification``). The
  gap-actionability predicate is driven by a synthetic thin note even when the
  inventory carries no real ``gap``, so the branch that adjudicates a FUTURE gap
  can still fail today (``test_every_status_gap_is_visible_and_actionable``).

Documented limitations
----------------------
* The evidence is a SUBSTRING check on the named module: it proves the marker
  text still exists, not that the predicate admits every intra-status shape.
  This gate proves the *coverage surface* is complete and wired; it cannot
  prove that an existing terminaliser's SQL admits EVERY row shape inside a
  status (the FAR-1803 class lived one level below this gate — a phase the
  generic legs excluded). That general level resists a false-positive-free
  mechanical predicate, and the phase axis below is its closest honest proxy:
  it ratchets a NEW durable phase. For the one observed intra-status carrier
  the gate now goes a level deeper: ``_node_started_shield_is_liveness_gated``
  re-derives from the registered running terminaliser's own SQL that its
  node-started shield lifts for a dead executor (FAR-1803), so an
  unconditional shield fails here instead of surviving until the 25h marker
  sweep.
* Wiring is a forward call-graph reachability check over module-level function
  names. Name collisions can only make a function look reachable when it is
  not (a false PASS), never the reverse, so the gate does not produce false
  failures; two terminalisers in different modules sharing a name would be
  indistinguishable, which this file documents rather than pretends to solve.
* ``_TERMINALIZERS`` coroutines are resolved dynamically
  (``globals().get(coroutine_name)``), so they are unreachable by a static call
  walk; they are wired iff their spec is in ``_BATCH_TERMINALIZER_SPECS`` — the
  registry-consistency test below is what makes that a real check.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = BACKEND_DIR / "src" / "modulo"
SAQ_WORKER_PATH = SRC_DIR / "core" / "saq_worker.py"
DISPATCH_PHASES_PATH = SRC_DIR / "core" / "dispatch_phases.py"

_COVERED = "covered"
_EXEMPT = "exempt"
_GAP = "gap"

_TERMINALISES = "terminalises"
_RECOVERS = "recovers"

_GENERIC = "generic"
_DEDICATED = "dedicated"

#: The ONLY reasons an in-flight status may be exempt. Each is a decision a
#: reader can check against the model; an exemption outside this vocabulary, or
#: one naming no lawful recovery, fails the gate.
EXEMPT_REASONS: dict[str, str] = {
    "operator_driven_recovery": (
        "the model documents this as a recovery state an operator reconciles by "
        "re-running with the SAME persisted idempotency_key; there is no automatic "
        "terminaliser, and reclassification is required if a production writer is added"
    ),
    "unreachable_legacy_state": (
        "no production code path creates this state (it survives from a historical "
        "migration/repair); it cannot strand a live run, and reclassification is "
        "required the moment a writer is introduced"
    ),
    "operator_decision_recovery": (
        "the park sweep moves a gate-expired run out of awaiting_human into a "
        "NON-terminal parked state so it stops occupying review state; the gate stays "
        "open and claimable, and the run is recovered ONLY by a human decision "
        "(HITLManager._decide un-parks it at decision time) or by the F6a gated "
        "recovery when a decision committed while the sweep was parking it. Parked "
        "runs hold no PIPELINE execution slot but ARE counted by the org-level "
        "run_concurrency_limit gate, so they cannot accumulate unboundedly, and they "
        "remain cancellable through the normal cancel path; reclassification is "
        "required the moment an automatic terminaliser (or any state that escapes the "
        "org gate) is introduced"
    ),
}


@dataclass(frozen=True)
class TerminaliserPath:
    """One named, source-evidenced path that reaches a terminal outcome."""

    function: str
    #: ``(src-relative module path, literal marker)`` — the marker must appear in
    #: the named file, which is the evidence the path is still wired in source.
    evidence: tuple[str, str]
    #: ``terminalises`` moves the run directly to a terminal status; ``recovers``
    #: moves it to another in-flight status that is itself covered.
    kind: str = _TERMINALISES

    def __post_init__(self) -> None:
        if self.kind not in {_TERMINALISES, _RECOVERS}:
            raise ValueError(f"unknown path kind {self.kind!r}")


@dataclass(frozen=True)
class StatusCoverage:
    """Classification for one non-terminal run status."""

    classification: str
    paths: tuple[TerminaliserPath, ...] = field(default_factory=tuple)
    reason: str = ""
    note: str = ""
    #: Required when EVERY path is a recovery: the in-flight status it recovers
    #: into, which must itself be covered.
    recovered_into: str = ""

    def __post_init__(self) -> None:
        if self.classification not in {_COVERED, _EXEMPT, _GAP}:
            raise ValueError(f"unknown classification {self.classification!r}")
        if self.classification == _EXEMPT and self.reason not in EXEMPT_REASONS:
            raise ValueError(
                f"exempt status must use a documented reason, got {self.reason!r}; known: {sorted(EXEMPT_REASONS)}"
            )
        if self.classification == _GAP and not self.note:
            raise ValueError("a gap must carry a note naming what is missing")
        if self.classification == _COVERED and not self.paths:
            raise ValueError("a covered status must name at least one terminalising path")
        if self.classification != _COVERED and self.paths:
            raise ValueError("only a covered status may carry paths")
        if self.paths and all(path.kind == _RECOVERS for path in self.paths) and not self.recovered_into:
            raise ValueError("a recovery-only status must name the covered status it recovers into")


@dataclass(frozen=True)
class PhaseHandling:
    """Classification for one durable dispatch phase label."""

    kind: str
    paths: tuple[TerminaliserPath, ...] = field(default_factory=tuple)
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {_GENERIC, _DEDICATED}:
            raise ValueError(f"unknown phase kind {self.kind!r}")
        if not self.paths:
            raise ValueError("a dispatch phase must name the path that collects a run left in it")


def _path(function: str, rel_path: str, marker: str, *, kind: str = _TERMINALISES) -> TerminaliserPath:
    return TerminaliserPath(function=function, evidence=(rel_path, marker), kind=kind)


_CRON = "src/modulo/core/cron_helpers.py"
_EXEC = "src/modulo/core/pipeline_execution.py"
_ADMISSION = "src/modulo/core/run_admission.py"
_CAPACITY = "src/modulo/core/runner_capacity.py"
_EXPIRY = "src/modulo/core/hitl_manager/expiry_job.py"

#: The generic ``running`` selector shared by the batch terminalisers — a run in
#: ANY dispatch phase (other than the shielded node-started one) is collected by
#: the mid-graph age gate.
_GENERIC_RUNNING = _path(
    "_terminalize_mid_graph_wedges",
    _CRON,
    "WHERE organisation_id=:oid AND status='running' AND dispatcher='saq' ",
)

# --- the status inventory ------------------------------------------------
_STATUS_COVERAGE: dict[str, StatusCoverage] = {
    "pending": StatusCoverage(
        _COVERED,
        paths=(
            _path(
                "_terminalize_claim_cap_exhausted",
                _CRON,
                "status IN ('running','pending') AND claim_count >= :cap",
            ),
            _path(
                "_terminalize_stranded_pending_runs",
                _CRON,
                "AND dispatcher IS NOT NULL AND dispatcher != 'saq' ",
            ),
            _path("_fail_run_dispatch_failed", _CRON, "AND enqueue_failed_at IS NOT NULL"),
        ),
    ),
    "running": StatusCoverage(
        _COVERED,
        paths=(
            _path(
                "_terminalize_aged_nodeless_zombies",
                _CRON,
                "await _fail_nodeless_run(session, run_id, org_id, summary)",
            ),
            _path("_terminalize_mid_graph_wedges", _CRON, "dispatcher_reconcile: age-bound wedge terminalized"),
            _path(
                "_terminalize_claim_cap_exhausted",
                _CRON,
                "status IN ('running','pending') AND claim_count >= :cap",
            ),
            _path("stale_run_recovery_sweep", _EXEC, 'source="stale_run_recovery"'),
            _path("reconcile_pipeline_slots", _ADMISSION, 'source="slot_reconciliation"'),
            _path("reconcile_runner_dispatch_markers", _CAPACITY, 'event_type="run.sweep_terminalised"'),
        ),
    ),
    "awaiting_human": StatusCoverage(
        _COVERED,
        paths=(
            _path("_terminalize_expired_hitl_reviews", _CRON, "expired-HITL-gate zombie terminalized"),
            _path("_terminalize_hitl_review_missing", _CRON, "zero-claim awaiting_human zombie terminalized"),
        ),
    ),
    "claimed": StatusCoverage(
        _COVERED,
        paths=(
            _path(
                "expire_stale_claims",
                _EXPIRY,
                'Run.status == "claimed"',
                kind=_RECOVERS,
            ),
        ),
        recovered_into="awaiting_human",
    ),
    "unknown": StatusCoverage(
        _EXEMPT,
        reason="operator_driven_recovery",
        note=(
            "FAR-410. Design anchor: db/models/run.py:62-65 documents ``unknown`` as a "
            "NON-TERMINAL recovery status whose outcome is indeterminate (e.g. a lost "
            "sandbox), reconciled to a terminal outcome ONLY by an operator re-running "
            "with the SAME persisted run-level ``idempotency_key``; db/models/run.py:213-214 "
            "excludes it from ONGOING_ACTIVE_STATUSES so a stuck UNKNOWN cannot trigger an "
            "ongoing top-up, and dispatcher_reconcile's drain note (cron_helpers.py:7038-7046) "
            "records the same contract at the detection site. Reclassification is required "
            "the moment an automatic terminaliser (rather than an operator re-run) is "
            "introduced."
        ),
    ),
    "hitl_parked": StatusCoverage(
        _EXEMPT,
        reason="operator_decision_recovery",
        note=(
            "FAR-604 D2/D3. Design anchor: db/models/run.py:66-71 documents the park "
            "sweep moving a gate-expired run out of awaiting_human so it stops "
            "occupying review state, deliberately kept NON-terminal so every in-flight "
            "consumer treats it identically; db/models/run.py:165-177 shows it is "
            "org-bounded (excluded from PIPELINE_CAPACITY_STATUSES, but the org gate "
            "counts ACTIVE_RUN_STATUSES - {'pending'}). Recovery paths: a decision "
            "un-parks at decision time (hitl_manager/__init__.py:1216-1230, via "
            "db/crud/run.py:unpark_parked_run) and the F6a gated recovery collects a "
            "parked run whose decision committed while the sweep was parking it "
            "(cron_helpers.py:5464-5475). Safety properties pinned by: "
            "tests/unit/test_run_admission.py::TestPipelineCapacityExcludesAwaitingHuman "
            "(org gate counts it), tests/unit/api/test_far1967_hitl_parked_safety.py "
            "(cancellable through the normal path), and "
            "tests/unit/cron_helpers/test_dispatcher_reconcile.py"
            "::TestHitlResumeOrSkipPredicateMatrix::test_parked_with_committed_decision_resumes "
            "and ::TestReDispatchPredicateMatchesParked (F6a recovery)."
        ),
    ),
}

# --- the phase inventory -------------------------------------------------
_PHASE_COVERAGE: dict[str, PhaseHandling] = {
    "PHASE_NOT_STARTED": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_CLAIMED": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_LOADING_SETUP": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_SETUP_COMPLETE": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_EXECUTOR_RUNNING": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_LOADING_CONTEXT": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_CAPACITY_CHECK": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_SPEND_CHECK": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_INIT_ENV": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_GRAPH_COMPILE": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    "PHASE_STREAMING": PhaseHandling(_GENERIC, paths=(_GENERIC_RUNNING,)),
    # FAR-1803: the node-started phase carried a LIVE dispatch marker that the
    # generic age gate's shield held hard in-flight regardless of heartbeat, so
    # it was excluded by every running terminaliser and only the 25h marker
    # sweep could ever collect it. The shield now lifts once the heartbeat is
    # stale — this dedicated path is that leg.
    "PHASE_FIRST_NODE_DISPATCHED": PhaseHandling(
        _DEDICATED,
        paths=(
            _GENERIC_RUNNING,
            _path(
                "_terminalize_mid_graph_wedges",
                _CRON,
                "AND heartbeat_at >= now() - (:heartbeat_fresh * interval '1 second')",
            ),
        ),
        note="FAR-1803: the live-marker shield now carries a liveness condition, so a stale/NULL heartbeat lifts it",
    ),
}


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _model_status_sets() -> tuple[frozenset[str], frozenset[str]]:
    from modulo.db.models.run import ACTIVE_RUN_STATUSES, TERMINAL_STATUSES

    return ACTIVE_RUN_STATUSES, TERMINAL_STATUSES


def _model_status_vocabulary() -> frozenset[str]:
    """The closed status vocabulary the DB CHECK constraint enforces."""
    from sqlalchemy import CheckConstraint

    from modulo.db.models.run import Run

    checks = [c for c in Run.__table_args__ if isinstance(c, CheckConstraint)]
    check = next(c for c in checks if c.name == "ck_runs_status")
    return frozenset(re.findall(r"'([a-z_]+)'", check.sqltext.text))


def _non_terminal_statuses() -> frozenset[str]:
    active, _terminal = _model_status_sets()
    return frozenset(active)


def _dispatch_phase_constants() -> dict[str, str]:
    """``PHASE_*`` constant name -> its value, read from the module's ``__all__``."""
    tree = ast.parse(DISPATCH_PHASES_PATH.read_text(encoding="utf-8"))
    exported: set[str] = set()
    values: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            exported = {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values[node.targets[0].id] = node.value.value
    if not exported:
        raise AssertionError("dispatch_phases.__all__ not found")
    return {name: values[name] for name in sorted(exported) if name in values}


def _terminalizer_specs() -> tuple[Any, ...]:
    from modulo.core import cron_helpers as ch

    return ch._TERMINALIZERS


def _batch_terminalizer_keys() -> set[str]:
    from modulo.core import cron_helpers as ch

    return {spec.key for spec in ch._BATCH_TERMINALIZER_SPECS}


def _batch_terminalizer_coroutines() -> set[str]:
    """Coroutine names that actually run, keyed through the batch registry."""
    batch_keys = _batch_terminalizer_keys()
    names = {
        spec.coroutine_name
        for spec in _terminalizer_specs()
        if spec.key in batch_keys and spec.coroutine_name is not None
    }
    return {name for name in names if name}


def _system_cron_function_names() -> set[str]:
    tree = ast.parse(SAQ_WORKER_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_system_cron_jobs":
            names: set[str] = set()
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "CronJob"
                    and call.args
                    and isinstance(call.args[0], ast.Name)
                ):
                    names.add(call.args[0].id)
            return names
    raise AssertionError("_system_cron_jobs not found in saq_worker.py")


def _periodic_root_names() -> set[str]:
    """Names registered on the SAQ periodic/worker paths (settings-free)."""
    from modulo.core import saq_worker as sw

    names = {fn.__name__ for fn in sw._system_functions()}
    names |= {fn.__name__ for _registered_name, fn in sw._runs_functions()}
    names |= _system_cron_function_names()
    return names


def _call_graph() -> dict[str, set[str]]:
    """Module-level function name -> the names its body calls (all of src/modulo)."""
    graph: dict[str, set[str]] = {}
    for path in sorted(SRC_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called: set[str] = set()
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Name):
                    called.add(call.func.id)
                elif isinstance(call.func, ast.Attribute):
                    called.add(call.func.attr)
            graph.setdefault(node.name, set()).update(called)
    return graph


def _reachable_from_periodic() -> set[str]:
    """Forward call-graph closure from the SAQ periodic/worker registrations."""
    graph = _call_graph()
    seen = set(_periodic_root_names())
    frontier = set(seen)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            for callee in graph.get(name, ()):
                if callee not in seen:
                    seen.add(callee)
                    nxt.add(callee)
        frontier = nxt
    return seen


def _module_level_function_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(SRC_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return names


def _evidence_present(rel_path: str, marker: str) -> bool:
    path = BACKEND_DIR / rel_path
    if not path.is_file():
        return False
    return marker in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Exempt-justification anchors — a claim of recovery must cite where it lives
# ---------------------------------------------------------------------------
#
# An exemption is a DECISION that a status needs no automatic terminaliser. The
# reason must come from the closed ``EXEMPT_REASONS`` vocabulary (validated at
# construction and by ``test_exempt_statuses_use_only_documented_reasons``) AND
# the entry must carry a justification anchored to specific source evidence — a
# bare restatement of the reason is not reviewable, and an exemption that cannot
# be checked by reading the source is a gap wearing an exempt label. The anchor
# is a ``*.py`` path (optionally ``:line`` or ``:start-end``) that must resolve
# under ``backend/``; a rotted anchor (the cited file removed, or a line past
# its end) fails the gate.

#: Trees an exempt justification's ``*.py`` anchors must resolve within: the
#: product source and the test tree the safety properties live in.
_ANCHOR_ROOTS: tuple[Path, ...] = (SRC_DIR, BACKEND_DIR / "tests")

#: A cited source anchor: a ``*.py`` path with an optional ``:line`` or
#: ``:start-end`` suffix. A ``path:symbol`` citation simply matches the path.
_PY_ANCHOR_RE = re.compile(r"([A-Za-z0-9_./-]+\.py)(?::(\d+)(?:-(\d+))?)?")

#: Shorter than this and an exempt justification reads as a bare string rather
#: than a reviewable decision.
_MIN_EXEMPT_JUSTIFICATION = 80


def _anchor_source_files() -> list[Path]:
    """The ``*.py`` files an exempt justification's anchors may resolve against."""
    return sorted(path for root in _ANCHOR_ROOTS for path in root.rglob("*.py"))


def _resolve_py_anchor(fragment: str, files: Iterable[Path]) -> list[Path]:
    """Files whose path ends with the cited fragment (``db/models/run.py`` …).

    Notes cite the shortest distinctive path, not a backend-relative one (both
    ``db/models/run.py`` and bare ``cron_helpers.py`` appear), so resolution is
    by path SUFFIX over the anchor trees.

    The cited fragment always uses POSIX separators (the anchor regex only
    permits ``/``), while ``str(Path)`` renders with the platform separator — so
    the comparison is normalised to POSIX on BOTH sides. Comparing
    ``str(path)`` against a ``/``-prefixed suffix is false on every match under
    Windows (``backend\\src\\...`` never ends with ``/db/models/run.py``), which
    made this gate unsatisfiable there regardless of the anchor's accuracy.
    """
    suffix = "/" + fragment.lstrip("./").replace("\\", "/")
    return [path for path in files if path.as_posix().endswith(suffix)]


def _exempt_justification_violations(
    status: str,
    record: StatusCoverage,
    *,
    anchor_files: Iterable[Path],
) -> list[str]:
    """An exemption's note must be substantial and cite resolving source anchors.

    ``record.reason`` is validated against ``EXEMPT_REASONS`` at construction;
    this is the complementary half — the note must be long enough to review and
    name at least one ``*.py`` anchor that resolves today. An anchor pointing
    past the end of every matching file is also a violation: that is a citation
    that has rotted.
    """
    files = list(anchor_files)
    note = record.note.strip()
    violations: list[str] = []
    if len(note) < _MIN_EXEMPT_JUSTIFICATION:
        violations.append(
            f"status:{status} exempt justification too thin to review "
            f"({len(note)} < {_MIN_EXEMPT_JUSTIFICATION}): {record.note!r}"
        )
    anchors = list(_PY_ANCHOR_RE.finditer(note))
    if not anchors:
        violations.append(f"status:{status} exempt justification cites no source anchor (*.py path): {record.note!r}")
        return violations
    for anchor in anchors:
        fragment, start, end = anchor.group(1), anchor.group(2), anchor.group(3)
        resolved = _resolve_py_anchor(fragment, files)
        if not resolved:
            violations.append(f"status:{status} exempt anchor {fragment!r} resolves to no file under backend/")
            continue
        if start is not None:
            bound = int(end) if end is not None else int(start)
            if not any(len(path.read_text(encoding="utf-8").splitlines()) >= bound for path in resolved):
                violations.append(
                    f"status:{status} exempt anchor {fragment!r} points past the end of every "
                    f"matching file (needs line {bound})"
                )
    return violations


# ---------------------------------------------------------------------------
# Intra-status row-shape predicate — the FAR-1803 carrier
# ---------------------------------------------------------------------------
#
# The coverage surface above proves a terminalising path EXISTS for every
# in-flight status. It cannot, in general, prove that an existing
# terminaliser's SQL ADMITS every row shape inside that status — the FAR-1803
# class was exactly that: a ``running`` row (``dispatcher='saq'``,
# ``dispatch_phase='first_node_dispatched'``) carrying a LIVE dispatch marker
# whose executor heartbeat was STALE, which every running selector excluded.
# The generic age gate excluded it behind the FAR-1740 (3) live-marker shield
# with NO heartbeat condition; the nodeless router and the stale leg both
# excluded the phase; the claim cap was unreached. Only the 25h marker sweep
# could ever collect it (fix: 894e03783, PR #1840).
#
# The narrow, honest predicate below re-derives the ONE invariant the fix
# established, straight from the registered terminaliser's own SQL: a shield
# that excludes a node-started row must require a FRESH heartbeat, so a DEAD
# executor's row is admitted. It deliberately does not attempt arbitrary
# row-shape reasoning (which resists a false-positive-free predicate); it pins
# the specific intra-status carrier that escaped. Its stated boundary: the
# liveness signal it recognises is ``heartbeat_at`` versus ``now()``. A future
# refactor that shields on a DIFFERENT liveness column would trip this check,
# and the honest response is to widen the predicate deliberately (a fresh
# deadline column is a new invariant worth naming), never to delete the check.

#: The node-started shield the generic running terminaliser applies, and the
#: token that terminates it in the (static) SQL template.
_NODE_STARTED_SHIELD_ANCHOR = "NOT (dispatch_phase = :first_node_phase"
_NODE_STARTED_SHIELD_END = "AND NOT (__ZERO_PROGRESS_SHAPE__)"


def _node_started_shield_is_liveness_gated(sql: str) -> bool:
    """Would this terminaliser's node-started shield lift for a DEAD executor?

    ``True`` when the SQL carries no node-started shield (the phase is not
    excluded) OR when every shield it carries requires a fresh heartbeat — the
    heartbeat-vs-``now()`` bound that the FAR-1803 fix added. ``False`` is the
    pre-fix shape: an unconditional shield that admits the dead-executor row
    to no terminaliser.
    """
    start = sql.find(_NODE_STARTED_SHIELD_ANCHOR)
    if start == -1:
        return True
    end = sql.find(_NODE_STARTED_SHIELD_END, start)
    shield = sql[start:] if end == -1 else sql[start:end]
    return "heartbeat_at >=" in shield and "now()" in shield


def _registered_running_terminalizer_sql() -> list[str]:
    """The SQL each REGISTERED terminaliser runs, filtered to running selectors.

    The registry (``_TERMINALIZERS``) carries no SQL — a terminaliser's
    selection predicate lives in its coroutine body. Resolving the module-level
    string constants each registered coroutine names (rather than hand-listing
    constants) keeps this mechanical: a relocated or renamed SQL constant still
    resolves, so the check cannot rot silently into a no-op.
    """
    from modulo.core import cron_helpers as ch

    tree = ast.parse(Path(ch.__file__).read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    registered = {spec.coroutine_name for spec in _terminalizer_specs() if spec.coroutine_name}
    sqls: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in registered:
            continue
        names = {load.id for load in ast.walk(node) if isinstance(load, ast.Name) and isinstance(load.ctx, ast.Load)}
        sqls.extend(constants[name] for name in names if name in constants)
    return [sql for sql in sqls if "status='running'" in sql]


# ---------------------------------------------------------------------------
# Pure predicates (the test seams the proof tests drive)
# ---------------------------------------------------------------------------


def _unclassified(inventory: Mapping[str, object], enumerated: Iterable[str]) -> list[str]:
    return sorted(set(enumerated) - set(inventory))


def _stale_entries(inventory: Mapping[str, object], enumerated: Iterable[str]) -> list[str]:
    return sorted(set(inventory) - set(enumerated))


def _path_violations(
    record: StatusCoverage | PhaseHandling,
    *,
    function_exists: Callable[[str], bool],
    is_wired: Callable[[str], bool],
    marker_present: Callable[[str, str], bool],
    label: str,
) -> list[str]:
    """Every named path must resolve, be wired, and keep its evidence marker."""
    violations: list[str] = []
    for path in record.paths:
        rel_path, marker = path.evidence
        if not function_exists(path.function):
            violations.append(f"{label}: path function {path.function!r} not found in src/modulo")
        if not is_wired(path.function):
            violations.append(f"{label}: path {path.function!r} is not reachable from a periodic path")
        if not marker_present(rel_path, marker):
            violations.append(f"{label}: evidence marker {marker!r} missing from {rel_path}")
    return violations


# ---------------------------------------------------------------------------
# Gates — enumeration integrity
# ---------------------------------------------------------------------------


def test_status_sets_partition_the_model_vocabulary() -> None:
    """TERMINAL and ACTIVE are disjoint and together cover the DB vocabulary.

    A status added to ``ck_runs_status`` but to neither set is a run state no
    consumer classifies — exactly the shape a wedge lands in.
    """
    active, terminal = _model_status_sets()
    vocabulary = _model_status_vocabulary()
    assert not (active & terminal), f"statuses in both sets: {sorted(active & terminal)}"
    assert active | terminal == vocabulary, (
        "the model status sets do not partition the ck_runs_status vocabulary; "
        f"unclassified: {sorted(vocabulary - (active | terminal))}; "
        f"unknown: {sorted((active | terminal) - vocabulary)}"
    )


def test_terminalizer_registry_is_consistent_and_batch_wired() -> None:
    """Every declared terminaliser resolves, and a coroutine-bearing one runs.

    A spec that names a coroutine but is absent from ``_BATCH_TERMINALIZER_SPECS``
    is declared and never executed — the silent-critical shape (a sweep with no
    production caller). A coroutine name that does not resolve fails loudly even
    under ``python -O`` (the resolver uses ``if`` guards, not ``assert``).
    """
    from modulo.core import cron_helpers as ch

    specs = _terminalizer_specs()
    keys = {spec.key for spec in specs}
    batch_keys = _batch_terminalizer_keys()
    assert batch_keys <= keys, f"batch terminalizer(s) not registered: {sorted(batch_keys - keys)}"
    missing = sorted(spec.key for spec in specs if spec.coroutine_name is not None and spec.key not in batch_keys)
    assert not missing, (
        "terminalizer spec(s) declare a coroutine but never run (absent from "
        "_BATCH_TERMINALIZER_SPECS):\n  " + "\n  ".join(missing)
    )
    unresolved = sorted(
        spec.coroutine_name
        for spec in specs
        if spec.coroutine_name is not None and not hasattr(ch, spec.coroutine_name)
    )
    assert not unresolved, f"terminalizer coroutine name(s) do not resolve in cron_helpers: {unresolved}"


def test_enumeration_is_non_vacuous() -> None:
    """An enumerator that silently returned nothing would pass every other gate."""
    assert "pending" in _non_terminal_statuses()
    assert "running" in _non_terminal_statuses()
    assert "complete" in _model_status_vocabulary()
    assert "PHASE_FIRST_NODE_DISPATCHED" in _dispatch_phase_constants()
    assert "dispatcher_reconcile" in _periodic_root_names()
    assert "stale_run_recovery_sweep" in _reachable_from_periodic()


# ---------------------------------------------------------------------------
# Gates — status inventory
# ---------------------------------------------------------------------------


def test_every_non_terminal_status_is_classified() -> None:
    """A NEW non-terminal status must be classified before it can land."""
    missing = _unclassified(_STATUS_COVERAGE, _non_terminal_statuses())
    assert not missing, (
        "unclassified non-terminal run status(es) — add each to _STATUS_COVERAGE in "
        "tests/architecture/test_run_terminalisation_coverage.py as covered (naming a wired "
        "terminalising path with file+marker evidence), exempt (a documented reason from "
        "EXEMPT_REASONS and its lawful recovery) or gap (a note naming the remainder):\n  " + "\n  ".join(missing)
    )


def test_no_stale_status_inventory_entries() -> None:
    """A renamed or removed status must be re-classified, not left rotting."""
    stale = _stale_entries(_STATUS_COVERAGE, _non_terminal_statuses())
    assert not stale, "status inventory entries no longer non-terminal (renamed or deleted):\n  " + "\n  ".join(stale)


def test_every_covered_status_names_a_real_wired_path_with_evidence() -> None:
    functions = _module_level_function_names()
    reachable = _reachable_from_periodic()
    batch_coroutines = _batch_terminalizer_coroutines()

    def is_wired(name: str) -> bool:
        return name in batch_coroutines or name in reachable

    def function_exists(name: str) -> bool:
        return name in functions

    violations: list[str] = []
    for status, record in sorted(_STATUS_COVERAGE.items()):
        if record.classification != _COVERED:
            continue
        violations.extend(
            _path_violations(
                record,
                function_exists=function_exists,
                is_wired=is_wired,
                marker_present=_evidence_present,
                label=f"status:{status}",
            )
        )
    assert not violations, "covered status(s) with an unwired or unproven path:\n  " + "\n  ".join(violations)


def test_recovery_only_status_recovers_into_a_covered_status() -> None:
    """A status whose only path is a recovery must land in a covered status."""
    for status, record in sorted(_STATUS_COVERAGE.items()):
        if record.classification != _COVERED:
            continue
        if not all(path.kind == _RECOVERS for path in record.paths):
            continue
        target = _STATUS_COVERAGE.get(record.recovered_into)
        assert target is not None, f"status:{status} recovers into unknown status {record.recovered_into!r}"
        assert target.classification == _COVERED, (
            f"status:{status} recovers into {record.recovered_into!r}, which is not covered"
        )


def test_exempt_statuses_use_only_documented_reasons() -> None:
    for status, record in sorted(_STATUS_COVERAGE.items()):
        if record.classification == _EXEMPT:
            assert record.reason in EXEMPT_REASONS, f"status:{status} undocumented exempt reason {record.reason!r}"


def test_exempt_statuses_carry_a_source_anchored_justification() -> None:
    """An exemption is a reviewed decision down to WHERE its recovery lives.

    The reason vocabulary is a closed set (``EXEMPT_REASONS``, plus
    ``test_exempt_statuses_use_only_documented_reasons``); this gate is the
    other half — every exempt entry must carry a justification anchored to
    specific ``*.py`` source evidence that still resolves. An entry whose note
    is a bare restatement, or cites a file/line that has since rotted, fails
    here: such an entry is a gap wearing an exempt label.
    """
    files = _anchor_source_files()
    violations: list[str] = []
    for status, record in sorted(_STATUS_COVERAGE.items()):
        if record.classification != _EXEMPT:
            continue
        violations.extend(_exempt_justification_violations(status, record, anchor_files=files))
    assert not violations, "exempt status(es) without a source-anchored justification:\n  " + "\n  ".join(violations)


def test_synthetic_bare_exempt_justification_is_flagged() -> None:
    """The anchor predicate must fire on a no-note and an unanchored exemption.

    Without this the gate could pass because the predicate silently stopped
    flagging anything — the exact rot the gate exists to catch.
    """
    files = _anchor_source_files()
    bare = StatusCoverage(_EXEMPT, reason="operator_driven_recovery")
    assert _exempt_justification_violations("synthetic", bare, anchor_files=files), (
        "an exemption with no justification passed the anchor gate"
    )
    unanchored = StatusCoverage(
        _EXEMPT,
        reason="operator_driven_recovery",
        note="a long enough justification that nevertheless names no source file anywhere at all",
    )
    assert _exempt_justification_violations("synthetic", unanchored, anchor_files=files), (
        "an exemption whose justification cites no source anchor passed the anchor gate"
    )
    anchored = StatusCoverage(
        _EXEMPT,
        reason="operator_driven_recovery",
        note=(
            "db/models/run.py:62-65 documents the operator-recovery contract this synthetic "
            "status relies on for its exemption decision"
        ),
    )
    assert not _exempt_justification_violations("synthetic", anchored, anchor_files=files)
    # The mirror of the line above: a cited ``*.py`` path that resolves to no
    # file must STILL be flagged, so normalising the separator comparison can
    # never be "made to pass" by loosening the predicate to match everything.
    nonexistent = StatusCoverage(
        _EXEMPT,
        reason="operator_driven_recovery",
        note=(
            "db/models/this_anchor_file_does_not_exist.py documents the operator-recovery "
            "contract this synthetic status relies on for its exemption decision"
        ),
    )
    assert _exempt_justification_violations("synthetic", nonexistent, anchor_files=files), (
        "an exemption citing a *.py path that resolves to no file passed the anchor gate"
    )


def test_resolve_py_anchor_normalises_separators_both_directions() -> None:
    """A POSIX-anchored fragment resolves against native (Windows) paths.

    The anchor is written ``db/models/run.py`` (POSIX separators) while the
    anchor tree yields native paths (``Path`` — backslashes on Windows), so the
    predicate must normalise before matching. Both directions are pinned here
    against synthetic files so the check is proven independent of the shipped
    inventory: the known-good fragment resolves, and a fragment naming no file
    still resolves to nothing.
    """
    files = [
        Path("backend/src/modulo/db/models/run.py"),
        Path("backend/src/modulo/db/crud/run.py"),
    ]
    assert _resolve_py_anchor("db/models/run.py", files) == [files[0]]
    assert not _resolve_py_anchor("db/models/missing.py", files)


def _gap_note_violations(status: str, note: str) -> list[str]:
    """A gap note must be long enough to act on and name the remaining work."""
    lowered = note.lower()
    violations: list[str] = []
    if len(lowered) <= 80:
        violations.append(f"status:{status} gap note too thin to act on: {note!r}")
    if "remainder" not in lowered:
        violations.append(f"status:{status} gap note must name the remaining work: {note!r}")
    return violations


def test_every_status_gap_is_visible_and_actionable() -> None:
    """Every real gap is actionable — and the predicate is never vacuous.

    FAR-1966b: ``_STATUS_COVERAGE`` currently carries NO ``gap`` entry (the last
    one, ``hitl_parked``, was exempted with its reasoning in PR #1985,
    squash-merged onto ``main`` as commit ``02ddc8ddd``), so the
    loop below iterates an empty set and would otherwise pass without asserting
    anything. The synthetic self-check drives the SAME predicate over a
    deliberately thin note and asserts it is flagged, so the branch that
    adjudicates a FUTURE gap can still fail here today.
    """
    assert _gap_note_violations("synthetic", "a remainder"), (
        "the gap-actionability predicate failed to flag a thin synthetic note — the gate "
        "would pass vacuously over an empty gap set"
    )
    violations: list[str] = []
    for status, record in sorted(_STATUS_COVERAGE.items()):
        if record.classification != _GAP:
            continue
        violations.extend(_gap_note_violations(status, record.note))
    assert not violations, "gap status(es) that are not visible/actionable:\n  " + "\n  ".join(violations)


# ---------------------------------------------------------------------------
# Gates — dispatch-phase inventory
# ---------------------------------------------------------------------------


def test_every_dispatch_phase_is_classified() -> None:
    """A NEW durable dispatch phase must be classified before it can land."""
    missing = _unclassified(_PHASE_COVERAGE, _dispatch_phase_constants())
    assert not missing, (
        "unclassified dispatch phase(s) — add each to _PHASE_COVERAGE in "
        "tests/architecture/test_run_terminalisation_coverage.py as generic (collected by the "
        "generic running terminalisers) or dedicated (naming the leg that admits it):\n  " + "\n  ".join(missing)
    )


def test_no_stale_phase_inventory_entries() -> None:
    stale = _stale_entries(_PHASE_COVERAGE, _dispatch_phase_constants())
    assert not stale, "phase inventory entries no longer exported:\n  " + "\n  ".join(stale)


def test_every_phase_path_is_real_wired_and_evidenced() -> None:
    functions = _module_level_function_names()
    reachable = _reachable_from_periodic()
    batch_coroutines = _batch_terminalizer_coroutines()

    def is_wired(name: str) -> bool:
        return name in batch_coroutines or name in reachable

    def function_exists(name: str) -> bool:
        return name in functions

    violations: list[str] = []
    for phase, record in sorted(_PHASE_COVERAGE.items()):
        violations.extend(
            _path_violations(
                record,
                function_exists=function_exists,
                is_wired=is_wired,
                marker_present=_evidence_present,
                label=f"phase:{phase}",
            )
        )
    assert not violations, "dispatch phase(s) with an unwired or unproven path:\n  " + "\n  ".join(violations)


def test_dedicated_phases_carry_a_note_and_a_generic_baseline() -> None:
    """A dedicated phase must explain why, and still ride the generic collector."""
    for phase, record in sorted(_PHASE_COVERAGE.items()):
        if record.kind != _DEDICATED:
            continue
        assert record.note, f"phase:{phase} is dedicated but carries no note"
        functions = {path.function for path in record.paths}
        assert "_terminalize_mid_graph_wedges" in functions, (
            f"phase:{phase} is dedicated but does not ride the generic running collector"
        )


# ---------------------------------------------------------------------------
# Proof — the gate fails on a synthetic wedge
# ---------------------------------------------------------------------------


def test_synthetic_new_wedge_status_is_flagged_unclassified() -> None:
    """A wedge shape shipped as a NEW non-terminal status fails the ratchet."""
    enumerated = set(_non_terminal_statuses())
    enumerated.add("wedge_status_that_does_not_exist")
    flagged = _unclassified(_STATUS_COVERAGE, enumerated)
    assert flagged == ["wedge_status_that_does_not_exist"]


def test_synthetic_new_wedge_phase_is_flagged_unclassified() -> None:
    """A wedge shape shipped as a NEW durable dispatch phase fails the ratchet."""
    enumerated = set(_dispatch_phase_constants())
    enumerated.add("PHASE_WEDGED_FOREVER")
    flagged = _unclassified(_PHASE_COVERAGE, enumerated)
    assert flagged == ["PHASE_WEDGED_FOREVER"]


def test_synthetic_unwired_terminaliser_fails_the_path_check() -> None:
    """A declared-but-unwired terminaliser is a violation, not a pass."""
    record = StatusCoverage(
        _COVERED,
        paths=(_path("_terminalizer_that_nobody_calls", _CRON, "status IN ('running','pending')"),),
    )
    violations = _path_violations(
        record,
        function_exists=lambda _name: False,
        is_wired=lambda _name: False,
        marker_present=_evidence_present,
        label="status:synthetic",
    )
    assert any("not found in src/modulo" in v for v in violations)
    assert any("not reachable from a periodic path" in v for v in violations)


def test_synthetic_missing_evidence_marker_fails_the_path_check() -> None:
    """A path whose source marker has been removed is a violation."""
    record = StatusCoverage(
        _COVERED,
        paths=(_path("_terminalize_mid_graph_wedges", _CRON, "a marker that does not exist anywhere"),),
    )
    violations = _path_violations(
        record,
        function_exists=lambda _name: True,
        is_wired=lambda _name: True,
        marker_present=_evidence_present,
        label="status:synthetic",
    )
    assert any("evidence marker" in v for v in violations)


def test_covered_record_without_paths_is_rejected_at_construction() -> None:
    """The construction contract itself blocks the vacuous ``covered`` claim."""
    with pytest.raises(ValueError, match="terminalising path"):
        StatusCoverage(_COVERED)


# ---------------------------------------------------------------------------
# Gates — intra-status row shape (the FAR-1803 carrier)
# ---------------------------------------------------------------------------


def test_running_terminalizer_sql_resolution_is_non_vacuous() -> None:
    """An AST walk that resolved nothing would pass the row-shape gate vacuously."""
    sqls = _registered_running_terminalizer_sql()
    assert len(sqls) >= 1
    shield_bearing = [sql for sql in sqls if _NODE_STARTED_SHIELD_ANCHOR in sql]
    assert len(shield_bearing) >= 1


def test_a_dead_executor_node_started_row_is_admitted_by_a_running_terminaliser() -> None:
    """FAR-1803: every node-started shield must lift for a STALE heartbeat.

    A shield that excludes ``dispatch_phase = 'first_node_dispatched'`` rows
    carrying a live marker WITHOUT a heartbeat condition admits the dead-executor
    row to no terminaliser — the observed wedge (runs sitting ``running`` for
    hours, collected only by the 25h marker sweep).
    """
    sqls = _registered_running_terminalizer_sql()
    shields = [sql for sql in sqls if _NODE_STARTED_SHIELD_ANCHOR in sql]
    violations = [sql for sql in shields if not _node_started_shield_is_liveness_gated(sql)]
    assert not violations, (
        "a registered running terminaliser excludes a node-started row behind an "
        "unconditional shield (no heartbeat-vs-now() liveness bound): a run whose "
        "executor died would be selected by no terminaliser (FAR-1803).\n  " + "\n  ".join(violations)
    )


def test_synthetic_pre_far1803_shield_is_flagged_by_the_predicate() -> None:
    """The predicate must fail on the exact pre-FAR-1803 selection predicate.

    Pre-fix (894e03783, PR #1840), the shield excluded a node-started row with a
    live dispatch marker and NO heartbeat condition, so the dead-executor row was
    not selected. Rebuild that SQL from the live template and assert the
    predicate reports it — while the live template itself passes.
    """
    sql = next(sql for sql in _registered_running_terminalizer_sql() if _NODE_STARTED_SHIELD_ANCHOR in sql)
    assert _node_started_shield_is_liveness_gated(sql) is True
    start = sql.index(_NODE_STARTED_SHIELD_ANCHOR)
    end = sql.index(_NODE_STARTED_SHIELD_END, start)
    pre_fix_shield = (
        "NOT (dispatch_phase = :first_node_phase "
        "AND runs.sandbox_dispatch_state IS NOT NULL "
        "AND runs.sandbox_dispatch_state NOT LIKE :live_marker_tombstone) "
    )
    synthetic = sql[:start] + pre_fix_shield + sql[end:]
    assert _node_started_shield_is_liveness_gated(synthetic) is False
