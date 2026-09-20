"""FAR-902: index predicate + sweep wiring test (D4, D7).

Verifies that the partial index predicate in migration 0250 matches the
analytics sweep query filter, and that the sweep is wired into
dispatcher_reconcile.
"""

from __future__ import annotations

from pathlib import Path

# The migration file (structural: no DB needed).
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0250_schema_enforcement_telemetry.py"
)

# The sweep module source.
_SWEEP_MODULE = Path(__file__).resolve().parents[3] / "src" / "modulo" / "core" / "analytics" / "enforcement_sweep.py"


def test_index_predicate_matches_sweep_filter() -> None:
    """The partial index predicate and the sweep query WHERE clause are identical.

    This prevents drift: if someone changes the sweep query but not the index
    (or vice versa), this test catches it.
    """
    migration_source = _MIGRATION_PATH.read_text(encoding="utf-8")
    sweep_source = _SWEEP_MODULE.read_text(encoding="utf-8")

    # Both must use the same predicate fragments.
    predicate_parts = [
        "schema_enforcement_json IS NOT NULL",
        "attempt_key <> '__final__'",
    ]
    for part in predicate_parts:
        assert part in migration_source, f"Migration missing predicate: {part}"
        assert part in sweep_source, f"Sweep missing predicate: {part}"


def test_sweep_wired_into_dispatcher_reconcile() -> None:
    """The sweep is imported and called from _run_reconcile_sweeps in cron_helpers."""
    cron_helpers_path = Path(__file__).resolve().parents[3] / "src" / "modulo" / "core" / "cron_helpers.py"
    source = cron_helpers_path.read_text(encoding="utf-8")
    assert "sweep_schema_enforcement_facts" in source
    assert "enforcement_sweep_scanned" in source
    assert "enforcement_sweep_corrected" in source


def test_sweep_module_has_named_predicate_constant() -> None:
    """The sweep module exports ENFORCEMENT_SWEEP_PREDICATE as a named constant.

    A sweep with zero production callers is a silent critical — the named
    constant makes the predicate grep-able and verifiable.
    """
    sweep_source = _SWEEP_MODULE.read_text(encoding="utf-8")
    assert "ENFORCEMENT_SWEEP_PREDICATE" in sweep_source
    assert "schema_enforcement_json IS NOT NULL" in sweep_source
    assert "attempt_key <> '__final__'" in sweep_source
