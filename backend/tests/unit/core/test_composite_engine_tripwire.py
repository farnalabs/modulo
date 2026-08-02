"""Composite-engine HITL trip-wire (hitl-gate-removal-guard-plan.md v19 §3.6).

The composite-template path has no real HITL write path; this regression test
asserts zero HITL references in ``core/composite_engine/`` so a future change
cannot silently add a gate-weakening-capable write there without review.
"""

from __future__ import annotations

from pathlib import Path

_PKG = Path(__file__).resolve().parents[3] / "src" / "modulo" / "core" / "composite_engine"

_FORBIDDEN = ("hitl", "human_only", "claim_expiry", "required_team_id", "gate_config")


def test_composite_engine_has_zero_hitl_references() -> None:
    assert _PKG.is_dir(), f"composite_engine package missing: {_PKG}"
    files = sorted(_PKG.rglob("*.py"))
    assert files, "expected at least one module in composite_engine"
    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        for token in _FORBIDDEN:
            if token in low:
                violations.append(f"{path.name}: contains '{token}'")
    assert not violations, (
        "core/composite_engine must not reference HITL gates (no write path exists; "
        "hitl-gate-removal-guard-plan.md v19 §3.6). Violations:\n" + "\n".join(violations)
    )
