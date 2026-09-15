"""Unit tests for FAR-860 HITLManager audit payload enrichment with answer.

Covers ``_base_audit_payload`` including ``answer_kind`` and
``answer_option_id`` fields.
"""

from unittest.mock import MagicMock

from modulo.core.hitl_manager import HITLManager


def _make_gate(**overrides: object) -> MagicMock:
    gate = MagicMock()
    gate.run_id = overrides.get("run_id", "run-1")
    gate.gate_id = overrides.get("gate_id", "gate-1")
    gate.decision = overrides.get("decision", "approved")
    gate.required_team_id = overrides.get("required_team_id")
    return gate


class TestBaseAuditPayloadWithAnswer:
    def test_no_answer(self):
        gate = _make_gate()
        payload = HITLManager._base_audit_payload(gate, client_type="browser")
        assert "answer_kind" not in payload
        assert "answer_option_id" not in payload

    def test_choice_answer(self):
        gate = _make_gate()
        answer = {"kind": "choice", "option_id": "approve"}
        payload = HITLManager._base_audit_payload(gate, client_type="mcp", answer=answer)
        assert payload["answer_kind"] == "choice"
        assert payload["answer_option_id"] == "approve"

    def test_approval_answer(self):
        gate = _make_gate()
        answer = {"kind": "approval"}
        payload = HITLManager._base_audit_payload(gate, answer=answer)
        assert payload["answer_kind"] == "approval"
        assert "answer_option_id" not in payload

    def test_empty_answer_no_fields(self):
        gate = _make_gate()
        payload = HITLManager._base_audit_payload(gate, answer={})
        assert "answer_kind" not in payload
        assert "answer_option_id" not in payload

    def test_none_answer_no_fields(self):
        gate = _make_gate()
        payload = HITLManager._base_audit_payload(gate, answer=None)
        assert "answer_kind" not in payload
        assert "answer_option_id" not in payload
