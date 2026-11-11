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
from tests.helpers.pipeline_graph import (
    _ACCOUNT_ID,
    _AGENT_ROW_ID,
    _CONNECTOR_FULL_ID,
    _CONNECTOR_INSTANCE_ID,
    _CONNECTOR_MIN_ID,
    _ORG_ID,
    _PIPELINE_ID,
    _SANDBOX_ID,
    _build_graph_payload,
)

from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_edge import PipelineEdge
from farna.db.models.pipeline_snapshot import PipelineSnapshot

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures: seed Postgres with org, admin, pipeline, agent, connector
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_env(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
) -> dict[str, Any]:
    """Seed a fresh org, admin account, pipeline, agent, connector and login.

    ``db_engine`` is session-scoped, so every test in this module shares one
    database. The seed rows below use fixed IDs, which means the fixture must be
    idempotent: re-running it for the second test must not violate a primary
    key, and the pipeline's mutable graph state (nodes/edges/snapshots) must be
    reset so each test starts from an empty graph.
    """
    # Seed via raw SQL (matches test_evals_redirect pattern)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"),
            {"pid": str(_PIPELINE_ID)},
        )
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(_PIPELINE_ID)},
        )
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, '{}'::json) ON CONFLICT DO NOTHING"
            ),
            {"id": str(_ORG_ID), "name": f"Roundtrip {str(_ORG_ID)[:8]}", "slug": f"rt-{str(_ORG_ID)[:8]}"},
        )
        from farna.auth.passwords import hash_password

        password = "roundtrip-password-1"
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, :hash, 'local', true) ON CONFLICT DO NOTHING"
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
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'admin') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(uuid.uuid4()), "aid": str(_ACCOUNT_ID), "oid": str(_ORG_ID)},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, organisation_id, account_id, name, prompt_template, "
                "is_executable, prompt_always_visible, prompt_version_history, connector_type_refs, "
                "required_environment_capabilities, retry_policy) "
                "VALUES (:id, :oid, :uid, :name, :prompt, true, false, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json) ON CONFLICT DO NOTHING"
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
                "VALUES (:id, :oid, :uid, :name, 'github', :cred, '{}'::json, '[]'::json, 'active', 'org') "
                "ON CONFLICT DO NOTHING"
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
                "VALUES (:id, :oid, :uid, :name, '[]'::json) ON CONFLICT DO NOTHING"
            ),
            {"id": str(_PIPELINE_ID), "oid": str(_ORG_ID), "uid": str(_ACCOUNT_ID), "name": "Roundtrip Pipeline"},
        )

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipelines SET graph_nodes_json = '[]'::json WHERE id = :pid"),
            {"pid": str(_PIPELINE_ID)},
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
            _assert_node_matches_payload(get_nodes[nid], n_payload, path=f"node {nid}", masked=True)

        put_edges = {e["id"]: e for e in put_graph["edges"]}
        get_edges = {e["id"]: e for e in get_graph["edges"]}
        for e_payload in payload["edges"]:
            eid = e_payload["id"]
            _assert_semantic_edge_eq(put_edges[eid], get_edges[eid], path=f"edge {eid}")
            _assert_semantic_edge_eq(get_edges[eid], _edge_payload_with_defaults(e_payload), path=f"edge {eid}")

        # Persisted DB state
        async with db_engine.begin() as conn:
            # Pipeline nodes
            # A raw AsyncConnection does not materialise ORM entities, so select
            # the JSON column directly rather than the whole entity.
            graph_nodes = (
                await conn.execute(select(Pipeline.graph_nodes_json).where(Pipeline.id == pipeline_id))
            ).scalar_one()
            persisted_nodes = {n["id"]: n for n in graph_nodes}
            for n_payload in payload["nodes"]:
                _assert_node_matches_payload(
                    persisted_nodes[n_payload["id"]],
                    n_payload,
                    path=f"persisted node {n_payload['id']}",
                    masked=False,
                )

            # Edges
            # Select explicit columns: a raw AsyncConnection returns column
            # values, not ORM entities (see the pipeline-nodes read above).
            edges = (
                await conn.execute(
                    select(
                        PipelineEdge.id,
                        PipelineEdge.source_node_id,
                        PipelineEdge.target_node_id,
                        PipelineEdge.edge_type,
                        PipelineEdge.source_port,
                        PipelineEdge.target_port,
                        PipelineEdge.condition_expression,
                        PipelineEdge.hitl_review_config,
                    ).where(PipelineEdge.pipeline_id == pipeline_id)
                )
            ).all()
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
                    _edge_payload_with_defaults(e_payload),
                    path=f"persisted edge {eid}",
                )

            # Snapshot
            # Explicit columns for the same raw-connection reason as above.
            snaps = (
                await conn.execute(
                    select(PipelineSnapshot.version_kind, PipelineSnapshot.graph_json)
                    .where(PipelineSnapshot.pipeline_id == pipeline_id)
                    .order_by(PipelineSnapshot.snapshot_version)
                )
            ).all()
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
                _assert_node_matches_payload(snap_node, n_payload, path=f"snapshot node {nid}", masked=False)
            for e_payload in payload["edges"]:
                eid = e_payload["id"]
                snap_edge = snap_edges[eid]
                _assert_semantic_edge_eq(
                    {
                        "id": snap_edge["id"],
                        "source_node_id": snap_edge.get("source", snap_edge.get("source_node_id")),
                        "target_node_id": snap_edge.get("target", snap_edge.get("target_node_id")),
                        "edge_type": snap_edge.get("type", snap_edge.get("edge_type")),
                        "source_port": snap_edge.get("source_port", "out"),
                        "target_port": snap_edge.get("target_port", "in"),
                        "condition_expression": snap_edge.get("condition_expression"),
                        "hitl_review_config": snap_edge.get("hitl_review_config"),
                    },
                    _edge_payload_with_defaults(e_payload),
                    path=f"snapshot edge {eid}",
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
        assert env["OPENAI_API_KEY"] == _MASK

        # Persisted value is real
        async with db_engine.begin() as conn:
            # A raw AsyncConnection does not materialise ORM entities, so read
            # the JSON column directly.
            graph_nodes = (
                await conn.execute(select(Pipeline.graph_nodes_json).where(Pipeline.id == pipeline_id))
            ).scalar_one()
            sb = next(n for n in graph_nodes if n["id"] == str(_SANDBOX_ID))
            assert sb["env_vars"]["OPENAI_API_KEY"] == "plain-placeholder-not-a-real-secret"


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
        "env_vars",
        "context_files",
    ):
        if key == "connector_binding" and a.get(key) is not None and b.get(key) is not None:
            _assert_connector_binding_eq(a[key], b[key], path=f"{path}.connector_binding")
            continue
        if key == "env_vars":
            # Both sides are response payloads, so sensitive values are masked
            # identically; plain equality asserts the masked round-trip.
            assert a.get(key) == b.get(key), f"{path}.env_vars"
            continue

        if key == "context_files":
            assert a.get(key) == b.get(key), f"{path}.context_files"
            continue
        assert a.get(key) == b.get(key), f"{path}.{key}: {a.get(key)!r} != {b.get(key)!r}"


# Six-bullet sentinel from ``farna.api.middleware.sensitive_mask``
# (``SENSITIVE_VALUE_MASK``); pinned here so a response-layer mask change is caught.
_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022"


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return upper.endswith("_API_KEY") or "SECRET" in upper or "TOKEN" in upper


def _assert_node_matches_payload(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    path: str = "node",
    masked: bool = False,
) -> None:
    """Assert the fields *explicitly present in the payload* survive on ``actual``.

    Fields the payload omits are filled by model defaults on the write path and
    are therefore intentionally not compared (mirrors the unit roundtrip suite).
    ``masked=True`` expects sensitive ``env_vars`` values to be replaced by
    ``_MASK`` (an API read response); ``masked=False`` expects the real stored
    value (persisted JSON / snapshot).
    """
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if key == "env_vars":
            assert isinstance(actual_value, dict), f"{path}.env_vars"
            assert set(actual_value.keys()) == set(expected_value.keys()), f"{path}.env_vars keys"
            for k, v in expected_value.items():
                if masked and _is_sensitive_env_key(k):
                    assert actual_value[k] == _MASK, f"{path}.env_vars[{k}]"
                else:
                    assert actual_value[k] == v, f"{path}.env_vars[{k}]"
        elif key == "context_files":
            assert actual_value == expected_value, f"{path}.context_files"
        elif isinstance(expected_value, dict) and isinstance(actual_value, dict):
            for ek, ev in expected_value.items():
                assert actual_value.get(ek) == ev, f"{path}.{key}.{ek}: {actual_value.get(ek)!r} != {ev!r}"
        else:
            assert actual_value == expected_value, f"{path}.{key}: {actual_value!r} != {expected_value!r}"


def _edge_payload_with_defaults(edge_payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the wire defaults an omitted ``source_port``/``target_port`` implies."""
    return {
        **edge_payload,
        "source_port": edge_payload.get("source_port", "out"),
        "target_port": edge_payload.get("target_port", "in"),
    }


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
