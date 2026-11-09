"""FAR-2475: Docker-backed integration test for the real graph persistence contract.

This test runs against a real Postgres instance (testcontainers) and exercises
the exact same ASGI HTTP path as production. It is SKIPPED locally when Docker
is unavailable and runs in CI.

It mirrors the assertions in ``tests/unit/api/test_pipeline_graph_roundtrip.py``
but against a real Postgres database with native advisory locks, RLS and
PG-specific types.
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
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
_E4_ID = uuid.UUID("00000000-0000-0000-0000-000000000204")
_E5_ID = uuid.UUID("00000000-0000-0000-0000-000000000205")
_E6_ID = uuid.UUID("00000000-0000-0000-0000-000000000206")
_E7_ID = uuid.UUID("00000000-0000-0000-0000-000000000207")
_E8_ID = uuid.UUID("00000000-0000-0000-0000-000000000208")

# ---------------------------------------------------------------------------
# Test graph builder (same as unit roundtrip)
# ---------------------------------------------------------------------------


def _pos(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y}


def _build_graph_payload() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": str(_MANUAL_ID),
                "node_type": "manual",
                "position": _pos(10.0, 20.0),
                "label": "Start",
                "description": "Manual kick-off",
                "output_schema_json": {"type": "object"},
            },
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
            {
                "id": str(_AGENT_NODE_ID),
                "node_type": "agent",
                "position": _pos(450.0, 20.0),
                "label": "Agent",
                "agent_id": str(_AGENT_ROW_ID),
                "timeout_seconds": 600,
            },
            {
                "id": str(_JOIN_ID),
                "node_type": "join",
                "position": _pos(560.0, 20.0),
                "label": "Join",
                "collect": [{"node": str(_AGENT_NODE_ID)}],
                "aggregate": {"kind": "concat"},
                "join_partial_policy": "fail",
            },
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
            {
                "id": str(_E5_ID),
                "source_node_id": str(_HITL_ID),
                "target_node_id": str(_SANDBOX_ID),
                "edge_type": "reject",
            },
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


# ---------------------------------------------------------------------------
# Fixtures: seed Postgres with org, admin, pipeline, agent, connector
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_env(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
) -> dict[str, Any]:
    """Seed a fresh org, admin account, pipeline, agent, connector and login."""
    # Seed via raw SQL (matches test_evals_redirect pattern)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(_ORG_ID), "name": f"Roundtrip {str(_ORG_ID)[:8]}", "slug": f"rt-{str(_ORG_ID)[:8]}"},
        )
        from modulo.auth.passwords import hash_password

        password = "roundtrip-password-1"
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, :hash, 'local', true)"
            ),
            {
                "id": str(_ACCOUNT_ID),
                "email": f"rt-{str(_ACCOUNT_ID)[:10]}@example.com",
                "name": "Roundtrip Admin",
                "hash": hash_password(password),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'admin')"
            ),
            {"id": str(uuid.uuid4()), "aid": str(_ACCOUNT_ID), "oid": str(_ORG_ID)},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, organisation_id, account_id, name, prompt_template, "
                "is_executable, prompt_always_visible, prompt_version_history, connector_type_refs, "
                "required_environment_capabilities, retry_policy) "
                "VALUES (:id, :oid, :uid, :name, :prompt, true, false, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json)"
            ),
            {
                "id": str(_AGENT_ROW_ID),
                "oid": str(_ORG_ID),
                "uid": str(_ACCOUNT_ID),
                "name": "Roundtrip Agent",
                "prompt": "You are a helpful assistant.",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO connector_instances (id, organisation_id, account_id, name, connector_type_id, "
                "credentials_ciphertext, config_json, allowed_operations, status, visibility) "
                "VALUES (:id, :oid, :uid, :name, 'github', :cred, '{}'::json, '[]'::json, 'active', 'org')"
            ),
            {
                "id": str(_CONNECTOR_INSTANCE_ID),
                "oid": str(_ORG_ID),
                "uid": str(_ACCOUNT_ID),
                "name": "GitHub Roundtrip",
                "cred": b"{}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, account_id, name, graph_nodes_json) "
                "VALUES (:id, :oid, :uid, :name, '[]'::json)"
            ),
            {"id": str(_PIPELINE_ID), "oid": str(_ORG_ID), "uid": str(_ACCOUNT_ID), "name": "Roundtrip Pipeline"},
        )

    # Login
    email = f"rt-{str(_ACCOUNT_ID)[:10]}@example.com"
    login = await integration_client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    return {"headers": headers, "org_id": _ORG_ID, "account_id": _ACCOUNT_ID, "pipeline_id": _PIPELINE_ID}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineGraphContractPostgres:
    """Real Postgres round-trip assertions matching the unit test suite."""

    async def test_postgres_rest_roundtrip_preserves_semantic_graph_and_snapshot(
        self,
        integration_client: AsyncClient,
        seeded_env: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        headers = seeded_env["headers"]
        pipeline_id = seeded_env["pipeline_id"]
        payload = _build_graph_payload()

        # PUT graph
        put_resp = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json=payload,
            headers=headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        put_graph = put_resp.json()

        # GET graph
        get_resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=headers,
        )
        assert get_resp.status_code == 200, get_resp.text
        get_graph = get_resp.json()

        # Semantic equivalence
        put_nodes = {n["id"]: n for n in put_graph["nodes"]}
        get_nodes = {n["id"]: n for n in get_graph["nodes"]}
        for n_payload in payload["nodes"]:
            nid = n_payload["id"]
            _assert_semantic_node_eq(put_nodes[nid], get_nodes[nid], path=f"node {nid}")
            _assert_semantic_node_eq(get_nodes[nid], n_payload, path=f"node {nid}")

        put_edges = {e["id"]: e for e in put_graph["edges"]}
        get_edges = {e["id"]: e for e in get_graph["edges"]}
        for e_payload in payload["edges"]:
            eid = e_payload["id"]
            _assert_semantic_edge_eq(put_edges[eid], get_edges[eid], path=f"edge {eid}")
            _assert_semantic_edge_eq(get_edges[eid], e_payload, path=f"edge {eid}")

        # Persisted DB state
        async with db_engine.begin() as conn:
            # Pipeline nodes
            pipeline_row = await conn.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
            pipeline = pipeline_row.scalar_one()
            persisted_nodes = {n["id"]: n for n in pipeline.graph_nodes_json}
            for n_payload in payload["nodes"]:
                _assert_semantic_node_eq(
                    persisted_nodes[n_payload["id"]], n_payload, path=f"persisted node {n_payload['id']}"
                )

            # Edges
            edges = list(
                (await conn.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))).scalars()
            )
            persisted_edges = {e.id: e for e in edges}
            for e_payload in payload["edges"]:
                eid = uuid.UUID(e_payload["id"])
                edge = persisted_edges[eid]
                _assert_semantic_edge_eq(
                    {
                        "id": str(edge.id),
                        "source_node_id": str(edge.source_node_id),
                        "target_node_id": str(edge.target_node_id),
                        "edge_type": edge.edge_type,
                        "source_port": edge.source_port or "out",
                        "target_port": edge.target_port or "in",
                        "condition_expression": edge.condition_expression,
                        "hitl_review_config": edge.hitl_review_config,
                    },
                    e_payload,
                    path=f"persisted edge {eid}",
                )

            # Snapshot
            snaps = list(
                (
                    await conn.execute(
                        select(PipelineSnapshot)
                        .where(PipelineSnapshot.pipeline_id == pipeline_id)
                        .order_by(PipelineSnapshot.snapshot_version)
                    )
                ).scalars()
            )
            assert snaps, "edit snapshot not created"
            latest = snaps[-1]
            assert latest.version_kind == "edit"
            snap_nodes = {n["id"]: n for n in latest.graph_json["nodes"]}
            snap_edges = {e["id"]: e for e in latest.graph_json["edges"]}
            for n_payload in payload["nodes"]:
                nid = n_payload["id"]
                snap_node = snap_nodes[nid]
                if n_payload["node_type"] == "agent":
                    assert "prompt_template" in snap_node
                _assert_semantic_node_eq(snap_node, n_payload, path=f"snapshot node {nid}")
            for e_payload in payload["edges"]:
                _assert_semantic_edge_eq(
                    snap_edges[e_payload["id"]], e_payload, path=f"snapshot edge {e_payload['id']}"
                )

    async def test_postgres_write_vs_response_drops_unset_connector_binding_inputs(
        self,
        integration_client: AsyncClient,
        seeded_env: dict[str, Any],
    ) -> None:
        headers = seeded_env["headers"]
        pipeline_id = seeded_env["pipeline_id"]
        payload = _build_graph_payload()

        put_resp = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json=payload,
            headers=headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        put_graph = put_resp.json()

        min_node = next(n for n in put_graph["nodes"] if n["id"] == str(_CONNECTOR_MIN_ID))
        binding = min_node.get("connector_binding")
        assert binding is not None
        assert "resource" in binding
        assert "filters" not in binding
        assert "data" not in binding
        assert "operation" in binding
        assert "instance_id" in binding

        full_node = next(n for n in put_graph["nodes"] if n["id"] == str(_CONNECTOR_FULL_ID))
        full_binding = full_node["connector_binding"]
        assert "resource" in full_binding
        assert "filters" in full_binding
        assert "data" in full_binding

    async def test_postgres_env_vars_are_masked_on_read(
        self,
        integration_client: AsyncClient,
        seeded_env: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        headers = seeded_env["headers"]
        pipeline_id = seeded_env["pipeline_id"]
        payload = _build_graph_payload()

        put_resp = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json=payload,
            headers=headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        put_graph = put_resp.json()

        sb_node = next(n for n in put_graph["nodes"] if n["id"] == str(_SANDBOX_ID))
        env = sb_node.get("env_vars", {})
        assert "OPENAI_API_KEY" in env
        assert env["OPENAI_API_KEY"] == "SENSITIVE_VALUE_MASK"

        # Persisted value is real
        async with db_engine.begin() as conn:
            pipeline_row = await conn.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
            pipeline = pipeline_row.scalar_one()
            sb = next(n for n in pipeline.graph_nodes_json if n["id"] == str(_SANDBOX_ID))
            assert sb["env_vars"]["OPENAI_API_KEY"] == "plain-placeholder-not-a-real-secret"


# ---------------------------------------------------------------------------
# Helpers (duplicated from unit test to keep integration self-contained)
# ---------------------------------------------------------------------------


def _pos(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y}


def _build_graph_payload() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": str(_MANUAL_ID),
                "node_type": "manual",
                "position": _pos(10.0, 20.0),
                "label": "Start",
                "description": "Manual kick-off",
                "output_schema_json": {"type": "object"},
            },
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
            {
                "id": str(_AGENT_NODE_ID),
                "node_type": "agent",
                "position": _pos(450.0, 20.0),
                "label": "Agent",
                "agent_id": str(_AGENT_ROW_ID),
                "timeout_seconds": 600,
            },
            {
                "id": str(_JOIN_ID),
                "node_type": "join",
                "position": _pos(560.0, 20.0),
                "label": "Join",
                "collect": [{"node": str(_AGENT_NODE_ID)}],
                "aggregate": {"kind": "concat"},
                "join_partial_policy": "fail",
            },
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
            {
                "id": str(_E5_ID),
                "source_node_id": str(_HITL_ID),
                "target_node_id": str(_SANDBOX_ID),
                "edge_type": "reject",
            },
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


# ---------------------------------------------------------------------------
# Shared assertion helpers (duplicated for independence)
# ---------------------------------------------------------------------------


def _assert_semantic_node_eq(a: dict[str, Any], b: dict[str, Any], *, path: str = "node") -> None:
    for key in (
        "id",
        "node_type",
        "position",
        "label",
        "description",
        "mode",
        "agent_commands",
        "agent_prompt",
        "template_id",
        "timeout_seconds",
        "stall_timeout_seconds",
        "enable_heartbeat",
        "read_only",
        "git_credentials",
        "egress_policy",
        "stdout_retention_mode",
        "stdout_max_bytes",
        "idempotent",
        "router_config",
        "hitl_config",
        "output_schema_json",
        "agent_id",
        "collect",
        "aggregate",
        "join_partial_policy",
        "connector_binding",
    ):
        if key == "connector_binding" and a.get(key) is not None and b.get(key) is not None:
            _assert_connector_binding_eq(a[key], b[key], path=f"{path}.connector_binding")
            continue
        if key == "env_vars":
            assert isinstance(a.get(key), dict), f"{path}.env_vars (A)"
            assert isinstance(b.get(key), dict), f"{path}.env_vars (B)"
            assert set(a[key].keys()) == set(b[key].keys()), f"{path}.env_vars keys"
            for k, v in a[key].items():
                assert isinstance(v, str), f"{path}.env_vars[{k}]"
            continue
        if key == "context_files":
            assert a.get(key) == b.get(key), f"{path}.context_files"
            continue
        assert a.get(key) == b.get(key), f"{path}.{key}: {a.get(key)!r} != {b.get(key)!r}"


def _assert_connector_binding_eq(a: dict[str, Any], b: dict[str, Any], path: str = "") -> None:
    for key in ("type", "instance_id", "operation", "dispatch_action", "resource", "filters", "data"):
        assert a.get(key) == b.get(key), f"{path}.{key}"


def _assert_semantic_edge_eq(a: dict[str, Any], b: dict[str, Any], path: str = "edge") -> None:
    for key in (
        "id",
        "source_node_id",
        "target_node_id",
        "edge_type",
        "source_port",
        "target_port",
        "condition_expression",
        "hitl_review_config",
    ):
        if key == "hitl_review_config":
            if a.get(key) is not None and b.get(key) is not None:
                for sub in ("label", "description", "claim_expiry_minutes", "human_only"):
                    assert a[key].get(sub) == b[key].get(sub), f"{path}.hitl_review_config.{sub}"
            else:
                assert a.get(key) == b.get(key), f"{path}.hitl_review_config"
            continue
        assert a.get(key) == b.get(key), f"{path}.{key}"


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
