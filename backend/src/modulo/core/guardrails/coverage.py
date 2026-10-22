"""Typed runtime-source coverage contract for loop interception."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LoopCoverageState(StrEnum):
    ENFORCED = "enforced"
    DEGRADED = "degraded"
    NOT_APPLICABLE = "not_applicable"


class LoopCoverageProvenance(StrEnum):
    OBSERVED = "observed"
    DERIVED = "derived"


class LoopCoverage(BaseModel):
    """Persisted and API-visible loop interception coverage."""

    model_config = ConfigDict(extra="forbid")

    state: LoopCoverageState
    reason: str = Field(min_length=1)
    provenance: LoopCoverageProvenance


class RuntimeToolEvent(BaseModel):
    """The authenticated shape of a sandbox bridge tool event."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    args: dict[str, Any]
    direction: Literal["before", "after"]
    result_summary: str = ""


def parse_loop_coverage(raw: Any) -> LoopCoverage | None:
    """Validate stored coverage, returning ``None`` for a legacy NULL value."""
    if raw is None:
        return None
    try:
        return LoopCoverage.model_validate(raw)
    except ValidationError:
        return LoopCoverage(
            state=LoopCoverageState.DEGRADED,
            reason="persisted_loop_coverage_invalid",
            provenance=LoopCoverageProvenance.DERIVED,
        )


def initial_loop_coverage(*, configured: bool) -> LoopCoverage:
    """Return the honest pre-event state for a configured runtime source."""
    if configured:
        return LoopCoverage(
            state=LoopCoverageState.DEGRADED,
            reason="runtime_tool_event_source_unobserved",
            provenance=LoopCoverageProvenance.DERIVED,
        )
    return LoopCoverage(
        state=LoopCoverageState.NOT_APPLICABLE,
        reason="no_runtime_loop_guardrail_bound",
        provenance=LoopCoverageProvenance.DERIVED,
    )


__all__ = [
    "LoopCoverage",
    "LoopCoverageProvenance",
    "LoopCoverageState",
    "RuntimeToolEvent",
    "initial_loop_coverage",
    "parse_loop_coverage",
]
