"""Composite-engine HITL trip-wire (hitl-gate-removal-guard-plan.md v19 §3.6).

The composite-template path has no real HITL write path; this regression test
asserts zero HITL references in ``core/composite_engine/`` so a future change
cannot silently add a gate-weakening-capable write there without review.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit._source_scan import iter_source_text

_PKG = Path(__file__).resolve().parents[3] / "src" / "modulo" / "core" / "composite_engine"

_FORBIDDEN = ("hitl", "human_only", "claim_expiry", "required_team_id", "gate_config")


def test_composite_engine_has_zero_hitl_references() -> None:
    assert _PKG.is_dir(), f"composite_engine package missing: {_PKG}"
    files = list(iter_source_text(_PKG))
    assert files, "expected at least one module in composite_engine"
    violations: list[str] = []
    for path, text in files:
        low = text.lower()
        violations.extend(f"{path.name}: contains '{token}'" for token in _FORBIDDEN if token in low)
    assert not violations, (
        "core/composite_engine must not reference HITL gates (no write path exists; "
        "hitl-gate-removal-guard-plan.md v19 §3.6). Violations:\n" + "\n".join(violations)
    )
