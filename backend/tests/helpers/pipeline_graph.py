"""Shared FAR-2475 pipeline-graph fixture data.

The SQLite unit round-trip suite (``tests/unit/api/
test_pipeline_graph_roundtrip.py``) and the Postgres integration suite
(``tests/integration/api/test_pipeline_graph_contract.py``) exercise the same
graph payload. The fixed IDs and the payload builder live here so the two
suites cannot drift apart, and so the integration module no longer defines
``_pos``/``_build_graph_payload`` twice.
"""

from __future__ import annotations

import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Graph fixture constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
_PIPELINE_ID_2 = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
_AGENT_ROW_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a9")
_CONNECTOR_INSTANCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

_MANUAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
_SANDBOX_ID = uuid.UUID("00000000-0000-0000-0000-000000000102")
_ROUTER_ID = uuid.UUID("00000000-0000-0000-0000-000000000103")
_HITL_ID = uuid.UUID("00000000-0000-0000-0000-000000000104")
_AGENT_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000105")
_JOIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000106")
_CONNECTOR_FULL_ID = uuid.UUID("00000000-0000-0000-0000-000000000107")
_CONNECTOR_MIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000108")

_E1_ID = uuid.UUID("00000000-0000-0000-0000-000000000201")
_E2_ID = uuid.UUID("00000000-0000-0000-0000-000000000202")
_E3_ID = uuid.UUID("00000000-0000-0000-0000-000000000203")
_E4_ID = uuid.UUID("00000000-0000-0000-0000-000000000204")  # hitl gate edge
_E5_ID = uuid.UUID("00000000-0000-0000-0000-000000000205")  # reject edge
_E6_ID = uuid.UUID("00000000-0000-0000-0000-000000000206")  # conditional edge
_E7_ID = uuid.UUID("00000000-0000-0000-0000-000000000207")
_E8_ID = uuid.UUID("00000000-0000-0000-0000-000000000208")


# ---------------------------------------------------------------------------
# Graph fixture builder
# ---------------------------------------------------------------------------


def _pos(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y}


def _build_graph_payload() -> dict[str, Any]:
    """Construct the full graph payload used by all round-trip tests."""
    return {
        "nodes": [
            # Manual start node
            {
                "id": str(_MANUAL_ID),
                "node_type": "manual",
                "position": _pos(10.0, 20.0),
                "label": "Start",
                "description": "Manual kick-off",
                "output_schema_json": {"type": "object"},
            },
            # Sandbox agent (llm mode) with many semantic fields
            {
                "id": str(_SANDBOX_ID),
                "node_type": "sandbox_agent",
                "position": _pos(120.0, 20.0),
                "label": "Sandbox",
                "template_id": "opencode",
                "mode": "llm",
                "agent_commands": ["echo first", "echo second"],
                "agent_prompt": "Do the thing",
                "env_vars": {
                    "OPENAI_API_KEY": "plain-placeholder-not-a-real-secret",
                    "LOG_LEVEL": "debug",
                },
                "context_files": {"/tmp/input.txt": "contents"},
                "timeout_seconds": 900,
                "stall_timeout_seconds": 120,
                "enable_heartbeat": False,
                "read_only": True,
                "git_credentials": "scoped",
                "egress_policy": "deny_all",
                "stdout_retention_mode": "full",
                "stdout_max_bytes": 4096,
                "idempotent": False,
            },
            # Router node
            {
                "id": str(_ROUTER_ID),
                "node_type": "router",
                "position": _pos(230.0, 20.0),
                "label": "Route",
                "router_config": {
                    "mode": "rules",
                    "rules": [
                        {"guard": "`true`", "target": str(_HITL_ID)},
                        {"default": True, "target": str(_HITL_ID)},
                    ],
                },
            },
            # HITL node
            {
                "id": str(_HITL_ID),
                "node_type": "hitl",
                "position": _pos(340.0, 20.0),
                "label": "Approve",
                "hitl_config": {
                    "mode": "approval",
                    "description": "A human must approve the sandbox output before the agent continues downstream.",
                    "claim_expiry_minutes": 30,
                    "human_only": True,
                },
            },
            # Agent node (requires Agent row)
            {
                "id": str(_AGENT_NODE_ID),
                "node_type": "agent",
                "position": _pos(450.0, 20.0),
                "label": "Agent",
                "agent_id": str(_AGENT_ROW_ID),
                "timeout_seconds": 600,
            },
            # Join node
            {
                "id": str(_JOIN_ID),
                "node_type": "join",
                "position": _pos(560.0, 20.0),
                "label": "Join",
                "collect": [{"node": str(_AGENT_NODE_ID)}],
                "aggregate": {"kind": "concat"},
                "join_partial_policy": "fail",
            },
            # Connector node - full binding (resource + filters + data)
            {
                "id": str(_CONNECTOR_FULL_ID),
                "node_type": "connector",
                "position": _pos(670.0, 20.0),
                "label": "Fetch PR",
                "connector_binding": {
                    "type": "github",
                    "instance_id": str(_CONNECTOR_INSTANCE_ID),
                    "operation": "query",
                    "resource": "pull",
                    "filters": {"state": "open", "labels": ["agent", "backend"], "limit": 25},
                    "data": {"note": "round-trip"},
                },
            },
            # Connector node - minimal binding (only resource)
            {
                "id": str(_CONNECTOR_MIN_ID),
                "node_type": "connector",
                "position": _pos(780.0, 20.0),
                "label": "Fetch Issue",
                "connector_binding": {
                    "type": "github",
                    "instance_id": str(_CONNECTOR_INSTANCE_ID),
                    "operation": "query",
                    "resource": "issue",
                },
            },
        ],
        "edges": [
            {
                "id": str(_E1_ID),
                "source_node_id": str(_MANUAL_ID),
                "target_node_id": str(_SANDBOX_ID),
                "edge_type": "normal",
            },
            {
                "id": str(_E2_ID),
                "source_node_id": str(_SANDBOX_ID),
                "target_node_id": str(_ROUTER_ID),
                "edge_type": "normal",
            },
            {
                "id": str(_E3_ID),
                "source_node_id": str(_ROUTER_ID),
                "target_node_id": str(_HITL_ID),
                "edge_type": "normal",
            },
            # HITL gate edge with review config
            {
                "id": str(_E4_ID),
                "source_node_id": str(_HITL_ID),
                "target_node_id": str(_AGENT_NODE_ID),
                "edge_type": "normal",
                "hitl_review_config": {
                    "label": "Approve agent output",
                    "description": "A human must approve the agent output before the join continues.",
                    "claim_expiry_minutes": 30,
                    "human_only": True,
                },
            },
            # Reject edge
            {
                "id": str(_E5_ID),
                "source_node_id": str(_HITL_ID),
                "target_node_id": str(_SANDBOX_ID),
                "edge_type": "reject",
            },
            # Conditional edge from agent to join
            {
                "id": str(_E6_ID),
                "source_node_id": str(_AGENT_NODE_ID),
                "target_node_id": str(_JOIN_ID),
                "edge_type": "conditional",
                "condition_expression": "`true`",
            },
            {
                "id": str(_E7_ID),
                "source_node_id": str(_JOIN_ID),
                "target_node_id": str(_CONNECTOR_FULL_ID),
                "edge_type": "normal",
            },
            {
                "id": str(_E8_ID),
                "source_node_id": str(_CONNECTOR_FULL_ID),
                "target_node_id": str(_CONNECTOR_MIN_ID),
                "edge_type": "normal",
            },
        ],
    }
