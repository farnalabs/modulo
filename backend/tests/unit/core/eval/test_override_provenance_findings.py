"""Regression pins for the override-provenance rule on test/verification results.

FAR-2491 — "Manual and attested test results" slice of the provenance override
rule (adopted 2026-11-09): *when a person overrides a result the system computed
from evidence, the override is recorded as a distinct, ADDITIVE fact — never by
writing over the computed value — and it must be authored and reasoned.*

This module pins the property at the boundary that actually exists in Modulo.
There is **no** path where a human records a test/verification PASS instead of a
computed result: every ``EvalResult`` row is produced by the eval engine, the
guardrail engine, a suite-run executor or the demo seed, each of which computes
``outcome``/``passed`` itself. The nearest real human-override surface is a
finding RESOLUTION (``POST /api/v1/evals/findings/{id}/resolve``), which records
a person's judgement *next to* the computed verdict (``outcome``/``passed``/
``score`` stay untouched) and feeds the acceptance-rate metric, never the
pass rate.

Two contracts are pinned here:

1. An override resolution must carry a reason — a resolution with no reason is
   the rule's prohibited "override with no reason" shape — and it is attributed
   to the authenticated caller (never defaulted).
2. The override is additive: resolving a finding must never write the computed
   verdict fields, so the computed result and the human judgement stay
   independently readable (rule property 2, "separable").
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.eval.finding_resolver import (
    FindingResolutionError,
    resolve_finding,
)

_ACCOUNT_ID = "550e8400-e29b-41d4-a716-446655440000"
_ORG_ID = "org-2491"


class _VerdictGuard:
    """A finding stand-in that REFUSES any write to the computed verdict.

    Resolving a finding is a human OVERRIDE of a computed result, so the write
    path must touch only the resolution columns. Any assignment to ``outcome``,
    ``passed`` or ``score`` raises here — that is the failing behaviour the
    provenance rule forbids ("overwriting the computed row in place"), so a
    future change that writes the verdict fails this module loudly.
    """

    _VERDICT_FIELDS = ("outcome", "passed", "score")

    def __init__(self) -> None:
        object.__setattr__(self, "eval_id", "eval-1")
        object.__setattr__(self, "outcome", "fail")
        object.__setattr__(self, "passed", False)
        object.__setattr__(self, "score", 0.0)
        object.__setattr__(self, "resolution", "pending")
        object.__setattr__(self, "resolution_reason", None)
        object.__setattr__(self, "resolved_at", None)
        object.__setattr__(self, "resolved_by", None)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._VERDICT_FIELDS:
            raise AssertionError(f"an override must never write the computed verdict field {name!r}")
        object.__setattr__(self, name, value)


def _session_returning(finding: _VerdictGuard) -> AsyncMock:
    """A session whose single execute() resolves to *finding*."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = finding
    session.execute.return_value = result
    return session


@pytest.mark.parametrize("resolution", ["accepted", "dismissed", "superseded"])
@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_override_resolution_requires_a_reason(resolution: str, reason: str | None) -> None:
    """A resolution that overrides the computed finding must state WHY.

    The rule names "an override with no reason" as a prohibited shape, so the
    override rolls back the rule's own audit guarantee. The check is ordered
    before the finding lookup, so it holds for both storage arms (eval result
    and trigger-event finding) and an unknown id cannot bypass it.
    """
    session = _session_returning(_VerdictGuard())

    with pytest.raises(FindingResolutionError) as exc_info:
        await resolve_finding(session, "finding-1", resolution, reason, _ACCOUNT_ID, _ORG_ID)

    assert "reason is required" in str(exc_info.value)
    session.flush.assert_not_awaited()


async def test_pending_revert_needs_no_reason() -> None:
    """The ``pending`` revert CLEARS an override — it is not one, so no reason.

    Requiring a reason here would make an override impossible to undo without
    inventing one, which would be worse than the nothing it would buy.
    """
    finding = _VerdictGuard()
    session = _session_returning(finding)

    resolved = await resolve_finding(session, "finding-1", "pending", None, _ACCOUNT_ID, _ORG_ID)

    assert resolved.resolution == "pending"
    session.flush.assert_awaited_once()


async def test_override_is_additive_and_never_writes_the_computed_verdict() -> None:
    """The human judgement is recorded BESIDE the verdict, never over it.

    ``_VerdictGuard`` raises on any write to ``outcome``/``passed``/``score``, so
    this fails if resolving a finding starts overwriting what the system
    computed — the exact "override becomes the value" collapse the rule exists
    to prevent.
    """
    finding = _VerdictGuard()
    session = _session_returning(finding)

    await resolve_finding(session, "finding-1", "dismissed", "False positive", _ACCOUNT_ID, _ORG_ID)

    # The computed verdict is intact and still readable on its own.
    assert finding.outcome == "fail"
    assert finding.passed is False
    assert finding.score == 0.0
    # The override is a separate, additive fact.
    assert finding.resolution == "dismissed"
    assert finding.resolution_reason == "False positive"


async def test_override_is_attributed_to_the_authenticated_author() -> None:
    """An override records WHO set it — never an unattributed or default actor.

    The rule forbids "an override with its actor defaulted". The core stamps the
    supplied caller; the route always supplies the authenticated principal's
    account id, so a resolution can never land with a NULL author.
    """
    finding = _VerdictGuard()
    session = _session_returning(finding)

    await resolve_finding(session, "finding-1", "accepted", "Confirmed true positive", _ACCOUNT_ID, _ORG_ID)

    assert finding.resolved_by == uuid.UUID(_ACCOUNT_ID)
    assert finding.resolved_at is not None
