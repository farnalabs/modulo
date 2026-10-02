"""Policy Gate resolution module (FAR-1060, chunk 1).

Standalone, unwired, pure synchronous Python over plain data.

Fixed source-priority order for ``resolve_policy_gate``:
    (c) node_id mismatch → (a) no EvalResult →
    (d) persistence failure (out of scope for chunk 1 / FAR-971).

Fail-closed / fail-open semantics:
    - ``block`` fails CLOSED on undefined — the run halts.
    - ``warn`` fails OPEN — log and continue.
    The ``Outcome`` carries the resolved action; the CALLER enforces
    halt-vs-log — this module does not raise for warn/block.

``eval_result_id`` is ``None`` for source (a) (no EvalResult exists) and
for soft-deleted eval (criterion 2 evaluation), and populated for sources
(c) when an EvalResult happens to exist (Eval scoring is
independent of PolicyGate validity).

Guardrail-type evals are no longer special-cased at evaluation time
(chunk 8 retired the ``GUARDRAIL_EVAL_TYPE`` re-check branch); they
resolve through the same code path as non-guardrail evals.

Pin-set integrity fingerprint (FAR-967 chunk 10, §3.3):
    ``fingerprint_policy_gate_pins()`` computes a deterministic SHA-256
    over the canonical JSON of a policy-gate pin set.  Unlike the
    predecessor ``fingerprint_guardrail_pins()``, an empty list ``[]``
    produces a deterministic digest (empty ≠ absent).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modulo.db.models.policy_gate_decision import PolicyGateDecision


class PolicyGateBindingViolationError(ValueError):
    """Raised by validate_binding when the PolicyGate→Eval binding violates one or more exclusions."""

    def __init__(self, violations: list[dict[str, str]]) -> None:
        self.violations = violations
        names = ", ".join(v["exclusion"] for v in violations)
        super().__init__(f"PolicyGate binding violations: {names}")


def _coerce_uuid(value: Any, field_name: str) -> uuid.UUID:
    """Validate that *value* is a UUID instance, not a string (C10)."""
    if isinstance(value, str):
        raise TypeError(
            f"{field_name} must be a uuid.UUID, not str — callers must normalise before constructing the snapshot"
        )
    if not isinstance(value, uuid.UUID):
        raise TypeError(f"{field_name} must be a uuid.UUID, got {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class PolicyGateView:
    id: uuid.UUID
    organisation_id: uuid.UUID
    version: int
    node_id: uuid.UUID
    action: str

    def __post_init__(self) -> None:
        # frozen=True means we use object.__setattr__ via the _coerce helper
        # — but post_init runs after __init__, so direct assignment works for
        # the validation pass.  For frozen dataclasses we need to re-assign
        # via object.__setattr__ if we want to normalise.  Here we only check.
        _coerce_uuid(self.node_id, "node_id")


@dataclass(frozen=True, slots=True)
class EvalView:
    id: uuid.UUID
    organisation_id: uuid.UUID
    node_id: uuid.UUID | None
    eval_type: str
    deleted_at: datetime | None

    def __post_init__(self) -> None:
        if self.node_id is not None:
            _coerce_uuid(self.node_id, "node_id")


@dataclass(frozen=True, slots=True)
class EvalResultView:
    id: uuid.UUID
    passed: bool


@dataclass(frozen=True, slots=True)
class EvalPolicySnapshot:
    policy_gate: PolicyGateView
    eval: EvalView
    eval_result: EvalResultView | None


@dataclass(frozen=True, slots=True)
class Outcome:
    result: bool | None  # None == undefined
    action: str  # "continue" | "warn" | "block" (resolved)
    eval_result_id: uuid.UUID | None
    error: str | None  # set when result is undefined; identifies the source


def resolve_policy_gate(snapshot: EvalPolicySnapshot) -> Outcome:
    """Resolve a PolicyGate evaluation outcome from an ``EvalPolicySnapshot``.

    Called ONLY for a live (non-soft-deleted) PolicyGate — the caller checks
    ``PolicyGate.deleted_at IS NULL`` before constructing a snapshot.

    Source priority (deterministic, fixed):
        (c) node_id mismatch         → undefined, error="node_id_mismatch"
        (a) no EvalResult             → undefined, error="no_eval_result"
        (d) persistence failure       → out of scope for chunk 1 (FAR-971)

    ``eval_result_id`` is populated whenever an EvalResult exists in the
    snapshot (sources b, c when an EvalResult is present) and ``None`` only
    when no EvalResult exists at all (source a).
    """
    pg = snapshot.policy_gate
    ev = snapshot.eval
    er = snapshot.eval_result

    # Source (c): node_id-match re-check
    if ev.node_id is None or ev.node_id != pg.node_id:
        return Outcome(
            result=None,
            action=pg.action,
            eval_result_id=er.id if er is not None else None,
            error="node_id_mismatch",
        )

    # Source (a): no EvalResult — also covers soft-deleted eval (criterion 2)
    if ev.deleted_at is not None or er is None:
        return Outcome(
            result=None,
            action=pg.action,
            eval_result_id=None,
            error="no_eval_result",
        )

    # Defined result
    if er.passed:
        return Outcome(
            result=True,
            action="continue",
            eval_result_id=er.id,
            error=None,
        )
    return Outcome(
        result=False,
        action=pg.action,
        eval_result_id=er.id,
        error=None,
    )


def build_decision_row(
    snapshot: EvalPolicySnapshot,
    outcome: Outcome,
    run_id: uuid.UUID,
) -> PolicyGateDecision:
    """Construct a ``PolicyGateDecision`` ORM row from an evaluation outcome.

    Pure construction — never touches a database or a repository.
    Persistence, and the fail-open wrapper around it, are the write-path
    concern (``eval_persist_order.run_evals_persist_before_decide``).

    The row carries the PolicyGate's OWN denormalized ``node_id`` (from the
    snapshot), not a fresh read.  This preserves what the gate BELIEVED its
    node_id was at evaluation time, which is the correct audit-trail value.
    """
    from modulo.db.models.policy_gate_decision import PolicyGateDecision

    pg_org = snapshot.policy_gate.organisation_id
    ev_org = snapshot.eval.organisation_id
    assert pg_org == ev_org, (
        f"cross-tenancy sanity check failed: policy_gate.organisation_id={pg_org} != eval.organisation_id={ev_org}"
    )

    return PolicyGateDecision(
        organisation_id=pg_org,
        policy_gate_id=snapshot.policy_gate.id,
        eval_id=snapshot.eval.id,
        resolved_action=outcome.action,
        error_detail=outcome.error,
        node_id=snapshot.policy_gate.node_id,
        eval_result_id=outcome.eval_result_id,
        run_id=run_id,
        policy_gate_version=snapshot.policy_gate.version,
    )


def validate_binding(
    policy_gate_fields: Mapping[str, Any],
    eval_fields: Mapping[str, Any],
) -> None:
    """Validate PolicyGate→Eval binding exclusions at creation time.

    Collects ALL violated exclusions (no short-circuit), then raises
    ``PolicyGateBindingViolationError`` ONCE with the full list.
    Returns ``None`` when the binding is valid.

    Organisation identifier VALUES are never included in the violation
    payload (they may reach a caller).
    """
    violations: list[dict[str, str]] = []

    pg_org = policy_gate_fields["organisation_id"]
    ev_org = eval_fields["organisation_id"]
    pg_id = policy_gate_fields["id"]
    ev_id = eval_fields["id"]

    # 1. Cross-tenancy
    if pg_org != ev_org:
        violations.append({"exclusion": "cross_tenancy", "policy_gate_id": str(pg_id), "eval_id": str(ev_id)})

    # 2. Suite-scoped eval (node_id IS NULL)
    if eval_fields["node_id"] is None:
        violations.append({"exclusion": "suite_scoped_eval", "policy_gate_id": str(pg_id), "eval_id": str(ev_id)})

    # 3. node_id mismatch
    ev_node = eval_fields["node_id"]
    pg_node = policy_gate_fields["node_id"]
    if ev_node is not None and ev_node != pg_node:
        violations.append({"exclusion": "node_id_mismatch", "policy_gate_id": str(pg_id), "eval_id": str(ev_id)})

    if violations:
        raise PolicyGateBindingViolationError(violations)


# ---------------------------------------------------------------------------
# Pin-set integrity fingerprint (FAR-967 chunk 10, §3.3)
# ---------------------------------------------------------------------------


def fingerprint_policy_gate_pins(
    pins: Sequence[Any] | None,
) -> str | None:
    """Deterministic fingerprint of a serialized policy-gate pin set.

    Computed over the canonical JSON of EVERY entry in the pin list (sorted
    so re-serialization order never changes the digest).  A snapshot's
    ``policy_gate_pins_json`` is fingerprinted at snapshot creation; the
    run-start replay seam re-computes the fingerprint of the LOADED pins
    and compares — a mismatch means the pins were tampered with (or
    drifted) since creation and the replay fails closed as a mechanism
    error.

    **Every entry is covered verbatim** — dict or not.  Filtering
    non-dict entries out of the digest (the pre-FAR-967-F5 behaviour)
    meant appending a junk entry to the pin list left the digest
    unchanged, so corruption went undetected and the junk later crashed
    per-entry consumers.  Entries are now digested as-is, so ANY
    appended/removed/replaced element — including a non-dict — flips the
    digest and fails closed at run start.

    **IMPORTANT — different empty-set semantics from the predecessor:**

    - ``None`` (absent) → returns ``None`` (legacy snapshot, no pins)
    - ``[]`` (empty list) → returns a **deterministic digest** (canonical
      ``[]`` JSON).  An empty pin set means "zero gates pinned" — a
      deliberate state, NOT indistinguishable from absent.
    - Non-empty list → returns SHA-256 hex digest over all entries.

    An implementer MUST NOT call ``fingerprint_guardrail_pins()`` for
    Policy Gate pins — the semantics are wrong (the predecessor collapses
    empty and absent into the same ``None`` return).
    """
    if pins is None:
        return None
    # Empty list IS a deliberate state — produce a deterministic digest.
    if not pins:
        canonical = "[]"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if isinstance(pins, (str, bytes)) or not isinstance(pins, Sequence):
        # Corrupt non-list top-level value: fail closed with a digest that
        # can never equal a stored (list-computed) fingerprint, without
        # raising out of the run-start seam.
        canonical = "[<corrupt-policy-gate-pin-set>]"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Cover every entry verbatim (dict, str, int, list — anything JSON-ish),
    # so no element of the pin set is invisible to the integrity check.
    items = sorted(json.dumps(pin, sort_keys=True, separators=(",", ":"), default=str) for pin in pins)
    canonical = "[" + ",".join(items) + "]"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
