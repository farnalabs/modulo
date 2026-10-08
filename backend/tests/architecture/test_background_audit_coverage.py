"""Architecture gate: every background/cron/boot write path is classified (FAR-1549).

FAR-1472 closed the REQUEST-layer audit gap: ``audited()`` / ``audited_system()``
on mutating REST routes, ``mcp_audited`` on MCP tools, both ratcheted by
``tests/architecture/test_audit_coverage.py``. What that gate cannot see is the
set of writes that never see a request — SAQ system-cron sweeps, runs-worker
tasks, reconcilers and boot-time seeds. They mutate org-owned state with no
HTTP principal in scope, so they either record a SYSTEM-actor event or carry a
documented exemption.

This gate is the background counterpart of that ratchet, built the same way
(a live enumeration + a classification inventory) so it stays honest:

* **Enumerate mechanically** — never hand-list the paths. The SAQ worker's own
  registration functions (``_system_functions`` / ``_runs_functions``), the
  ``CronJob(...)`` list, the ``_boot_seed("<name>")`` calls in ``api/main.py``,
  the ``reconcile_*`` / ``*_sweep`` / ``*_cleanup`` / ``seed_*`` functions
  declared under ``src/modulo/core``, and (FAR-1574) every public function that
  builds a ``SuiteRun`` row, are read from the source, so a NEW background path
  fails the gate until someone classifies it.
* **Classify explicitly** — ``audited`` (with the file + literal marker that
  proves the append is wired), ``exempt`` (from a fixed, documented reason
  vocabulary) or ``gap`` (a real gap this PR did not close, carrying a note).
  An exemption is a decision someone can read; a gap is visible.

Why an architecture test and not semgrep: same reasoning as
``test_audit_coverage.py`` — the rule has to run on every platform the suite
runs on, and it needs Python-level enumeration (importing the registration
functions) that a pattern match cannot do.

Documented limitations
----------------------
* ``audited`` evidence is a SUBSTRING check on the target module. It proves the
  append literal exists in the file the inventory names; it does not prove the
  call is on the path taken at runtime. The wiring itself is proven by
  ``tests/unit/core/test_background_audit_wiring.py``, which patches the shared
  writer and asserts each sweep reaches it.
* The reconciler/seed sweep pattern (``reconcile_`` / ``seed_`` / ``_sweep`` /
  ``_cleanup`` / ``_reconcile``) reads MODULE-LEVEL functions only. A private
  helper reached through one of those (``_sweep_org_stale_runs``,
  ``_terminalize_*``) is covered by its public caller's classification.
* Path-name collisions across modules collapse to one inventory entry —
  ``dispatcher_reconcile`` exists in both ``saq_worker`` and ``cron_helpers``
  and is one logical path (the worker delegates).
* ``api/main.py`` boot seeds are enumerated by their ``_boot_seed`` label, not
  by the coroutine they run, so renaming the coroutine does not break the gate
  but renaming the label does (deliberate: the label is the operator-visible
  boot summary name).
* The SuiteRun-creation scan (FAR-1574) starts from ``build_suite_run`` — the
  single statement that INSERTs a ``suite_runs`` row — and walks outward over
  PUBLIC module-level callers until it stops, so an API route or CLI command
  that wires an existing entry point is caught too. Private helpers
  (``_build_suite_run_or_skip``) are covered by their registered public caller,
  which the SAQ scan enumerates separately.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = BACKEND_DIR / "src" / "modulo"
CORE_DIR = BACKEND_DIR / "src" / "modulo" / "core"
SAQ_WORKER_PATH = BACKEND_DIR / "src" / "modulo" / "core" / "saq_worker.py"
API_MAIN_PATH = BACKEND_DIR / "src" / "modulo" / "api" / "main.py"

#: Mechanical pattern for the reconciler / seeder half of the enumeration.
_RECONCILER_NAME = re.compile(r"^(reconcile_|seed_)|(_sweep|_reconcile|_cleanup)$")

#: ``build_suite_run`` is the single statement that INSERTs a ``suite_runs``
#: row (FAR-1574) — every caller of it is a SuiteRun-creation entry point,
#: whichever surface it hangs off (SAQ, cron, REST route, CLI).
_SUITE_RUN_BUILDER = "build_suite_run"

#: Modules under ``core/`` that are the audit machinery itself, not a background
#: write path — they are exempt from enumeration by construction.
_SKIP_DIR_PREFIX = "audit_logger/"
_SKIP_FILE = "audit_coverage.py"

#: The ONLY reasons a background path may be exempt. Each is a decision someone
#: can read; an exemption outside this vocabulary fails the gate.
EXEMPT_REASONS: dict[str, str] = {
    "trigger_bookkeeping": (
        "per-tick trigger schedule bookkeeping (next_fire_at / last_fired_at advances); "
        "high-frequency, non-entity state whose consequential action (the created run) is "
        "audited at the execution seam"
    ),
    "notification_only": "emits an alert/notification and mutates no org-owned entity row",
    "ephemeral_log_retention": (
        "age-based purge of ephemeral delivery-log / dedup-hash rows; the retention policy "
        "is operator-configured and the entity-bearing retention (run purge) IS audited"
    ),
    "derived_cache": "writes a derived cache/probe row re-derivable from its source of truth",
    "derived_state": ("re-derives a row from state that is already recorded elsewhere (idempotent, compare-and-set)"),
    "derived_analytics": "internal analytics facts maintenance; no user-visible entity change",
    "probe_bookkeeping": "probe watermark / cooldown state; the consequential action audits separately",
    "liveness": "per-tick liveness heartbeat or memory forensics — never audit internal bookkeeping",
    "telemetry_watermark": "opt-in outbound telemetry watermark advance",
    "internal_bookkeeping": "instance-internal scheduler/registration state, not org-owned data",
    "boot_default_config": (
        "idempotent creation of instance-default configuration at boot; re-runs every cold "
        "boot, has no actor and grants no privilege"
    ),
    "boot_env_config": (
        "idempotent boot seeding from an operator env var; every subsequent mutation of the "
        "seeded rows is audited through its admin route"
    ),
    "demo_fixture": "demo/sample data, gated off by default (MODULO_SEED_DEMO_ORGS)",
    "infra_container_gc": (
        "destroys orphaned sandbox containers rather than org rows; gated by "
        "RUNNER_RECONCILER_DESTROY_ENABLED (default off), logged per destroy "
        "(runner.reconciler.orphan_destroyed) and surfaced by the advisory health "
        "check. Re-affirmed exempt in FAR-1561: the sweep is cross-org (one event "
        "would have no single owning org), an orphan's run row may no longer exist "
        "(so resource_id has nothing to point at), and no org-owned entity changes "
        "hands — the run lifecycle that produced the container is audited on the run "
        "chain instead"
    ),
}

#: Every entry classified ``gap`` MUST carry a note saying what is missing and
#: where the remainder lives.
_GAP = "gap"
_AUDITED = "audited"
_EXEMPT = "exempt"


@dataclass(frozen=True)
class PathRecord:
    """Classification for one enumerated background write path."""

    classification: str
    reason: str = ""
    note: str = ""
    #: ``(repo-relative module path, literal marker)`` pairs that must all
    #: appear in the named file — the evidence the audit append is wired.
    evidence: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.classification not in {_AUDITED, _EXEMPT, _GAP}:
            raise ValueError(f"unknown classification {self.classification!r}")
        if self.classification == _EXEMPT and self.reason not in EXEMPT_REASONS:
            raise ValueError(
                f"exempt path must use a documented reason, got {self.reason!r}; known: {sorted(EXEMPT_REASONS)}"
            )
        if self.classification == _GAP and not self.note:
            raise ValueError("a gap must carry a note naming what is missing")
        if self.classification == _AUDITED and not self.evidence:
            raise ValueError("an audited path must name the file + marker proving the append")


def _audited(*pairs: tuple[str, str]) -> PathRecord:
    return PathRecord(classification=_AUDITED, evidence=tuple(pairs))


def _exempt(reason: str) -> PathRecord:
    return PathRecord(classification=_EXEMPT, reason=reason)


def _gap(note: str) -> PathRecord:
    return PathRecord(classification=_GAP, note=note)


_EXECUTOR = ("src/modulo/core/pipeline_engine/executor.py", 'event_type="run_started"')
_TERMINAL_ADVANCE = ("src/modulo/core/run_terminal_advance.py", 'event_type="run.sweep_terminalised"')

#: The inventory. Keys are either a bare SAQ/reconciler function name or
#: ``boot:<label>`` for an ``_boot_seed`` entry.
INVENTORY: dict[str, PathRecord] = {
    # --- runs-worker tasks (registered by saq_worker._runs_functions) -----
    "execute_run": _audited(_EXECUTOR),
    "resume_run": _audited(_EXECUTOR),
    "execute_suite_run": _audited(
        # FAR-1561: the SAQ job records the start + terminal outcome of the
        # SuiteRun it just committed (post-commit, re-selected state guard).
        ("src/modulo/core/saq_worker.py", 'event_type="suite_run_started"'),
        ("src/modulo/core/saq_worker.py", 'event_type="suite_run_completed"'),
    ),
    "fire_cron_trigger": _audited(_EXECUTOR),
    "fire_polling_trigger": _audited(_EXECUTOR),
    "fire_ongoing_trigger": _audited(_EXECUTOR),
    "fire_suite_run_trigger": _audited(
        # FAR-1561: creation is recorded before enqueue (and the enqueue-failure
        # path records the terminal `failed` too) with a `pending` re-select guard.
        ("src/modulo/core/saq_worker.py", 'event_type="suite_run_created"'),
    ),
    "fire_report_trigger": _exempt("trigger_bookkeeping"),
    # --- SuiteRun-creation entry points (public build_suite_run callers) --
    # FAR-1574: the SAQ seam above is the only path that actually runs. The
    # caller scan below still enumerates every PUBLIC function that reaches
    # ``build_suite_run``, so a future cron/REST/CLI caller cannot land
    # unclassified. Today it finds none: ``run_scheduled_suite`` was the only
    # one and was deleted as dead code (FAR-1561) — no production or test
    # caller ever reached it — so this section is deliberately empty rather
    # than exempt.
    # --- system-cron tasks (saq_worker._system_functions) -----------------
    "fire_due_triggers": _exempt("trigger_bookkeeping"),
    "dispatcher_reconcile": _audited(
        ("src/modulo/core/cron_helpers.py", "_record_terminalisation_audits(terminalized_run_ids)"),
        ("src/modulo/core/cron_helpers.py", 'event_type="run.sweep_terminalised"'),
    ),
    "claim_expiry": _audited(("src/modulo/core/hitl_manager/expiry_job.py", 'event_type="hitl.claim_expired"')),
    "hitl_overdue": _exempt("notification_only"),
    "hitl_deadline_warning": _exempt("notification_only"),
    "retention_cleanup": _audited(("src/modulo/core/saq_worker.py", 'event_type="run_retention_purge"')),
    "webhook_dedup_cleanup": _exempt("ephemeral_log_retention"),
    "expired_webhook_dedup_purge": _exempt("ephemeral_log_retention"),
    "trigger_events_cleanup": _exempt("ephemeral_log_retention"),
    "stale_run_recovery": _audited(
        ("src/modulo/core/pipeline_execution.py", 'source="stale_run_recovery"'),
        _TERMINAL_ADVANCE,
    ),
    "slot_reconciliation": _audited(
        ("src/modulo/core/run_admission.py", 'source="slot_reconciliation"'),
        _TERMINAL_ADVANCE,
    ),
    "hitl_park_sweep": _audited(("src/modulo/core/run_admission.py", 'event_type="hitl.run_parked"')),
    "runner_marker_sweep": _audited(
        ("src/modulo/core/runner_capacity.py", 'actor_source="runner_marker_sweep"'),
        _TERMINAL_ADVANCE,
    ),
    "runner_workspace_reconcile": _exempt("infra_container_gc"),
    "runner_health_probe": _exempt("derived_cache"),
    "cost_probe": _exempt("probe_bookkeeping"),
    "analytics_facts_maintenance": _exempt("derived_analytics"),
    "journey_reconcile": _exempt("derived_state"),
    "check_missed_fire_alerts_cron": _exempt("notification_only"),
    "library_sync": _exempt("derived_state"),
    "metrics_dump": _exempt("telemetry_watermark"),
    "connector_health_checks": _exempt("derived_cache"),
    "health_readiness_alert": _exempt("notification_only"),
    "memory_monitor_cron": _exempt("liveness"),
    # --- reconcilers / sweep helpers declared under core/ -----------------
    "reconcile_facts": _exempt("derived_analytics"),
    "reconcile_runner_workspaces": _exempt("infra_container_gc"),
    "run_classification_reconcile": _exempt("derived_state"),
    "reconcile_journeys": _exempt("derived_state"),
    "reconcile_missing_classifications": _exempt("derived_state"),
    "reconcile_noop_evidence": _exempt("derived_state"),
    "reconcile_pipeline_slots": _audited(
        ("src/modulo/core/run_admission.py", 'source="slot_reconciliation"'),
        _TERMINAL_ADVANCE,
    ),
    "reconcile_runner_dispatch_markers": _audited(
        ("src/modulo/core/runner_capacity.py", 'event_type="run.sweep_terminalised"')
    ),
    "reconcile_cron_registrations": _exempt("internal_bookkeeping"),
    "stale_run_recovery_sweep": _audited(
        ("src/modulo/core/pipeline_execution.py", 'source="stale_run_recovery"'),
        _TERMINAL_ADVANCE,
    ),
    "maybe_alarm_approve_sweep": _audited(("src/modulo/core/hitl_manager/sweep_alarm.py", "append_audit_event")),
    # --- boot-time seeds declared under core/ -----------------------------
    "seed_default_alert_rules_for_org": _exempt("boot_default_config"),
    "seed_default_alert_rules": _exempt("boot_default_config"),
    "seed_cost_components_for_org": _exempt("boot_default_config"),
    "seed_cost_components": _exempt("boot_default_config"),
    "seed_demo_org": _exempt("demo_fixture"),
    "seed_demo_orgs": _exempt("demo_fixture"),
    # --- boot seeds enumerated from api/main.py ---------------------------
    "boot:modulo_users": _audited(
        # FAR-1561: the seeder appends a SYSTEM-actor `user_seeded` (and
        # `user_rehashed`, which can GRANT the admin role) in the seeding
        # transaction — grant and record commit atomically.
        ("src/modulo/db/seed.py", 'event_type="user_seeded"'),
    ),
    "boot:sso_providers": _exempt("boot_env_config"),
    "boot:system_schemas": _exempt("boot_default_config"),
    "boot:environment_profiles": _exempt("boot_default_config"),
    "boot:tier_catalog": _exempt("boot_default_config"),
    "boot:cost_components": _exempt("boot_default_config"),
    "boot:demo_orgs": _exempt("demo_fixture"),
    "boot:demo_user": _exempt("demo_fixture"),
}


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _saq_system_function_names() -> set[str]:
    """Names SAQ registers on the system worker (import-time, settings-free)."""
    from modulo.core import saq_worker as sw

    return {fn.__name__ for fn in sw._system_functions()}


def _saq_runs_function_names() -> set[str]:
    """Names SAQ registers on the runs worker (import-time, settings-free)."""
    from modulo.core import saq_worker as sw

    return {fn.__name__ for _registered_name, fn in sw._runs_functions()}


def _saq_cron_function_names() -> set[str]:
    """Function names bound to a ``CronJob(...)`` in ``_system_cron_jobs``.

    AST-read rather than called: building the list reads settings (the library
    sync cadence), which an architecture test must not depend on.
    """
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


def _public_function_calls(path: Path) -> dict[str, set[str]]:
    """Public module-level functions in *path* -> the names their bodies call.

    Nested/inner functions are ignored on purpose: the inventory classifies
    module-level entry points, and a private helper is covered by the public
    caller that reaches it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        called: set[str] = set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                called.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                called.add(call.func.attr)
        calls.setdefault(node.name, set()).update(called)
    return calls


def _suite_run_entry_point_names(*, sources: list[Path] | None = None) -> set[str]:
    """Public module-level functions that create a ``SuiteRun`` (FAR-1574).

    ``build_suite_run`` is the single statement that INSERTs a ``suite_runs``
    row, so it seeds the frontier; the walk then expands over PUBLIC callers
    until the frontier stops growing. That second hop is what makes the gate
    honest about surfaces: wiring an existing entry point into a new cron job,
    REST route or CLI command shows up as a NEW enumerated name the inventory
    must classify, not as an invisible side entrance to the SAQ seam.

    *sources* overrides the scanned files (test seam) — production callers
    pass nothing and get the whole ``src/modulo`` tree.
    """
    paths = sorted(SRC_DIR.rglob("*.py")) if sources is None else sorted(sources)
    callers: dict[str, set[str]] = {}
    for path in paths:
        if "migrations" in path.parts:
            continue
        for name, called in _public_function_calls(path).items():
            callers.setdefault(name, set()).update(called)

    frontier = {_SUITE_RUN_BUILDER}
    reached: set[str] = set()
    while True:
        new = {name for name, called in callers.items() if name not in reached and called & frontier}
        if not new:
            return reached
        reached |= new
        frontier |= new


def _boot_seed_labels() -> set[str]:
    """``_boot_seed("<label>", ...)`` labels declared in ``api/main.py``."""
    tree = ast.parse(API_MAIN_PATH.read_text(encoding="utf-8"))
    labels: set[str] = set()
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_boot_seed"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            labels.add(call.args[0].value)
    if not labels:
        raise AssertionError("no _boot_seed(...) calls found in api/main.py")
    return {f"boot:{label}" for label in labels}


def _reconciler_function_names() -> set[str]:
    """Module-level ``reconcile_`` / ``seed_`` / ``*_sweep|_reconcile|_cleanup``
    functions declared anywhere under ``src/modulo/core``."""
    names: set[str] = set()
    for path in sorted(CORE_DIR.rglob("*.py")):
        rel = path.relative_to(CORE_DIR).as_posix()
        if rel.startswith(_SKIP_DIR_PREFIX) or rel == _SKIP_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
                and _RECONCILER_NAME.search(node.name)
            ):
                names.add(node.name)
    if not names:
        raise AssertionError("the reconciler/seed enumeration matched nothing")
    return names


def _enumerated_paths() -> dict[str, str]:
    """All enumerated paths -> the enumeration source that produced each."""
    enumerated: dict[str, str] = {}
    for name in _saq_system_function_names():
        enumerated[name] = "_system_functions()"
    for name in _saq_runs_function_names():
        enumerated.setdefault(name, "_runs_functions()")
    for name in _saq_cron_function_names():
        enumerated.setdefault(name, "_system_cron_jobs()")
    for name in _reconciler_function_names():
        enumerated.setdefault(name, "core/ reconciler-sweep-seed scan")
    for name in _suite_run_entry_point_names():
        enumerated.setdefault(name, "build_suite_run caller scan (FAR-1574)")
    for name in _boot_seed_labels():
        enumerated.setdefault(name, "_boot_seed() in api/main.py")
    return enumerated


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_enumeration_finds_the_expected_registration_sites() -> None:
    """Guard the enumerators themselves: an enumerator that silently returns
    nothing would make every other gate pass vacuously."""
    assert "dispatcher_reconcile" in _saq_system_function_names()
    assert "execute_run" in _saq_runs_function_names()
    assert "fire_due_triggers" in _saq_cron_function_names()
    assert "reconcile_journeys" in _reconciler_function_names()
    assert "boot:modulo_users" in _boot_seed_labels()


def test_suite_run_caller_scan_flags_a_future_public_caller(tmp_path: Path) -> None:
    """Guard the SuiteRun-creation scan itself (FAR-1574, kept by FAR-1561).

    Since ``run_scheduled_suite`` was deleted as dead code the production scan
    legitimately returns NOTHING — a vacuous result the old
    ``"run_scheduled_suite" in _suite_run_entry_point_names()`` guard used to
    rule out. Prove the mechanism instead: a synthetic module whose public
    function reaches ``build_suite_run`` must be enumerated, else a future
    cron/REST/CLI wiring would land unclassified and this half of the ratchet
    would pass vacuously.
    """
    synthetic = tmp_path / "future_suite_entry.py"
    synthetic.write_text(
        "async def future_suite_cron_entry(session: object) -> object:\n    return await build_suite_run(session)\n",
        encoding="utf-8",
    )
    assert _suite_run_entry_point_names(sources=[synthetic]) == {"future_suite_cron_entry"}


def test_every_enumerated_background_path_is_classified() -> None:
    """A NEW SAQ task, cron job, reconciler, seeder or boot seed must be
    classified before it can land — this is the ratchet."""
    missing = sorted(set(_enumerated_paths()) - set(INVENTORY))
    assert not missing, (
        "unclassified background write path(s) — add each to INVENTORY in "
        "tests/architecture/test_background_audit_coverage.py as audited "
        "(with file+marker evidence), exempt (with a documented reason from "
        "EXEMPT_REASONS) or gap (with a note):\n  " + "\n  ".join(missing)
    )


def test_no_stale_inventory_entries() -> None:
    """A renamed or removed path must be re-classified, not left rotting."""
    stale = sorted(set(INVENTORY) - set(_enumerated_paths()))
    assert not stale, "inventory entries no longer enumerated (renamed or deleted):\n  " + "\n  ".join(stale)


def test_every_cron_job_is_a_registered_system_function() -> None:
    """A ``CronJob(...)`` bound to an unregistered function cannot run — and
    would be invisible to ``_system_functions``-keyed classification."""
    unregistered = sorted(_saq_cron_function_names() - _saq_system_function_names())
    assert not unregistered, "cron jobs whose function is not registered on the system worker:\n  " + "\n  ".join(
        unregistered
    )


def test_audited_paths_point_at_a_real_file_containing_their_marker() -> None:
    """Evidence check: the named module exists and still contains the literal
    that proves the audit append is wired."""
    failures: list[str] = []
    for name, record in sorted(INVENTORY.items()):
        if record.classification != _AUDITED:
            continue
        for rel_path, marker in record.evidence:
            path = BACKEND_DIR / rel_path
            if not path.is_file():
                failures.append(f"{name}: evidence file missing: {rel_path}")
                continue
            if marker not in path.read_text(encoding="utf-8"):
                failures.append(f"{name}: marker {marker!r} not found in {rel_path}")
    assert not failures, "audited background path(s) without their evidence:\n  " + "\n  ".join(failures)


def test_exempt_paths_use_only_documented_reasons() -> None:
    """The vocabulary check also runs at inventory construction (``__post_init__``);
    this test documents the contract for readers."""
    for name, record in sorted(INVENTORY.items()):
        if record.classification == _EXEMPT:
            assert record.reason in EXEMPT_REASONS, f"{name}: undocumented exempt reason {record.reason!r}"


def test_every_gap_is_visible_and_actionable() -> None:
    """A gap must say what is missing and where the remainder lives."""
    for name, record in sorted(INVENTORY.items()):
        if record.classification != _GAP:
            continue
        note = record.note.lower()
        assert len(note) > 40, f"{name}: gap note too thin to act on: {record.note!r}"
        assert "remainder" in note or "slice" in note, (
            f"{name}: a gap note must point at the remaining work, not just name the hole: {record.note!r}"
        )


def test_inventory_covers_both_endpoints_of_the_registration_surface() -> None:
    """Sanity: the inventory must actually span both workers and the boot
    seeds, not just one convenient source."""
    assert any(name.startswith("boot:") for name in INVENTORY)
    assert "execute_run" in INVENTORY
    assert "retention_cleanup" in INVENTORY
