"""Additional guardrail compensation branch coverage."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.guardrails.compensation as comp


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def execute(self, _stmt):
        if self.error:
            raise self.error
        return _Scalar(self.result)


@pytest.mark.parametrize("value", [None, "not-a-uuid", object()])
def test_parse_uuid_rejects_invalid_values(value):
    assert comp._parse_uuid(value) is None


def test_parse_uuid_accepts_uuid_string():
    value = uuid.uuid4()
    assert comp._parse_uuid(str(value)) == value


@pytest.mark.asyncio
async def test_load_graph_nodes_handles_errors_and_invalid_shapes():
    run = MagicMock(id=uuid.uuid4(), snapshot_id=uuid.uuid4())
    assert not await comp._load_graph_nodes(_Session(result=None), run)
    assert not await comp._load_graph_nodes(_Session(result={"nodes": "wrong"}), run)
    assert not await comp._load_graph_nodes(_Session(error=RuntimeError("db")), run)


@pytest.mark.asyncio
async def test_attempt_audit_cancellation_propagates():
    with (
        patch.object(comp, "append_audit_event", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await comp._append_attempt_audit(
            MagicMock(),
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            node_id="node",
            resource="pr",
            outcome=comp.CompensationOutcome.COMPENSATED,
            detail="done",
        )


@pytest.mark.asyncio
async def test_summary_audit_cancellation_propagates():
    with (
        patch.object(comp, "append_audit_event", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await comp._append_summary_audit(
            MagicMock(),
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            blocking_eval_name="eval",
            executed={"node": {}},
        )


@pytest.mark.asyncio
async def test_compensate_one_node_publishes_unbound_node():
    result = await comp._compensate_one_node(
        MagicMock(),
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        node_id="agent",
        node_output={"output": {"secret": "not persisted"}},
        node_def={},
        connector_hub=MagicMock(),
        guardrail_block="blocked",
    )
    assert result["publish_status"] == "published"
    assert result["compensation"] is None


@pytest.mark.asyncio
async def test_compensate_one_node_invalid_result_is_failed(audit_patch=None):
    connector = MagicMock()
    connector.compensate = AsyncMock(return_value=object())
    hub = MagicMock()
    hub.get.return_value = connector
    with patch.object(comp, "_append_attempt_audit", new_callable=AsyncMock) as audit:
        result = await comp._compensate_one_node(
            MagicMock(),
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            node_id="node",
            node_output={"output": {"n": 1}},
            node_def={"connector_binding": {"instance_id": str(uuid.uuid4()), "resource": "pr"}},
            connector_hub=hub,
            guardrail_block="blocked",
        )
    assert result["publish_status"] == "not-compensated"
    assert result["compensation"]["outcome"] == "failed"
    audit.assert_awaited_once()
