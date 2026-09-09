"""Unit tests for FAR-611 client-type audit enrichment on HITL decisions.

``claim`` / ``approve`` / ``approve_with_modification`` / ``reject`` /
``deliver_manual`` accept an optional ``client_type`` (``"browser"`` for JWT
logins, ``"api_key"`` for mk_ keys, ``"mcp"`` for MCP transport) that lands in
the audit event payload. Internal callers that omit it keep payloads unchanged
(no ``client_type`` key).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from modulo.core.hitl_manager import HITLManager

from .conftest import _session_decide
from .test_hitl_manager import _GATE, _ORG, _RUN, _USER, _gate, _session_update

_BROWSER = "browser"
_API_KEY = "api_key"
_MCP = "mcp"


def _decided(decision: str):
    """A decided gate row + the session mock that yields it from _decide()."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision=decision)
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    return session, gate_decided


async def _approve_payload(client_type: str | None) -> dict:
    session, _gate_decided = _decided("approved")
    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        kwargs = {} if client_type is None else {"client_type": client_type}
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", **kwargs)
    return mock_audit.await_args.kwargs["payload_json"]


class TestApproveClientType:
    async def test_browser_client_type_recorded(self):
        payload = await _approve_payload(_BROWSER)
        assert payload["client_type"] == "browser"

    async def test_api_key_client_type_recorded(self):
        payload = await _approve_payload(_API_KEY)
        assert payload["client_type"] == "api_key"

    async def test_mcp_client_type_recorded(self):
        payload = await _approve_payload(_MCP)
        assert payload["client_type"] == "mcp"

    async def test_no_client_type_omits_key(self):
        """Internal callers that cannot know the client keep the payload unchanged."""
        payload = await _approve_payload(None)
        assert "client_type" not in payload


class TestRejectAndDeliverManualClientType:
    async def test_reject_records_client_type(self):
        session, _gate_decided = _decided("rejected")
        with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
            mgr = HITLManager()
            await mgr.reject(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claim_token="tok",
                actor_id=_USER,
                client_type=_MCP,
            )
        payload = mock_audit.await_args.kwargs["payload_json"]
        assert payload["client_type"] == "mcp"
        assert payload["decision"] == "rejected"

    async def test_reject_without_client_type_omits_key(self):
        session, _gate_decided = _decided("rejected")
        with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
            mgr = HITLManager()
            await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)
        assert "client_type" not in mock_audit.await_args.kwargs["payload_json"]

    async def test_deliver_manual_records_client_type(self):
        session, _gate_decided = _decided("deliver_manual")
        with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
            mgr = HITLManager()
            await mgr.deliver_manual(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claim_token="tok",
                output={"value": 1},
                actor_id=_USER,
                client_type=_BROWSER,
            )
        payload = mock_audit.await_args.kwargs["payload_json"]
        assert payload["client_type"] == "browser"
        assert payload["decision"] == "deliver_manual"


class TestApproveWithModificationClientType:
    async def test_both_events_record_client_type(self):
        session, _gate_decided = _decided("approved")
        with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
            mgr = HITLManager()
            await mgr.approve_with_modification(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claim_token="tok",
                modified_output={"value": 42},
                actor_id=_USER,
                client_type=_API_KEY,
            )
        assert mock_audit.await_count == 2
        for call in mock_audit.await_args_list:
            assert call.kwargs["payload_json"]["client_type"] == "api_key"


class TestClaimClientType:
    async def test_claim_records_client_type(self):
        """The hitl_claimed audit event carries the caller's client type."""
        future = datetime.now(UTC) + timedelta(minutes=5)
        gate = _gate(account_id=None, claim_token=None, expires_at=future)
        session = _session_update(pre_check_gate=gate, gate=gate)

        with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
            mgr = HITLManager()
            await mgr.claim(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claimant_id=_USER,
                client_type=_BROWSER,
            )

        payload = mock_audit.await_args.kwargs["payload_json"]
        assert payload["client_type"] == "browser"
        assert payload["node_id"] == _GATE

    async def test_claim_without_client_type_omits_key(self):
        future = datetime.now(UTC) + timedelta(minutes=5)
        gate = _gate(account_id=None, claim_token=None, expires_at=future)
        session = _session_update(pre_check_gate=gate, gate=gate)

        with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
            mgr = HITLManager()
            await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

        assert "client_type" not in mock_audit.await_args.kwargs["payload_json"]
