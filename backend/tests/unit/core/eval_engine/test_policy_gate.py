"""Pure-unit tests for policy_gate module (FAR-1060, chunk 1).

Covers criteria 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 18.
No database, no Docker -- plain dataclass construction and function calls.
"""

import uuid
from datetime import UTC, datetime

import pytest

from modulo.core.eval_engine.policy_gate import (
    EvalPolicySnapshot,
    EvalResultView,
    EvalView,
    PolicyGateBindingViolationError,
    PolicyGateView,
    resolve_policy_gate,
    validate_binding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_A = uuid.uuid4()
_UUID_B = uuid.uuid4()
_UUID_C = uuid.uuid4()
_UUID_D = uuid.uuid4()
_UUID_E = uuid.uuid4()


def _pg_view(
    *,
    org_id: uuid.UUID = _UUID_A,
    node_id: uuid.UUID = _UUID_B,
    action: str = "warn",
) -> PolicyGateView:
    return PolicyGateView(
        id=uuid.uuid4(),
        organisation_id=org_id,
        version=1,
        node_id=node_id,
        action=action,
    )


def _eval_view(
    *,
    org_id: uuid.UUID = _UUID_A,
    node_id: uuid.UUID | None = _UUID_B,
    eval_type: str = "regex",
    deleted_at: datetime | None = None,
) -> EvalView:
    return EvalView(
        id=uuid.uuid4(),
        organisation_id=org_id,
        node_id=node_id,
        eval_type=eval_type,
        deleted_at=deleted_at,
    )


def _result_view(*, passed: bool) -> EvalResultView:
    return EvalResultView(id=uuid.uuid4(), passed=passed)


def _snapshot(
    *,
    pg: PolicyGateView | None = None,
    ev: EvalView | None = None,
    er: EvalResultView | None = None,
) -> EvalPolicySnapshot:
    return EvalPolicySnapshot(
        policy_gate=pg or _pg_view(),
        eval=ev or _eval_view(),
        eval_result=er,
    )


# ---------------------------------------------------------------------------
# C1: Action x result cross product (6 cases)
# ---------------------------------------------------------------------------


class TestC1ActionForResult:
    """warn/block x true/false/undefined -> resolved action."""

    @pytest.mark.parametrize("gate_action", ["warn", "block"])
    def test_true_yields_continue(self, gate_action: str) -> None:
        """result=true -> action='continue' regardless of configured action."""
        snap = _snapshot(
            pg=_pg_view(action=gate_action),
            ev=_eval_view(),
            er=_result_view(passed=True),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is True
        assert outcome.action == "continue"

    @pytest.mark.parametrize("gate_action", ["warn", "block"])
    def test_false_yields_configured_action(self, gate_action: str) -> None:
        """result=false -> action=<configured action>."""
        snap = _snapshot(
            pg=_pg_view(action=gate_action),
            ev=_eval_view(),
            er=_result_view(passed=False),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is False
        assert outcome.action == gate_action

    @pytest.mark.parametrize("gate_action", ["warn", "block"])
    def test_undefined_yields_configured_action(self, gate_action: str) -> None:
        """result=undefined -> action=<configured action> (fail-open/closed)."""
        snap = _snapshot(
            pg=_pg_view(action=gate_action),
            ev=_eval_view(),
            er=None,
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is None
        assert outcome.action == gate_action


# ---------------------------------------------------------------------------
# C18: Outcome.error is None whenever result is not undefined
# ---------------------------------------------------------------------------


class TestC18ErrorNoneForDefined:
    """For every true/false outcome, Outcome.error is None."""

    @pytest.mark.parametrize("gate_action", ["warn", "block"])
    def test_true_has_no_error(self, gate_action: str) -> None:
        snap = _snapshot(
            pg=_pg_view(action=gate_action),
            ev=_eval_view(),
            er=_result_view(passed=True),
        )
        assert resolve_policy_gate(snap).error is None

    @pytest.mark.parametrize("gate_action", ["warn", "block"])
    def test_false_has_no_error(self, gate_action: str) -> None:
        snap = _snapshot(
            pg=_pg_view(action=gate_action),
            ev=_eval_view(),
            er=_result_view(passed=False),
        )
        assert resolve_policy_gate(snap).error is None


# ---------------------------------------------------------------------------
# C2: eval_result_id identity
# ---------------------------------------------------------------------------


class TestC2EvalResultIdIdentity:
    """eval_result_id equals the specific triggering EvalResult.id."""

    def test_true_yields_correct_result_id(self) -> None:
        """Two EvalResult views built; assert the correct one's id surfaces."""
        result_a = EvalResultView(id=_UUID_C, passed=True)
        snap = _snapshot(
            pg=_pg_view(),
            ev=_eval_view(),
            er=result_a,
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.eval_result_id == _UUID_C
        assert outcome.eval_result_id != _UUID_D

    def test_false_yields_correct_result_id(self) -> None:
        """Two EvalResult views built; assert the correct one's id surfaces."""
        result_b = EvalResultView(id=_UUID_D, passed=False)
        snap = _snapshot(
            pg=_pg_view(),
            ev=_eval_view(),
            er=result_b,
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.eval_result_id == _UUID_D
        assert outcome.eval_result_id != _UUID_C

    def test_undefined_source_a_yields_none(self) -> None:
        """No EvalResult present -> eval_result_id is None."""
        snap = _snapshot(pg=_pg_view(), ev=_eval_view(), er=None)
        outcome = resolve_policy_gate(snap)
        assert outcome.eval_result_id is None

    def test_undefined_guardrail_populated_when_result_exists(self) -> None:
        """Source (b): guardrail re-check -> eval_result_id populated if present."""
        er = EvalResultView(id=_UUID_E, passed=True)
        snap = _snapshot(
            pg=_pg_view(),
            ev=_eval_view(eval_type="guardrail"),
            er=er,
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.eval_result_id == _UUID_E

    def test_undefined_node_mismatch_populated_when_result_exists(self) -> None:
        """Source (c): node_id mismatch -> eval_result_id populated if present."""
        er = EvalResultView(id=_UUID_E, passed=True)
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_C),
            ev=_eval_view(node_id=_UUID_D),
            er=er,
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.eval_result_id == _UUID_E


# ---------------------------------------------------------------------------
# C3: Outcome.error identifies the undefined source
# ---------------------------------------------------------------------------


class TestC3ErrorSourceToken:
    """Outcome.error carries the exact source token for each undefined case."""

    def test_source_a_token(self) -> None:
        snap = _snapshot(pg=_pg_view(), ev=_eval_view(), er=None)
        outcome = resolve_policy_gate(snap)
        assert outcome.error == "no_eval_result"

    def test_source_b_token(self) -> None:
        snap = _snapshot(
            pg=_pg_view(),
            ev=_eval_view(eval_type="guardrail"),
            er=_result_view(passed=False),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.error == "guardrail_recheck"

    def test_source_c_token_node_id_none(self) -> None:
        """Eval.node_id is None -> node_id mismatch (suite-scoped)."""
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_C),
            ev=_eval_view(node_id=None),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.error == "node_id_mismatch"

    def test_source_c_token_diverged(self) -> None:
        """PolicyGate.node_id != Eval.node_id -> node_id mismatch."""
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_C),
            ev=_eval_view(node_id=_UUID_D),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.error == "node_id_mismatch"

    def test_error_populated_regardless_of_configured_action(self) -> None:
        """error is set on undefined regardless of warn or block."""
        for action in ("warn", "block"):
            snap = _snapshot(
                pg=_pg_view(action=action),
                ev=_eval_view(),
                er=None,
            )
            outcome = resolve_policy_gate(snap)
            assert outcome.error == "no_eval_result"

    def test_defined_results_have_no_error(self) -> None:
        """True and false outcomes must carry error=None."""
        for passed in (True, False):
            snap = _snapshot(
                pg=_pg_view(),
                ev=_eval_view(),
                er=_result_view(passed=passed),
            )
            assert resolve_policy_gate(snap).error is None


# ---------------------------------------------------------------------------
# C5: Eval soft-delete causes undefined
# ---------------------------------------------------------------------------


class TestC5EvalSoftDelete:
    """resolve_policy_gate returns undefined when Eval.deleted_at is set."""

    def test_deleted_at_yields_undefined(self) -> None:
        snap = _snapshot(
            pg=_pg_view(),
            ev=_eval_view(deleted_at=datetime.now(tz=UTC)),
            er=_result_view(passed=True),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is None
        assert outcome.error == "no_eval_result"


# ---------------------------------------------------------------------------
# C6: validate_binding raises on cross-tenancy
# ---------------------------------------------------------------------------


class TestC6CrossTenancy:
    """validate_binding raises PolicyGateBindingViolationError on cross-tenancy."""

    def test_cross_tenancy_detected(self) -> None:
        pg = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_B}
        ev = {"id": uuid.uuid4(), "organisation_id": _UUID_C, "node_id": _UUID_B, "eval_type": "regex"}
        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(pg, ev)
        names = [v["exclusion"] for v in exc_info.value.violations]
        assert "cross_tenancy" in names

    def test_cross_tenancy_no_org_id_in_payload(self) -> None:
        """Payload must NOT contain any organisation-id VALUE."""
        pg = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_B}
        ev = {"id": uuid.uuid4(), "organisation_id": _UUID_C, "node_id": _UUID_B, "eval_type": "regex"}
        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(pg, ev)
        payload_str = str(exc_info.value.violations)
        assert str(_UUID_A) not in payload_str
        assert str(_UUID_C) not in payload_str

    def test_multi_violation_no_short_circuit(self) -> None:
        """Pass three simultaneous exclusions; assert ALL are present (no short-circuit).

        Note: suite_scoped (eval.node_id=None) and node_id_mismatch
        (eval.node_id != pg.node_id, eval.node_id is not None) are mutually
        exclusive -- you cannot trigger both in one call.  This test triggers
        cross_tenancy + guardrail_eval + node_id_mismatch simultaneously.
        """
        pg = {
            "id": uuid.uuid4(),
            "organisation_id": _UUID_A,
            "node_id": _UUID_B,
        }
        ev = {
            "id": uuid.uuid4(),
            "organisation_id": _UUID_C,
            "node_id": _UUID_D,
            "eval_type": "guardrail",
        }
        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(pg, ev)
        names = {v["exclusion"] for v in exc_info.value.violations}
        assert names == {"cross_tenancy", "guardrail_eval", "node_id_mismatch"}
        assert len(names) == 3, "All three violations must be collected (no short-circuit)"


# ---------------------------------------------------------------------------
# C7: validate_binding raises on guardrail-typed eval
# ---------------------------------------------------------------------------


class TestC7GuardrailEval:
    def test_guardrail_detected(self) -> None:
        pg = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_B}
        ev = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_B, "eval_type": "guardrail"}
        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(pg, ev)
        names = [v["exclusion"] for v in exc_info.value.violations]
        assert "guardrail_eval" in names


# ---------------------------------------------------------------------------
# C8: validate_binding raises on suite-scoped eval (node_id IS NULL)
# ---------------------------------------------------------------------------


class TestC8SuiteScopedEval:
    def test_suite_scoped_detected(self) -> None:
        pg = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_B}
        ev = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": None, "eval_type": "regex"}
        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(pg, ev)
        names = [v["exclusion"] for v in exc_info.value.violations]
        assert "suite_scoped_eval" in names


# ---------------------------------------------------------------------------
# C9: validate_binding raises on node_id mismatch
# ---------------------------------------------------------------------------


class TestC9NodeIdMismatch:
    def test_node_id_mismatch_detected(self) -> None:
        pg = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_B}
        ev = {"id": uuid.uuid4(), "organisation_id": _UUID_A, "node_id": _UUID_C, "eval_type": "regex"}
        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(pg, ev)
        names = [v["exclusion"] for v in exc_info.value.violations]
        assert "node_id_mismatch" in names


# ---------------------------------------------------------------------------
# C10: Guardrail exclusion at evaluation time (two-call structure)
# ---------------------------------------------------------------------------


class TestC10GuardrailReCheck:
    """Two-call: non-guardrail snapshot -> normal; guardrail snapshot -> undefined."""

    def test_non_guardrail_normal(self) -> None:
        """Non-guardrail eval + matching node_id + passed result -> defined."""
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_B),
            ev=_eval_view(node_id=_UUID_B, eval_type="regex"),
            er=_result_view(passed=True),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is True
        assert outcome.action == "continue"
        assert outcome.error is None

    def test_guardrail_undefined(self) -> None:
        """Guardrail eval + matching node_id -> undefined (source b)."""
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_B),
            ev=_eval_view(node_id=_UUID_B, eval_type="guardrail"),
            er=_result_view(passed=True),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is None
        assert outcome.error == "guardrail_recheck"


# ---------------------------------------------------------------------------
# C10 enforcement: constructing a view with str node_id raises TypeError
# ---------------------------------------------------------------------------


class TestC10UuidEnforcement:
    """PolicyGateView and EvalView must enforce UUID type on node_id (C10)."""

    def test_pg_view_str_node_id_raises(self) -> None:
        with pytest.raises(TypeError):
            PolicyGateView(
                id=uuid.uuid4(),
                organisation_id=_UUID_A,
                version=1,
                node_id="not-a-uuid",  # type: ignore[arg-type]
                action="warn",
            )

    def test_eval_view_str_node_id_raises(self) -> None:
        with pytest.raises(TypeError):
            EvalView(
                id=uuid.uuid4(),
                organisation_id=_UUID_A,
                node_id="not-a-uuid",  # type: ignore[arg-type]
                eval_type="regex",
                deleted_at=None,
            )


# ---------------------------------------------------------------------------
# C11: node_id-match re-check at evaluation time (two-call structure)
# ---------------------------------------------------------------------------


class TestC11NodeIdReCheck:
    """Two-call: matching node_ids -> normal; diverged node_ids -> undefined."""

    def test_matching_node_ids_normal(self) -> None:
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_B),
            ev=_eval_view(node_id=_UUID_B),
            er=_result_view(passed=False),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is False
        assert outcome.action == "warn"
        assert outcome.error is None

    def test_diverged_node_ids_undefined(self) -> None:
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_B),
            ev=_eval_view(node_id=_UUID_C),
            er=_result_view(passed=False),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is None
        assert outcome.error == "node_id_mismatch"

    def test_eval_node_id_none_undefined(self) -> None:
        """Eval.node_id is None (suite-scoped) -> node_id mismatch (source c)."""
        snap = _snapshot(
            pg=_pg_view(node_id=_UUID_B),
            ev=_eval_view(node_id=None),
            er=_result_view(passed=True),
        )
        outcome = resolve_policy_gate(snap)
        assert outcome.result is None
        assert outcome.error == "node_id_mismatch"
