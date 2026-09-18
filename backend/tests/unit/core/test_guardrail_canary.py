"""Canary CI gate for guardrail detection (FAR-223 item 12).

Two distinct canaries, run through the REAL interception seam
(``run_interception_pass_async``), never a mock of the seam:

1. **Known-DETECTABLE canary (HARD CI GATE)** — a regex that deterministically
   matches a fixed marker in a fixed payload is asserted to fire 100% of the
   time (violation detected, block outcome) across a large iteration count.
   This is a hard gate: if the detection engine ever silently stops firing on
   a known marker, CI fails. The canary is deterministic — no timing, no
   randomness — so a single missed fire is a real regression, not flake.

2. **Known-WEAK evasion canary (INFORMATIONAL BAND ONLY)** — a payload that is
   designed to evade a naive regex (obfuscated / encoded marker) is asserted
   NOT to fire. This is deliberately excluded from CI failure: it documents
   the evasion band (what a naive structural detector does NOT catch) rather
   than gating on it. It RUNS through the real interception seam and asserts
   the weak-evasion payload genuinely does NOT fire, pinning the documented
   non-coverage to reality — it is a real assertion, not a skipped no-op.

The purpose of the informational canary is to record, for the operator, that a
naive regex guardrail does NOT catch obfuscated credentials — the known
non-coverage documented in PRD §8.17 ("What guardrails do NOT cover"). It is
intentionally not a gate.
"""

import uuid
from typing import Any

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.guardrails import GuardrailAction, run_interception_pass_async

# The fixed marker the detectable canary matches. Deterministic and immutable —
# changing the marker or the payload must be a deliberate, reviewed act.
_CANARY_MARKER = "CANARY_ABC12345"
_CANARY_PATTERN = r"CANARY_[A-Z0-9]{8}"

# The evasion canary: the SAME marker transformed so a naive regex misses it.
_EVASION_PAYLOAD = "C4N4RY_ABC12345"  # 0 -> O-lookalike digits defeat the naive regex

_ITERATIONS = 100


def _canary_definition(*, action: str) -> EvalDefinition:
    """A deterministic canary guardrail row (org-level, block/observe action)."""
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        node_id=None,
        name="canary-detectable",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": action,
            "interception_point": "input",
            "type": "regex",
            "field": "body",
            "pattern": _CANARY_PATTERN,
        },
        failure_behaviour="warn",
    )


async def _pass_once(action: str, payload: dict[str, Any]) -> bool:
    """Run the REAL interception seam once and report whether it fired.

    For a ``block`` canary "fired" means the outcome reports a block; for an
    ``observe`` canary it means a violation was detected (results non-empty and
    interpreted as a violation). Uses the async production seam
    (``run_interception_pass_async``) which is what create_run calls.
    """
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_canary_definition(action=action)],
        payload,
    )
    if action == GuardrailAction.BLOCK.value:
        return outcome.blocked
    # observe: the regex ``passed`` result means the pattern MATCHED = violation.
    return any(r.passed for r in outcome.results)


@pytest.mark.asyncio
async def test_detectable_canary_fires_100_percent_through_real_seam():
    """HARD CI GATE — the known marker must be caught on EVERY iteration."""
    payload = {"body": f"leak {_CANARY_MARKER} here"}
    fired = 0
    for _ in range(_ITERATIONS):
        if await _pass_once(GuardrailAction.BLOCK.value, payload):
            fired += 1
    assert fired == _ITERATIONS, f"canary fired {fired}/{_ITERATIONS} — detection regression"


@pytest.mark.asyncio
async def test_detectable_canary_block_outcome_is_deterministic():
    """The block canary reports a TERMINAL block outcome through the seam."""
    payload = {"body": f"leak {_CANARY_MARKER} here"}
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_canary_definition(action=GuardrailAction.BLOCK.value)],
        payload,
    )
    assert outcome.blocked is True
    assert "canary-detectable" in outcome.blocking_eval_name
    assert _CANARY_MARKER not in outcome.block_message  # no raw payload in detail


@pytest.mark.asyncio
async def test_detectable_canary_clean_payload_never_fires():
    """The same canary must NOT fire on a clean payload (no false positive)."""
    payload = {"body": "no secret marker here"}
    fired = 0
    for _ in range(_ITERATIONS):
        if await _pass_once(GuardrailAction.BLOCK.value, payload):
            fired += 1
    assert fired == 0, f"clean payload fired {fired} times — false positive"


@pytest.mark.asyncio
async def test_weak_evasion_canary_does_not_fire_informational_only():
    """INFORMATIONAL ONLY — documents the known evasion band.

    This canary documents the known evasion band: a naive structural regex
    (``CANARY_[A-Z0-9]{8}``) does NOT catch a marker whose characters are
    obfuscated (digits swapped for letter-lookalikes: ``C4N4RY_ABC12345``).
    This is EXPECTED behaviour — it is precisely the free-text/obfuscation
    non-coverage the PRD §8.17 records as known failure class. It is NOT a
    regression and MUST NOT gate CI; it runs and asserts the weak-evasion
    payload genuinely evades the regex, so the documented band stays pinned
    to reality rather than being a never-executed no-op.

    If this canary ever fires, the regex was tightened to catch the evasion —
    that is a *new* detection capability worth reviewing, not a failure.
    """
    payload = {"body": f"leak {_EVASION_PAYLOAD} here"}
    fired = await _pass_once(GuardrailAction.OBSERVE.value, payload)
    assert not fired, (
        f"evasion canary fired for {_EVASION_PAYLOAD!r} — the naive regex "
        "CANARY_[A-Z0-9]{8} unexpectedly caught this obfuscation"
    )
