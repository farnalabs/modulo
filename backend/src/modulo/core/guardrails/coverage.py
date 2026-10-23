"""Seam-scoped guardrail coverage for a run (FAR-2054).

Alpha states coverage **per seam, exhaustively**. A single enum cannot say
"the ingestion edge is enforced AND the runtime loop is uncovered" without
collapsing provenance across seams, so the run record carries one statement
per known seam::

    {seam, state, provenance, reason}

The three known seams are the only places a guardrail can act on a run:

* ``ingestion_edge``  — the run-creation pass over the input payload
  (``crud.run.create_run`` -> ``_intercept_guardrails``);
* ``node_output_edge`` — the executor's chain-end output interception
  (``core.guardrails.output_intercept``);
* ``runtime_loop`` — the third-party agent runtime's internal tool loop.

**The binding rule.** ``enforced`` may only be claimed where MODULO ITSELF
acted and recorded the fact natively. A runtime plugin event is at best
advisory observation of a third party's process, so it can never produce
``enforced`` — this is the product principle "dispatch and observe, never
reach inside" made structural. ``enforced`` therefore requires the ADR-042
provenance class ``attested``; the model validator below rejects any other
combination. ``runtime_loop`` is always ``not_applicable``: Alpha does not
consume a tool-event stream from the runtime it can verify, so there is
nothing Modulo can honestly claim to have enforced mid-loop.

**Exhaustiveness.** A seam with no control is ``not_applicable`` *with a
reason* — never absent, never silently omitted. The read-side parser
(:func:`parse_seam_coverage`) always returns all known seams, in
:data:`SEAM_ORDER`, and never round-trips an arbitrary string as a state or a
seam name.

This module deliberately contains NO runtime-plugin machinery: no callback
server, no plugin source generation, no runtime-capability detection. The
seam list is derived from what Modulo itself did (or did not do) at run
creation.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from modulo.core.provenance import (
    PROVENANCE_ATTESTED,
    PROVENANCE_DERIVED,
    PROVENANCE_OBSERVED,
    PROVENANCE_REPORTED,
)

#: The known enforcement seams, in the order the run-detail surface renders.
SeamName = Literal["ingestion_edge", "node_output_edge", "runtime_loop"]

SEAM_INGESTION_EDGE: Final[SeamName] = "ingestion_edge"
SEAM_NODE_OUTPUT_EDGE: Final[SeamName] = "node_output_edge"
SEAM_RUNTIME_LOOP: Final[SeamName] = "runtime_loop"

SEAM_ORDER: Final[tuple[SeamName, ...]] = (
    SEAM_INGESTION_EDGE,
    SEAM_NODE_OUTPUT_EDGE,
    SEAM_RUNTIME_LOOP,
)

#: The one honest statement the runtime loop can carry in Alpha. The runtime
#: (opencode and any future runtime) does not expose a tool-event contract
#: Modulo can verify, so no Modulo-controlled enforcement can be attested
#: mid-loop. A runtime plugin is observation of a third party's process, not
#: evidence of a Modulo-owned control.
RUNTIME_LOOP_UNAVAILABLE_REASON: Final[str] = "runtime does not expose a tool-event contract Modulo can verify"


class SeamCoverageState(StrEnum):
    """Whether a Modulo-owned control covered the seam for this run."""

    ENFORCED = "enforced"
    DEGRADED = "degraded"
    NOT_APPLICABLE = "not_applicable"


class SeamCoverageProvenance(StrEnum):
    """The ADR-042 provenance classes, bound to the canonical constants.

    Members are assigned FROM ``modulo.core.provenance`` so a rename there
    propagates instead of drifting; a test asserts this enum covers the whole
    canonical set (a new ADR-042 class must be added here too).
    """

    OBSERVED = PROVENANCE_OBSERVED
    DERIVED = PROVENANCE_DERIVED
    REPORTED = PROVENANCE_REPORTED
    ATTESTED = PROVENANCE_ATTESTED


class SeamCoverageStatement(BaseModel):
    """One seam-scoped coverage statement on a run.

    ``extra="forbid"``: a stored value carrying fields this contract does not
    know is malformed for the read path, which degrades it (never a 500, never
    an arbitrary string round-tripped to the UI).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seam: SeamName
    state: SeamCoverageState
    provenance: SeamCoverageProvenance
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _enforced_requires_attested(self) -> SeamCoverageStatement:
        """``enforced`` is reachable ONLY from a Modulo-attested fact.

        A ``reported`` (external surface / runtime) or ``derived``/``observed``
        statement claiming ``enforced`` is the evidence laundering ADR-042 and
        ADR-067 exist to stop. Fail the statement rather than publish the claim.
        """
        if self.state is SeamCoverageState.ENFORCED and self.provenance is not SeamCoverageProvenance.ATTESTED:
            raise ValueError("enforced seam coverage requires attested provenance")
        return self


# ---------------------------------------------------------------------------
# Derivation at run creation (what Modulo actually did)
# ---------------------------------------------------------------------------

_REASON_NO_INGESTION_GUARDRAIL = "no ingestion-edge guardrail is bound to this run"
_REASON_NO_OUTPUT_GUARDRAIL = "no node-output guardrail is bound to this run"
_REASON_BLOCKED_INGESTION = "run blocked before runtime dispatch; no input reached the runtime"
_REASON_BLOCKED_OUTPUT = "run blocked before runtime dispatch; no node output was produced"
_REASON_INVALID_SEAM = "persisted seam coverage is invalid for this seam"
_REASON_MALFORMED = "persisted seam coverage is malformed"
_REASON_MISSING = "seam not recorded in persisted coverage"


def _statement(
    seam: SeamName,
    state: SeamCoverageState,
    provenance: SeamCoverageProvenance,
    reason: str,
) -> SeamCoverageStatement:
    return SeamCoverageStatement(seam=seam, state=state, provenance=provenance, reason=reason)


def _runtime_loop_statement() -> SeamCoverageStatement:
    return _statement(
        SEAM_RUNTIME_LOOP,
        SeamCoverageState.NOT_APPLICABLE,
        SeamCoverageProvenance.DERIVED,
        RUNTIME_LOOP_UNAVAILABLE_REASON,
    )


def _count(summary: Mapping[str, Any], key: str) -> int:
    """Read a non-negative count defensively (a malformed summary never raises)."""
    value = summary.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _ingestion_statement(
    summary: Mapping[str, Any] | None,
    suppression_reason: str | None,
) -> SeamCoverageStatement:
    """Derive the ingestion-edge statement from the run-creation pass.

    ``enforced`` requires the pass to have actually evaluated at least one
    bound guardrail AND enforcement not to have been suppressed (kill-switch
    observe mode, or a detection-only replay). An errored pass is ``degraded``
    — Modulo attempted and recorded the failure natively (``attested``).
    """
    if not summary:
        return _statement(
            SEAM_INGESTION_EDGE,
            SeamCoverageState.NOT_APPLICABLE,
            SeamCoverageProvenance.DERIVED,
            _REASON_NO_INGESTION_GUARDRAIL,
        )
    bound = _count(summary, "bound")
    evaluated = _count(summary, "evaluated")
    errored = _count(summary, "errored")
    if errored:
        return _statement(
            SEAM_INGESTION_EDGE,
            SeamCoverageState.DEGRADED,
            SeamCoverageProvenance.ATTESTED,
            f"{errored} of {bound} ingestion-edge guardrail(s) failed to evaluate",
        )
    if evaluated and suppression_reason is not None:
        return _statement(
            SEAM_INGESTION_EDGE,
            SeamCoverageState.DEGRADED,
            SeamCoverageProvenance.ATTESTED,
            suppression_reason,
        )
    if evaluated:
        return _statement(
            SEAM_INGESTION_EDGE,
            SeamCoverageState.ENFORCED,
            SeamCoverageProvenance.ATTESTED,
            f"{evaluated} ingestion-edge guardrail(s) evaluated at run creation",
        )
    if bound:
        return _statement(
            SEAM_INGESTION_EDGE,
            SeamCoverageState.DEGRADED,
            SeamCoverageProvenance.DERIVED,
            f"{bound} ingestion-edge guardrail(s) bound but none evaluated",
        )
    return _statement(
        SEAM_INGESTION_EDGE,
        SeamCoverageState.NOT_APPLICABLE,
        SeamCoverageProvenance.DERIVED,
        _REASON_NO_INGESTION_GUARDRAIL,
    )


def _node_output_statement(
    output_guardrail_count: int,
    suppression_reason: str | None,
) -> SeamCoverageStatement:
    """Derive the node-output statement from the guardrails bound for the run.

    The executor enforces output-point guardrails at the node-output seam; a
    bound output guardrail is a Modulo action (``attested``), so the seam reads
    ``enforced`` unless enforcement is suppressed (kill-switch observe mode /
    replay detection-only).
    """
    if output_guardrail_count <= 0:
        return _statement(
            SEAM_NODE_OUTPUT_EDGE,
            SeamCoverageState.NOT_APPLICABLE,
            SeamCoverageProvenance.DERIVED,
            _REASON_NO_OUTPUT_GUARDRAIL,
        )
    if suppression_reason is not None:
        return _statement(
            SEAM_NODE_OUTPUT_EDGE,
            SeamCoverageState.DEGRADED,
            SeamCoverageProvenance.ATTESTED,
            suppression_reason,
        )
    return _statement(
        SEAM_NODE_OUTPUT_EDGE,
        SeamCoverageState.ENFORCED,
        SeamCoverageProvenance.ATTESTED,
        f"{output_guardrail_count} node-output guardrail(s) bound at the node-output seam",
    )


def derive_seam_coverage(
    *,
    blocked_before_runtime: bool,
    ingestion_summary: Mapping[str, Any] | None,
    output_guardrail_count: int,
    enforcement_suppressed_reason: str | None = None,
) -> list[SeamCoverageStatement]:
    """Build the exhaustive per-seam coverage list for a newly created run.

    *blocked_before_runtime* — the run was blocked at creation (ingestion
    guardrail, policy-gate pin fingerprint, or a cap/conformance block), so it
    never reached the runtime: every seam is ``not_applicable``. This matches
    the policy-gate block path in ``create_run`` (both surface as
    ``interception.blocked``).

    *ingestion_summary* — the persisted ``guardrail_summary`` dict (``None``
    when no ingestion guardrail ran).

    *output_guardrail_count* — how many output-point guardrails are bound to
    the run's frozen guardrail set.

    *enforcement_suppressed_reason* — non-``None`` when Modulo evaluated but
    deliberately did not act (kill-switch observe mode, replay detection-only).
    A suppressed pass is ``degraded``, never ``enforced``: Modulo did not act.
    """
    if blocked_before_runtime:
        return [
            _statement(
                SEAM_INGESTION_EDGE,
                SeamCoverageState.NOT_APPLICABLE,
                SeamCoverageProvenance.DERIVED,
                _REASON_BLOCKED_INGESTION,
            ),
            _statement(
                SEAM_NODE_OUTPUT_EDGE,
                SeamCoverageState.NOT_APPLICABLE,
                SeamCoverageProvenance.DERIVED,
                _REASON_BLOCKED_OUTPUT,
            ),
            _runtime_loop_statement(),
        ]
    return [
        _ingestion_statement(ingestion_summary, enforcement_suppressed_reason),
        _node_output_statement(output_guardrail_count, enforcement_suppressed_reason),
        _runtime_loop_statement(),
    ]


# ---------------------------------------------------------------------------
# Read path (persisted JSON -> typed, guarded, exhaustive list)
# ---------------------------------------------------------------------------


def _known_seam(entry: Any) -> SeamName | None:
    if not isinstance(entry, Mapping):
        return None
    seam = entry.get("seam")
    if isinstance(seam, str) and seam in SEAM_ORDER:
        return seam
    return None


def _invalid_statement(seam: SeamName) -> SeamCoverageStatement:
    if seam == SEAM_RUNTIME_LOOP:
        return _runtime_loop_statement()
    return _statement(
        seam,
        SeamCoverageState.DEGRADED,
        SeamCoverageProvenance.DERIVED,
        _REASON_INVALID_SEAM,
    )


def _missing_statement(seam: SeamName) -> SeamCoverageStatement:
    if seam == SEAM_RUNTIME_LOOP:
        return _runtime_loop_statement()
    return _statement(
        seam,
        SeamCoverageState.NOT_APPLICABLE,
        SeamCoverageProvenance.DERIVED,
        _REASON_MISSING,
    )


def _guard(statement: SeamCoverageStatement) -> SeamCoverageStatement:
    """Clamp a parsed statement to what Modulo may honestly publish.

    ``runtime_loop`` is ALWAYS ``not_applicable`` regardless of what a stored
    (or runtime-sourced) value claims: no runtime event can attest a
    Modulo-owned in-loop control, so even an ``attested`` claim for that seam is
    over-claiming and is replaced with the honest statement. This is the point
    at which a tampered/persisted ``enforced`` for the runtime loop cannot reach
    the API or the run-detail page.
    """
    if statement.seam == SEAM_RUNTIME_LOOP:
        return _runtime_loop_statement()
    return statement


def _coerce_statement(entry: Any) -> SeamCoverageStatement | None:
    """Validate one persisted entry, degrading a known-seam failure explicitly.

    Unknown seams return ``None`` and are dropped (never round-tripped as an
    arbitrary string). A known seam whose entry fails validation becomes an
    explicit ``degraded`` statement rather than vanishing.
    """
    try:
        return SeamCoverageStatement.model_validate(entry)
    except (ValidationError, TypeError, ValueError):
        seam = _known_seam(entry)
        return None if seam is None else _invalid_statement(seam)


def _malformed_coverage() -> list[SeamCoverageStatement]:
    statements: list[SeamCoverageStatement] = []
    for seam in SEAM_ORDER:
        if seam == SEAM_RUNTIME_LOOP:
            statements.append(_runtime_loop_statement())
            continue
        statements.append(
            _statement(
                seam,
                SeamCoverageState.DEGRADED,
                SeamCoverageProvenance.DERIVED,
                _REASON_MALFORMED,
            )
        )
    return statements


def parse_seam_coverage(raw: Any) -> list[SeamCoverageStatement] | None:
    """Parse persisted ``seam_coverage_json`` for the run-detail API.

    ``None`` in -> ``None`` out: a legacy row (column never written) exposes no
    coverage rather than a fabricated claim. Every non-``None`` value produces
    the exhaustive list in :data:`SEAM_ORDER`; malformed data degrades to an
    explicit ``degraded``/``not_applicable`` statement per seam and never
    surfaces arbitrary strings.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        return _malformed_coverage()
    parsed: dict[str, SeamCoverageStatement] = {}
    for entry in raw:
        statement = _coerce_statement(entry)
        if statement is not None:
            parsed[str(statement.seam)] = _guard(statement)
    return [parsed.get(seam) or _missing_statement(seam) for seam in SEAM_ORDER]


__all__ = [
    "RUNTIME_LOOP_UNAVAILABLE_REASON",
    "SEAM_INGESTION_EDGE",
    "SEAM_NODE_OUTPUT_EDGE",
    "SEAM_ORDER",
    "SEAM_RUNTIME_LOOP",
    "SeamCoverageProvenance",
    "SeamCoverageState",
    "SeamCoverageStatement",
    "SeamName",
    "derive_seam_coverage",
    "parse_seam_coverage",
]
