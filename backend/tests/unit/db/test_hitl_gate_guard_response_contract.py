"""Unit tests for FAR-860 HITL gate-weakening detection with response_contract.

Covers ``_weakening_types`` detecting response_contract changes as gate
weakening.
"""

from modulo.db.crud.hitl_gate_guard import _weakening_types


class TestResponseContractWeakening:
    def test_no_change_no_weakening(self):
        old = {"human_only": True, "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}]}}
        new = {"human_only": True, "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}]}}
        assert "response_contract" not in _weakening_types(old, new)

    def test_adding_response_contract(self):
        old = {"human_only": True}
        new = {"human_only": True, "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}]}}
        assert "response_contract" in _weakening_types(old, new)

    def test_removing_response_contract(self):
        old = {"human_only": True, "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}]}}
        new = {"human_only": True}
        assert "response_contract" in _weakening_types(old, new)

    def test_changing_kind(self):
        old = {"human_only": True, "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}]}}
        new = {"human_only": True, "response_contract": {"kind": "approval"}}
        assert "response_contract" in _weakening_types(old, new)

    def test_changing_options(self):
        old = {"human_only": True, "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}]}}
        new = {
            "human_only": True,
            "response_contract": {"kind": "choice", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
        }
        assert "response_contract" in _weakening_types(old, new)

    def test_no_response_contract_on_both_no_weakening(self):
        old = {"human_only": True}
        new = {"human_only": True}
        assert "response_contract" not in _weakening_types(old, new)
