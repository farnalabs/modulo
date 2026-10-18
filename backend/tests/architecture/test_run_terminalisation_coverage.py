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

Documented limitations
----------------------
* The evidence is a SUBSTRING check on the named module: it proves the marker
  text still exists, not that the predicate admits every intra-status shape.
  This gate proves the *coverage surface* is complete and wired; it cannot
  prove that an existing terminaliser's SQL admits every row shape inside a
  status (the FAR-1803 class lived one level below this gate — a phase the
  generic legs excluded). That level genuinely resists a false-positive-free
  mechanical predicate, and the phase axis below is the closest honest proxy:
  it ratchets a NEW durable phase, which is how the FAR-1803 shape entered.
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
    ),
    "hitl_parked": StatusCoverage(
        _GAP,
        note=(
            "no terminaliser selects this status: the FAR-648 and FAR-721 HITL sweeps bind "
            "status=awaiting_human only, and resume_run's F6a recovery requires a committed "
            "decision (a park carries none). Under healthy reconcile ticks the FAR-648 sweep "
            "collects the run at terminalize_at, strictly before the park sweep can match at "
            "terminalize_at + park_margin, so a run is cancelled before it can park; but if "
            "the terminaliser is deferred on a tick (dispatcher_reconcile's per-org budget or "
            "the FAR-746 max_rows cap), the park can win and the run is then unreachable by "
            "the terminaliser. Remainder: decide whether an abandoned hitl_parked run should "
            "be collected automatically or unpark-on-decision is the intended full recovery; "
            "the remedy is a design call, not a silent test edit."
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


def test_every_status_gap_is_visible_and_actionable() -> None:
    for status, record in sorted(_STATUS_COVERAGE.items()):
        if record.classification != _GAP:
            continue
        note = record.note.lower()
        assert len(note) > 80, f"status:{status} gap note too thin to act on: {record.note!r}"
        assert "remainder" in note, f"status:{status} gap note must name the remaining work: {record.note!r}"


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
