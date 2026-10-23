"""FAR-2054: seam-scoped guardrail coverage contract.

Alpha states guardrail coverage per seam, exhaustively, and may claim
``enforced`` only where MODULO ITSELF acted (ADR-042 ``attested``). These tests
pin the two structural guards that make an over-claim impossible:

* an ``enforced`` statement with any provenance other than ``attested`` is
  rejected by the model (a runtime-/derived-sourced value can never produce it);
* the read-path parser always returns ``runtime_loop`` as ``not_applicable``
  with its fixed reason, whatever a stored value claims — so stubbing a runtime
  event source cannot yield "full coverage".

They also pin exhaustiveness (every known seam always present), the
``not_applicable``-carries-a-reason rule, and that malformed persisted data
degrades explicitly instead of round-tripping arbitrary strings.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.core.guardrails.coverage import (
    RUNTIME_LOOP_UNAVAILABLE_REASON,
    SEAM_INGESTION_EDGE,
    SEAM_NODE_OUTPUT_EDGE,
    SEAM_ORDER,
    SEAM_RUNTIME_LOOP,
    SeamCoverageProvenance,
    SeamCoverageState,
    SeamCoverageStatement,
    derive_seam_coverage,
    parse_seam_coverage,
)
from modulo.core.provenance import PROVENANCE_VALUES

_INGESTION_SUMMARY = {"bound": 2, "evaluated": 2, "errored": 0}


def _by_seam(statements: list[SeamCoverageStatement]) -> dict[str, SeamCoverageStatement]:
    return {statement.seam: statement for statement in statements}


# ---------------------------------------------------------------------------
# The ADR-042 vocabulary is the whole vocabulary
# ---------------------------------------------------------------------------


def test_provenance_enum_matches_the_adr042_vocabulary() -> None:
    """The typed enum must cover EVERY ADR-042 class (never a two-value subset)."""
    assert frozenset(member.value for member in SeamCoverageProvenance) == PROVENANCE_VALUES


# ---------------------------------------------------------------------------
# Anti-overclaim guard: enforced requires attested
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provenance",
    [SeamCoverageProvenance.OBSERVED, SeamCoverageProvenance.DERIVED, SeamCoverageProvenance.REPORTED],
)
def test_enforced_statement_without_attested_provenance_is_rejected(
    provenance: SeamCoverageProvenance,
) -> None:
    """A non-attested source can never be published as ``enforced``.

    This fails if the model validator is removed — the exact regression the
    ticket's honesty guard exists to stop.
    """
    with pytest.raises(ValidationError):
        SeamCoverageStatement(
            seam=SEAM_INGESTION_EDGE,
            state=SeamCoverageState.ENFORCED,
            provenance=provenance,
            reason="a runtime plugin reported this seam as enforced",
        )


def test_enforced_statement_with_attested_provenance_is_accepted() -> None:
    """The one legitimate ``enforced`` shape is a Modulo-attested statement."""
    statement = SeamCoverageStatement(
        seam=SEAM_INGESTION_EDGE,
        state=SeamCoverageState.ENFORCED,
        provenance=SeamCoverageProvenance.ATTESTED,
        reason="Modulo evaluated the ingestion-edge guardrail",
    )
    assert statement.state is SeamCoverageState.ENFORCED


def test_runtime_loop_is_always_not_applicable_even_when_stored_as_enforced() -> None:
    """No runtime-sourced event can produce ``enforced`` for the runtime loop.

    A stored (or plugin-reported) ``enforced``/``attested`` claim for
    ``runtime_loop`` is still clamped: Alpha has no verifiable tool-event
    contract, so the honest state is ``not_applicable``. This fails if the
    read-path guard is removed.
    """
    raw = [
        {
            "seam": SEAM_RUNTIME_LOOP,
            "state": SeamCoverageState.ENFORCED.value,
            "provenance": SeamCoverageProvenance.ATTESTED.value,
            "reason": "plugin reports in-loop enforcement",
        }
    ]

    parsed = parse_seam_coverage(raw)

    assert parsed is not None
    runtime = _by_seam(parsed)[SEAM_RUNTIME_LOOP]
    assert runtime.state is SeamCoverageState.NOT_APPLICABLE
    assert runtime.provenance is SeamCoverageProvenance.DERIVED
    assert runtime.reason == RUNTIME_LOOP_UNAVAILABLE_REASON


def test_runtime_sourced_full_coverage_does_not_survive_the_read_path() -> None:
    """Stubbing the event source with "everything enforced" cannot yield full coverage."""
    raw = [
        {
            "seam": seam,
            "state": SeamCoverageState.ENFORCED.value,
            "provenance": SeamCoverageProvenance.REPORTED.value,
            "reason": "runtime reported enforcement",
        }
        for seam in SEAM_ORDER
    ]

    parsed = parse_seam_coverage(raw)

    assert parsed is not None
    by_seam = _by_seam(parsed)
    assert by_seam[SEAM_RUNTIME_LOOP].state is SeamCoverageState.NOT_APPLICABLE
    assert by_seam[SEAM_INGESTION_EDGE].state is not SeamCoverageState.ENFORCED
    assert by_seam[SEAM_NODE_OUTPUT_EDGE].state is not SeamCoverageState.ENFORCED


# ---------------------------------------------------------------------------
# Exhaustiveness and reasons
# ---------------------------------------------------------------------------


def test_parse_fills_every_known_seam_when_the_stored_list_omits_one() -> None:
    """Omission is the only unacceptable outcome — a missing seam is explicit."""
    raw = [
        {
            "seam": SEAM_INGESTION_EDGE,
            "state": SeamCoverageState.ENFORCED.value,
            "provenance": SeamCoverageProvenance.ATTESTED.value,
            "reason": "evaluated at run creation",
        }
    ]

    parsed = parse_seam_coverage(raw)

    assert parsed is not None
    assert [statement.seam for statement in parsed] == list(SEAM_ORDER)


def test_every_not_applicable_statement_carries_its_reason() -> None:
    """A seam with no control states WHY — never a bare omission or empty reason."""
    parsed = parse_seam_coverage([])

    assert parsed is not None
    not_applicable = [statement for statement in parsed if statement.state is SeamCoverageState.NOT_APPLICABLE]
    assert not_applicable
    assert all(statement.reason for statement in not_applicable)


def test_unknown_seam_is_never_round_tripped() -> None:
    """A foreign seam name is dropped, not surfaced as an arbitrary string."""
    parsed = parse_seam_coverage(
        [
            {
                "seam": "made_up_seam",
                "state": SeamCoverageState.ENFORCED.value,
                "provenance": SeamCoverageProvenance.ATTESTED.value,
                "reason": "should not survive",
            }
        ]
    )

    assert parsed is not None
    assert "made_up_seam" not in {statement.seam for statement in parsed}


def test_malformed_persisted_value_degrades_to_explicit_statements() -> None:
    """A non-list JSON value degrades safely — never a crash, never a claim."""
    parsed = parse_seam_coverage({"unexpected": "shape"})

    assert parsed is not None
    assert [statement.seam for statement in parsed] == list(SEAM_ORDER)
    assert all(statement.state is not SeamCoverageState.ENFORCED for statement in parsed)
    assert all(statement.reason for statement in parsed)


def test_legacy_null_exposes_no_coverage() -> None:
    """A row written before the column shipped makes no claim at all."""
    assert parse_seam_coverage(None) is None


def test_non_mapping_persisted_entry_is_dropped_safely() -> None:
    """A list entry that is not an object degrades to "missing", never a crash."""
    parsed = parse_seam_coverage([42, "not-a-statement", None])

    assert parsed is not None
    assert [statement.seam for statement in parsed] == list(SEAM_ORDER)
    assert all(statement.state is not SeamCoverageState.ENFORCED for statement in parsed)


# ---------------------------------------------------------------------------
# Derivation at run creation
# ---------------------------------------------------------------------------


def test_no_guardrails_bound_yields_three_not_applicable_seams() -> None:
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary=None,
        output_guardrail_count=0,
    )

    assert [statement.seam for statement in statements] == list(SEAM_ORDER)
    assert all(statement.state is SeamCoverageState.NOT_APPLICABLE for statement in statements)
    assert all(statement.reason for statement in statements)


def test_all_zero_ingestion_summary_is_not_applicable() -> None:
    """A summary with no bound/evaluated/errored guardrails makes no claim."""
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary={"bound": 0, "evaluated": 0, "errored": 0},
        output_guardrail_count=0,
    )
    ingestion = _by_seam(statements)[SEAM_INGESTION_EDGE]

    assert ingestion.state is SeamCoverageState.NOT_APPLICABLE
    assert ingestion.provenance is SeamCoverageProvenance.DERIVED


@pytest.mark.parametrize(
    "summary",
    [
        {"bound": True, "evaluated": 0, "errored": 0},
        {"bound": 2, "evaluated": "2", "errored": 0},
    ],
)
def test_non_integer_counts_are_treated_as_zero(summary: dict[str, object]) -> None:
    """A malformed count (bool/non-int) is never interpreted as enforcement."""
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary=summary,
        output_guardrail_count=0,
    )
    ingestion = _by_seam(statements)[SEAM_INGESTION_EDGE]

    assert ingestion.state is not SeamCoverageState.ENFORCED


def test_ingestion_guardrail_that_ran_is_enforced_and_attested() -> None:
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary=_INGESTION_SUMMARY,
        output_guardrail_count=0,
    )
    ingestion = _by_seam(statements)[SEAM_INGESTION_EDGE]

    assert ingestion.state is SeamCoverageState.ENFORCED
    assert ingestion.provenance is SeamCoverageProvenance.ATTESTED


def test_errored_ingestion_guardrail_is_degraded_not_enforced() -> None:
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary={"bound": 1, "evaluated": 0, "errored": 1},
        output_guardrail_count=0,
    )
    ingestion = _by_seam(statements)[SEAM_INGESTION_EDGE]

    assert ingestion.state is SeamCoverageState.DEGRADED
    assert ingestion.state is not SeamCoverageState.ENFORCED


def test_blocked_run_never_reaches_the_runtime_so_every_seam_is_not_applicable() -> None:
    """A pre-dispatch block (ingestion guardrail or policy-gate pin) covers nothing.

    This matches the policy-gate block path: both surface as
    ``interception.blocked`` and the run never reaches the runtime.
    """
    statements = derive_seam_coverage(
        blocked_before_runtime=True,
        ingestion_summary=_INGESTION_SUMMARY,
        output_guardrail_count=2,
    )

    assert all(statement.state is SeamCoverageState.NOT_APPLICABLE for statement in statements)


def test_bound_output_guardrail_marks_the_node_output_edge_enforced() -> None:
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary=None,
        output_guardrail_count=1,
    )
    node_output = _by_seam(statements)[SEAM_NODE_OUTPUT_EDGE]

    assert node_output.state is SeamCoverageState.ENFORCED
    assert node_output.provenance is SeamCoverageProvenance.ATTESTED


@pytest.mark.parametrize(
    "reason",
    [
        "org guardrails kill-switch is on; guardrails are observe-only",
        "replay run; guardrails are detection-only",
    ],
)
def test_suppressed_enforcement_is_degraded_not_enforced(reason: str) -> None:
    """Evaluating without acting (observe mode / replay) is never ``enforced``."""
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary=_INGESTION_SUMMARY,
        output_guardrail_count=1,
        enforcement_suppressed_reason=reason,
    )
    by_seam = _by_seam(statements)

    assert by_seam[SEAM_INGESTION_EDGE].state is SeamCoverageState.DEGRADED
    assert by_seam[SEAM_NODE_OUTPUT_EDGE].state is SeamCoverageState.DEGRADED


def test_derived_list_round_trips_through_the_read_path() -> None:
    """A list written at creation parses back unchanged (persistence contract)."""
    statements = derive_seam_coverage(
        blocked_before_runtime=False,
        ingestion_summary=_INGESTION_SUMMARY,
        output_guardrail_count=1,
    )

    parsed = parse_seam_coverage([statement.model_dump(mode="json") for statement in statements])

    assert parsed == statements
