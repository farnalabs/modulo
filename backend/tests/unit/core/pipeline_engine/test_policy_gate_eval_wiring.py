"""Executor-path wiring for FAR-967 chunk 10 (criteria 8, 9, 10).

These tests exercise the REAL eval-def build the run uses
(``PipelineExecutor._load_eval_defs_for_pipeline`` +
``_build_eval_defs_by_node``) — the ONE authoritative site for the operator
control (F8 single-sourcing; the former duplicate filter helper at the
run-start interception seam was removed). Criterion 8's exclusion must be
load-bearing on the run's own evaluation path:

* **C8** — a live ``PolicyGate`` with ``enabled = false`` is excluded from
  the evaluated set: ``failure_behaviour`` degrades to ``"warn"`` and
  ``policy_gate_id`` is ``None`` (so no ``PolicyGateDecision`` row can be
  written — ``run_evals_persist_before_decide`` only builds a decision
  snapshot when ``policy_gate_id is not None``).  Remove the enabled check
  and the assertions fail.
* **C9** — operator control OVERRIDES the pin: a snapshot pinning
  ``action: "block"`` whose live row is disabled is skipped; the run is not
  blocked by that gate.
* **C10** — the pin's ``action`` governs: a pinned ``block`` with the live
  row edited to ``warn`` still resolves ``block`` when the gate is enabled.
* Pin membership is the evaluation universe: an unpinned gate is never
  evaluated, an empty pin set governs nothing, and a legacy ``None`` pin
  set still falls back to the live gates.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.core.pipeline_engine.executor import PipelineExecutor

# ---------------------------------------------------------------------------
# Helpers — duck-typed (Eval, PolicyGate) row pairs, the shape the loader
# returns from ``select(Eval, PolicyGate).outerjoin(...)``.
# ---------------------------------------------------------------------------


def _eval_row(
    *,
    eval_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    eval_type: str = "regex",
    pipeline_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=eval_id or uuid.uuid4(),
        node_id=node_id or uuid.uuid4(),
        name="wiring-eval",
        eval_type=eval_type,
        config_json={"pattern": "x", "field": "out"},
        pass_threshold=None,
        suite_id=None,
        version=1,
        pipeline_id=pipeline_id or uuid.uuid4(),
        organisation_id=uuid.uuid4(),
    )


def _gate_row(
    eval_row: SimpleNamespace,
    *,
    action: str = "block",
    enabled: bool = True,
    gate_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=gate_id or uuid.uuid4(),
        eval_id=eval_row.id,
        node_id=eval_row.node_id,
        action=action,
        enabled=enabled,
        version=3,
        deleted_at=None,
        organisation_id=eval_row.organisation_id,
    )


def _pin(eval_row: SimpleNamespace, gate_row: SimpleNamespace, *, action: str = "block") -> dict[str, str]:
    return {
        "policy_gate_id": str(gate_row.id),
        "eval_id": str(eval_row.id),
        "action": action,
        "node_id": str(gate_row.node_id),
    }


def _only(rows: list[tuple[Any, Any]], pins: list[dict[str, Any]] | None) -> Any:
    """Build the eval-def map and return the single DTO it contains."""
    by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid.uuid4(), uuid.uuid4(), pins)
    assert len(by_node) == 1
    return next(iter(by_node.values()))[0]


# ---------------------------------------------------------------------------
# C8 — disabled gate excluded from the evaluated set (live fallback)
# ---------------------------------------------------------------------------


def test_c8_disabled_gate_excluded_in_live_fallback() -> None:
    """A node-scoped eval whose live gate is DISABLED evaluates as ungoverned:
    warn (never blocks) and ``policy_gate_id is None`` (no decision row).
    This is the run's own build path — remove the ``enabled`` re-check and
    the DTO keeps ``block`` + the gate id, failing both assertions."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=False)

    dto = _only([(eval_row, gate_row)], None)

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None
    assert dto.policy_gate_version is None
    assert dto.policy_gate_node_id is None


def test_c8_sibling_enabled_gate_still_governs_in_live_fallback() -> None:
    """Load-bearing sibling: the SAME row with ``enabled = true`` still
    yields ``block`` + the gate id — proving the exclusion above is caused
    by ``enabled`` alone, not by the surrounding code."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)

    dto = _only([(eval_row, gate_row)], None)

    assert dto.failure_behaviour == "block"
    assert dto.policy_gate_id == gate_row.id


def test_c8_disabled_gate_does_not_emit_backfill_anomaly(caplog: pytest.LogCaptureFixture) -> None:
    """A disabled gate is a deliberate operator state, not a missing binding —
    the build must not emit the ``eval_defs.node_without_gate`` anomaly
    warning (that warning means a rejected backfill binding) and must record
    the operator-control exclusion instead."""
    import logging

    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, enabled=False)

    with caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.executor"):
        dto = _only([(eval_row, gate_row)], None)

    assert dto.failure_behaviour == "warn"
    anomaly = [r for r in caplog.records if r.getMessage() == "eval_defs.node_without_gate"]
    assert not anomaly
    excluded = [r for r in caplog.records if r.getMessage() == "eval_defs.gate_disabled_excluded"]
    assert excluded


def test_c8_missing_gate_still_keeps_the_backfill_anomaly(caplog: pytest.LogCaptureFixture) -> None:
    """The anomaly warning itself is preserved for a genuinely gate-less
    node-scoped non-guardrail eval (legacy behaviour must not regress)."""
    import logging

    eval_row = _eval_row()

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.executor"):
        dto = _only([(eval_row, None)], None)

    assert dto.failure_behaviour == "warn"
    anomaly = [r for r in caplog.records if r.getMessage() == "eval_defs.node_without_gate"]
    assert anomaly


# ---------------------------------------------------------------------------
# C9 — the operator control overrides the pin
# ---------------------------------------------------------------------------


def test_c9_disabled_live_gate_overrides_a_pinned_block() -> None:
    """A snapshot pinning ``action: "block"`` whose live row is now DISABLED
    is skipped (§5.2): warn + no gate metadata — the run is NOT blocked by
    this gate, whatever the pin says."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=False)
    pins = [_pin(eval_row, gate_row, action="block")]

    dto = _only([(eval_row, gate_row)], pins)

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


def test_c9_pinned_gate_with_no_live_row_is_skipped() -> None:
    """A pinned gate whose live row is gone (soft-deleted since snapshot
    creation) is skipped, mirroring the guardrail-pin soft-delete skip."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block")
    pins = [_pin(eval_row, gate_row, action="block")]

    dto = _only([(eval_row, None)], pins)

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


# ---------------------------------------------------------------------------
# C10 — the pin's action governs, not the live row
# ---------------------------------------------------------------------------


def test_c10_pinned_block_wins_over_live_action_edit_to_warn() -> None:
    """The pin captured ``block`` at snapshot creation; the live row has since
    been edited to ``warn`` (and is enabled). The run resolves ``block`` —
    a live edit cannot re-score an in-flight run. The decision metadata
    carries the live row's identity (id/version) and the PINNED node."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="warn", enabled=True)
    pins = [_pin(eval_row, gate_row, action="block")]

    dto = _only([(eval_row, gate_row)], pins)

    assert dto.failure_behaviour == "block"
    assert dto.policy_gate_id == gate_row.id
    assert dto.policy_gate_version == gate_row.version
    assert dto.policy_gate_node_id == gate_row.node_id


def test_c10_pinned_warn_wins_over_live_action_edit_to_block() -> None:
    """The symmetric direction: a pin capturing ``warn`` must NOT escalate to
    ``block`` because the live row was hardened to ``block`` mid-run."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)
    pins = [_pin(eval_row, gate_row, action="warn")]

    dto = _only([(eval_row, gate_row)], pins)

    assert dto.failure_behaviour == "warn"


# ---------------------------------------------------------------------------
# Pin membership is the evaluation universe (§3.1, §5.4)
# ---------------------------------------------------------------------------


def test_unpinned_live_gate_is_never_evaluated_in_a_pinned_run() -> None:
    """A gate absent from the pin set (created after the snapshot, or
    disabled at snapshot creation and re-enabled since) is never evaluated
    for this run — pin membership is immutable."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)

    dto = _only([(eval_row, gate_row)], [])

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


def test_empty_pin_set_governs_everything_as_ungoverned() -> None:
    """Case (ii): an EMPTY (but present) pin list means zero gates — even a
    live ``block`` row cannot govern. Distinguishable from ``None`` (legacy
    fallback below)."""
    eval_a = _eval_row()
    eval_b = _eval_row()
    gate_a = _gate_row(eval_a, action="block")
    gate_b = _gate_row(eval_b, action="block")

    by_node = PipelineExecutor._build_eval_defs_by_node(
        [(eval_a, gate_a), (eval_b, gate_b)],
        uuid.uuid4(),
        uuid.uuid4(),
        [],
    )

    dtos = [d for defs in by_node.values() for d in defs]
    assert len(dtos) == 2
    for dto in dtos:
        assert dto.failure_behaviour == "warn"
        assert dto.policy_gate_id is None


def test_legacy_none_pins_fall_back_to_live_gates() -> None:
    """Case (i): ``policy_gate_pins_json IS NULL`` — the live enabled action
    governs (the legacy pre-pinning behaviour)."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)

    dto = _only([(eval_row, gate_row)], None)

    assert dto.failure_behaviour == "block"
    assert dto.policy_gate_id == gate_row.id


def test_pins_are_keyed_by_eval_and_survive_ordering() -> None:
    """Each pin resolves through its ``eval_id`` regardless of list order —
    two evals with pins each get their OWN pinned action."""
    eval_a = _eval_row()
    eval_b = _eval_row()
    gate_a = _gate_row(eval_a, action="warn")
    gate_b = _gate_row(eval_b, action="warn")
    pin_a = _pin(eval_a, gate_a, action="block")
    pin_b = _pin(eval_b, gate_b, action="warn")

    by_node = PipelineExecutor._build_eval_defs_by_node(
        [(eval_a, gate_a), (eval_b, gate_b)],
        uuid.uuid4(),
        uuid.uuid4(),
        [pin_b, pin_a],
    )

    dtos = {d.id: d for defs in by_node.values() for d in defs}
    assert dtos[eval_a.id].failure_behaviour == "block"
    assert dtos[eval_b.id].failure_behaviour == "warn"


def test_replaced_pinned_gate_is_skipped() -> None:
    """The pin names gate X, but the live row for that eval is a DIFFERENT
    gate (X was soft-deleted and re-created) — the pinned row is gone, so
    the gate is skipped rather than silently re-bound to the new row."""
    eval_row = _eval_row()
    original_gate = _gate_row(eval_row, action="block")
    replacement_gate = _gate_row(eval_row, action="block")
    pins = [_pin(eval_row, original_gate, action="block")]

    dto = _only([(eval_row, replacement_gate)], pins)

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


def test_malformed_pin_action_never_blocks() -> None:
    """A pin whose ``action`` is outside the warn/block vocabulary cannot
    drive a decision row (a stray value would land verbatim in
    ``PolicyGateDecision.resolved_action``) — it degrades to warn."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)
    pin = _pin(eval_row, gate_row, action="block")
    pin["action"] = "detonate"

    dto = _only([(eval_row, gate_row)], [pin])

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


def test_malformed_pin_node_id_falls_back_to_the_live_node(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pin whose ``node_id`` is not a UUID cannot be used as the decision
    row's gate node — the build logs and falls back to the live gate's node
    rather than raising mid-run."""
    import logging

    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)
    pin = _pin(eval_row, gate_row, action="block")
    pin["node_id"] = "not-a-uuid"

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.executor"):
        dto = _only([(eval_row, gate_row)], [pin])

    assert dto.failure_behaviour == "block"
    assert dto.policy_gate_id == gate_row.id
    assert dto.policy_gate_node_id == gate_row.node_id
    assert any(r.getMessage() == "eval_defs.pin_node_id_invalid" for r in caplog.records)


def test_absent_pin_node_id_uses_the_live_gate_node() -> None:
    """A pin carrying no ``node_id`` (falsy) keeps the live gate's node as the
    decision row's node instead of blanking it."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)
    pin = _pin(eval_row, gate_row, action="block")
    pin["node_id"] = ""

    dto = _only([(eval_row, gate_row)], [pin])

    assert dto.failure_behaviour == "block"
    assert dto.policy_gate_id == gate_row.id
    assert dto.policy_gate_node_id == gate_row.node_id


def test_malformed_pin_entries_are_ignored_when_keying_the_universe() -> None:
    """The pinned universe is keyed defensively: non-dict entries and entries
    without an ``eval_id`` are skipped, so a malformed pin list degrades to an
    empty pinned universe (governs nothing) rather than crashing the build."""
    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=True)
    pins = ["not-a-dict", {"node_id": "x"}, None]

    dto = _only([(eval_row, gate_row)], pins)  # type: ignore[arg-type]

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


# ---------------------------------------------------------------------------
# Preserved behaviour
# ---------------------------------------------------------------------------


def test_guardrail_typed_eval_without_a_gate_keeps_warn() -> None:
    """Guardrail-typed Evals keep warn semantics by design (no gate row)."""
    eval_row = _eval_row(eval_type="guardrail")

    dto = _only([(eval_row, None)], None)

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


def test_disabled_guardrail_typed_eval_with_gate_is_warn() -> None:
    """A guardrail-typed eval whose gate is disabled is ungoverned too —
    warn, no gate metadata (the guardrail pass owns block for guardrails)."""
    eval_row = _eval_row(eval_type="guardrail")
    gate_row = _gate_row(eval_row, action="block", enabled=False)

    dto = _only([(eval_row, gate_row)], None)

    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


def test_suite_scoped_eval_rows_are_skipped() -> None:
    """A row with no ``node_id`` never enters the map (pre-existing
    behaviour — suite-scoped evals are not node-governed)."""
    eval_row = _eval_row()
    eval_row.node_id = None  # suite-scoped eval
    gate_row = _gate_row(eval_row, action="block")

    by_node = PipelineExecutor._build_eval_defs_by_node(
        [(eval_row, gate_row)],
        uuid.uuid4(),
        uuid.uuid4(),
        None,
    )

    assert not by_node


# ---------------------------------------------------------------------------
# The loader hands disabled gate rows THROUGH (the build filters them)
# ---------------------------------------------------------------------------


async def test_loader_passes_disabled_gate_rows_through_to_the_build() -> None:
    """``_load_eval_defs_for_pipeline`` must NOT filter ``enabled`` at SQL
    level: the build needs the row (its ``enabled`` state is the operator
    re-check, §5.2 — loaded alongside ``action``/``node_id`` in the same
    once-per-run query) to tell a deliberately-disabled gate apart from a
    gate that never existed.  The loader keeps the soft-delete filter only."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy.dialects import postgresql

    eval_row = _eval_row()
    gate_row = _gate_row(eval_row, action="block", enabled=False)
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [(eval_row, gate_row)]
    session.execute = AsyncMock(return_value=result)

    executor = PipelineExecutor(MagicMock())
    rows = await executor._load_eval_defs_for_pipeline(session, uuid.uuid4())

    assert rows == [(eval_row, gate_row)]
    stmt = session.execute.call_args[0][0]
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "policy_gates.deleted_at IS NULL" in compiled
    # The enabled state must reach the build, not be dropped in the JOIN.
    assert "policy_gates.enabled IS true" not in compiled

    # …and the build then excludes that same disabled row (C8 end-to-end).
    dto = _only(rows, None)
    assert dto.failure_behaviour == "warn"
    assert dto.policy_gate_id is None


# ---------------------------------------------------------------------------
# Threading: BOTH run paths must feed the snapshot's pins into the build
# ---------------------------------------------------------------------------
#
# Replaced (FAR-967 F9) by the BEHAVIOURAL tests in
# ``tests/unit/pipeline_engine/test_executor_eval_defs_freshness.py``:
#   * ``test_execute_threads_snapshot_pins_into_the_build``
#   * ``test_resume_threads_snapshot_pins_into_the_build``
# which run execute()/resume() for real and assert the captured
# ``_build_eval_defs_by_node`` arguments equal ``snapshot.policy_gate_pins_json``
# (the old version here asserted inspect.getsource() text — a source-string
# check that never exercised the call sites).
#
# The same file carries the FAR-967 F1 pin-integrity backstop tests
# (execute/resume terminalize on fingerprint mismatch).
