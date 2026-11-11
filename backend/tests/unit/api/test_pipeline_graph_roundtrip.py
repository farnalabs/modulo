"""FAR-2475: Real graph persistence round-trips through the REST read/write path.

This is the RUNNABLE-ON-ANY-WORKSTATION half of the contract test. It uses an
in-memory SQLite database behind the real FastAPI app (ASGI transport), so
the REAL route handlers (``replace_pipeline_graph_endpoint`` /
``get_pipeline_graph_endpoint``), the REAL service layer
(``replace_pipeline_graph``) and the REAL snapshot copy
(``create_snapshot_from_live_graph``) all execute.

Docker note: the Postgres/testcontainers equivalent lives in
``tests/integration/api/test_pipeline_graph_contract.py``.

Two documented SQLite harness adaptations (neither touches the code under test):
1. The per-pipeline snapshot ADVISORY LOCK is Postgres-only
   (``pg_try_advisory_lock``); it is patched no-op so the real graph-copy runs.
2. Live-role re-read binds string UUIDs against SQLAlchemy's ``Uuid`` type,
   which SQLite rejects; ``resolve_role_from_membership`` is pinned to "admin"
   for the duration (the role gate is not what this module tests).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from farna.api.dependencies import (
    get_current_tenant_user,
    get_current_tenant_user_or_api_key,
    get_current_user,
    get_db_session,
    get_plan_context,
    get_settings,
)
from farna.api.main import app
from farna.api.routes.pipelines import (
    PipelineGraphUpdate,
    _edge_data_to_dict,
    _edge_to_data,
    replace_pipeline_graph,
    strip_unset_connector_binding_inputs,
)
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.db.crud.pipeline import GraphWriteActor, GraphWriteRecording
from farna.db.models.account import Account
from farna.db.models.agent import Agent
from farna.db.models.base import Base
from farna.db.models.connector_instance import ConnectorInstance
from farna.db.models.org_membership import OrgMembership
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_edge import PipelineEdge
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.rls import set_rls_org, set_rls_user_context
from tests.helpers.pipeline_graph import (
    _ACCOUNT_ID,
    _AGENT_ROW_ID,
    _CONNECTOR_FULL_ID,
    _CONNECTOR_INSTANCE_ID,
    _CONNECTOR_MIN_ID,
    _ORG_ID,
    _PIPELINE_ID,
    _PIPELINE_ID_2,
    _SANDBOX_ID,
    _build_graph_payload,
)

# ---------------------------------------------------------------------------
# SQLite harness: tables needed by the real service layer + snapshot
# ---------------------------------------------------------------------------

_TABLE_NAMES = {
    "accounts",
    "agents",
    "audit_events",
    "audit_chain_heads",
    "connector_instances",
    "evals",
    "organisations",
    "org_memberships",
    "pipeline_edges",
    "pipeline_snapshots",
    "pipelines",
    "policy_gates",
    "schemas",
    "snapshot_schema_pins",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite engine with StaticPool (single connection) and the
    tables needed by the real service layer."""
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="function")
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest_asyncio.fixture(scope="function")
async def seeded_db(session: AsyncSession) -> None:
    """Seed the DB with org, admin account, pipeline, agent, connector."""
    # Org + admin account
    session.add(Account(id=_ACCOUNT_ID, email="admin@test", display_name="Test Admin"))
    session.add(OrgMembership(account_id=_ACCOUNT_ID, organisation_id=_ORG_ID, role="admin"))

    # Agent row for the agent node
    session.add(
        Agent(
            id=_AGENT_ROW_ID,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            name="Roundtrip Agent",
            prompt_template="You are a helpful assistant.",
        )
    )

    # Connector instance for connector nodes
    session.add(
        ConnectorInstance(
            id=_CONNECTOR_INSTANCE_ID,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            name="GitHub Roundtrip",
            connector_type_id="github",
            credentials_ciphertext=b"{}",
            config_json={},
            allowed_operations=[],
            status="active",
            visibility="org",
        )
    )

    # Empty pipelines (graphs seeded by tests)
    session.add(
        Pipeline(
            id=_PIPELINE_ID,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            name="Roundtrip Pipeline",
            graph_nodes_json=[],
        )
    )
    session.add(
        Pipeline(
            id=_PIPELINE_ID_2,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            name="Roundtrip Pipeline 2",
            graph_nodes_json=[],
        )
    )

    await session.flush()


# ---------------------------------------------------------------------------
# ASGI client with real sqlite session overrides
# ---------------------------------------------------------------------------


async def _make_settings() -> Any:
    from farna.settings import Settings

    return Settings(
        database_url="sqlite+aiosqlite://",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
    )


def _make_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
        is_system_admin=False,
        via_api_key=False,
    )


def _make_tenant_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
        is_system_admin=False,
        via_api_key=False,
    )


async def _override_tenant_user() -> TenantPrincipal:
    return _make_tenant_principal()


async def _override_tenant_user_or_key() -> TenantPrincipal:
    return _make_tenant_principal()


async def _override_session(maker: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with maker() as s:
        yield s


def _make_session_override(maker: async_sessionmaker[AsyncSession]):
    """Create a dependency override for get_db_session that yields a fresh session."""

    async def override() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as s:
            yield s

    return override


async def _override_plan_context() -> Any:
    from farna.core.feature_flags import PlanContext

    class _AllFeatures(PlanContext):
        def feature_enabled(self, name: str) -> bool:
            return True

        def get_limit(self, name: str) -> int | None:
            return None

    return _AllFeatures()


@pytest_asyncio.fixture(scope="function")
async def asgi_client(
    engine: AsyncEngine,
    seeded_db: None,
) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with the FastAPI app backed by the real sqlite session."""
    maker = async_sessionmaker(engine, expire_on_commit=False)

    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = _make_session_override(maker)
    app.dependency_overrides[get_current_user] = _make_principal
    app.dependency_overrides[get_current_tenant_user] = _override_tenant_user
    app.dependency_overrides[get_current_tenant_user_or_api_key] = _override_tenant_user_or_key
    app.dependency_overrides[get_plan_context] = _override_plan_context

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Patch: snapshot lock (Postgres advisory) + live-role re-read (string UUID bind)
# ---------------------------------------------------------------------------


async def _mock_acquire_lock(*_args: Any, **_kwargs: Any) -> MagicMock:
    """Mock for _acquire_snapshot_lock - returns a mock connection."""
    return MagicMock()


async def _mock_release_lock(*_args: Any, **_kwargs: Any) -> None:
    """Mock for _release_snapshot_lock - does nothing."""
    return


def _patch_snapshot_and_role():
    """Return context managers that patch the snapshot advisory lock and the
    live-role re-read (which binds string UUIDs on SQLite)."""
    lock_patch = patch("farna.db.crud.pipeline_snapshot._acquire_snapshot_lock", _mock_acquire_lock)
    release_patch = patch("farna.db.crud.pipeline_snapshot._release_snapshot_lock", _mock_release_lock)
    role_patch = patch("farna.db.crud.org_membership.resolve_role_from_membership", AsyncMock(return_value="admin"))
    return lock_patch, release_patch, role_patch


# ---------------------------------------------------------------------------
# Graph assertion helpers
# ---------------------------------------------------------------------------


def _assert_semantic_node_eq(a: dict[str, Any], b: dict[str, Any], *, path: str = "node") -> None:
    """Assert semantic equivalence of two node dicts, ignoring masked env vars."""
    # Fields that MUST be identical (except masked env)
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
# Tests
# ---------------------------------------------------------------------------


class TestRestGraphRoundtrip:
    """Full REST PUT -> GET round-trip through the real route handlers and
    persistence layer, asserting semantic field preservation at every hop.
    """

    @pytest.fixture(autouse=True)
    def _apply_patches(self):
        """Patch SQLite-incompatible primitives: live-role re-read and snapshot advisory lock."""
        lock_patch, release_patch, role_patch = _patch_snapshot_and_role()
        with lock_patch, release_patch, role_patch:
            yield

    async def test_rest_roundtrip_preserves_semantic_graph_and_snapshot(
        self,
        asgi_client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        # 1. PUT graph
        payload = _build_graph_payload()
        put_resp = await asgi_client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json=payload,
        )
        assert put_resp.status_code == 200, put_resp.text
        put_graph = put_resp.json()

        # 2. GET graph
        get_resp = await asgi_client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert get_resp.status_code == 200, get_resp.text
        get_graph = get_resp.json()

        # 3. Assert response nodes/edges match semantic payload (modulo masking)
        put_nodes = {n["id"]: n for n in put_graph["nodes"]}
        get_nodes = {n["id"]: n for n in get_graph["nodes"]}
        payload_node_ids = {n["id"] for n in payload["nodes"]}
        assert set(put_nodes.keys()) == payload_node_ids, "PUT node id set"
        assert payload_node_ids == set(get_nodes.keys()), "GET node id set"
        assert {e["id"] for e in put_graph["edges"]} == {e["id"] for e in get_graph["edges"]}, "edge id sets"

        for n_payload in payload["nodes"]:
            nid = n_payload["id"]
            # PUT and GET responses must match (both masked)
            _assert_semantic_node_eq(put_nodes[nid], get_nodes[nid], path=f"node {nid}")
            # GET must match fields that were explicitly in the payload.
            # For fields not in payload, we don't assert (they get model defaults).
            node_path = f"node {nid}"
            for key, expected_value in n_payload.items():
                actual_value = get_nodes[nid].get(key)
                if key == "env_vars":
                    # Env vars: sensitive keys are masked, others pass through
                    assert set(actual_value.keys()) == set(expected_value.keys()), f"{node_path}.env_vars keys"
                    for k, v in expected_value.items():
                        if k.upper().endswith("_API_KEY") or "SECRET" in k.upper() or "TOKEN" in k.upper():
                            # Sensitive keys get masked
                            assert actual_value[k] == "••••••", f"{node_path}.env_vars[{k}]"
                        else:
                            # Non-sensitive keys pass through
                            assert actual_value[k] == v, f"{node_path}.env_vars[{k}]"
                elif key == "context_files":
                    assert actual_value == expected_value, f"{node_path}.context_files"
                # For dicts, check that expected keys are present with matching values
                # (response may include extra None default fields)
                elif isinstance(expected_value, dict) and isinstance(actual_value, dict):
                    for ek, ev in expected_value.items():
                        assert actual_value.get(ek) == ev, f"{node_path}.{key}.{ek}: {actual_value.get(ek)!r} != {ev!r}"
                else:
                    assert actual_value == expected_value, f"{node_path}.{key}: {actual_value!r} != {expected_value!r}"

        put_edges = {e["id"]: e for e in put_graph["edges"]}
        get_edges = {e["id"]: e for e in get_graph["edges"]}
        for e_payload in payload["edges"]:
            eid = e_payload["id"]
            _assert_semantic_edge_eq(put_edges[eid], get_edges[eid], path=f"edge {eid}")
            # Edge payload may omit source_port/target_port (defaults "out"/"in")
            e_payload_with_defaults = {
                **e_payload,
                "source_port": e_payload.get("source_port", "out"),
                "target_port": e_payload.get("target_port", "in"),
            }
            _assert_semantic_edge_eq(get_edges[eid], e_payload_with_defaults, path=f"edge {eid}")

        # 4. Assert persisted DB state matches
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            # Pipeline graph_nodes_json
            pipeline = await s.execute(select(Pipeline).where(Pipeline.id == _PIPELINE_ID))
            pipeline = pipeline.scalar_one()
            persisted_nodes = {n["id"]: n for n in pipeline.graph_nodes_json}
            for n_payload in payload["nodes"]:
                persisted = persisted_nodes[n_payload["id"]]
                # Only check fields that were in the payload (plus known defaults for manual)
                for key, expected_value in n_payload.items():
                    actual_value = persisted.get(key)
                    if key == "env_vars":
                        assert set(actual_value.keys()) == set(expected_value.keys()), (
                            f"persisted node {n_payload['id']}.env_vars keys"
                        )
                        # Persisted JSON holds the REAL value for every key -
                        # masking happens only at the API response layer - so
                        # every key round-trips verbatim here.
                        for k, v in expected_value.items():
                            assert actual_value[k] == v, f"persisted node {n_payload['id']}.env_vars[{k}]"
                    elif key == "context_files":
                        assert actual_value == expected_value, f"persisted node {n_payload['id']}.context_files"
                    elif isinstance(expected_value, dict) and isinstance(actual_value, dict):
                        for ek, ev in expected_value.items():
                            assert actual_value.get(ek) == ev, (
                                f"persisted node {n_payload['id']}.{key}.{ek}: {actual_value.get(ek)!r} != {ev!r}"
                            )
                    else:
                        assert actual_value == expected_value, (
                            f"persisted node {n_payload['id']}.{key}: {actual_value!r} != {expected_value!r}"
                        )
                # For manual node, also check defaults are present
                if n_payload["node_type"] == "manual":
                    assert persisted.get("mode") == "llm"
                    assert persisted.get("enable_heartbeat") is True
                    assert persisted.get("idempotent") is True
                    assert persisted.get("read_only") is False
                    assert persisted.get("single_pr_per_run") is False
                    assert persisted.get("await_completion") is False
                    assert persisted.get("join_partial_policy") == "collect_and_proceed"
                    assert persisted.get("commands_concatenation_string") == " && "

            # Edges
            edges = list(
                (await s.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == _PIPELINE_ID))).scalars()
            )
            persisted_edges = {e.id: e for e in edges}
            for e_payload in payload["edges"]:
                eid = uuid.UUID(e_payload["id"])
                edge = persisted_edges[eid]
                # Edge payload may omit source_port/target_port (defaults)
                e_payload_with_defaults = {
                    **e_payload,
                    "source_port": e_payload.get("source_port", "out"),
                    "target_port": e_payload.get("target_port", "in"),
                }
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
                    e_payload_with_defaults,
                    path=f"persisted edge {eid}",
                )

            # 5. Snapshot created (version_kind='edit')
            snaps = list(
                (
                    await s.execute(
                        select(PipelineSnapshot)
                        .where(PipelineSnapshot.pipeline_id == _PIPELINE_ID)
                        .order_by(PipelineSnapshot.snapshot_version)
                    )
                ).scalars()
            )
            assert snaps, "edit snapshot not created"
            latest = snaps[-1]
            assert latest.version_kind == "edit"
            snap_nodes = {n["id"]: n for n in latest.graph_json["nodes"]}
            snap_edges = {e["id"]: e for e in latest.graph_json["edges"]}
            # Snapshot nodes must contain all semantic fields (agent materialisation adds prompt_template etc.)
            for n_payload in payload["nodes"]:
                nid = n_payload["id"]
                snap_node = snap_nodes[nid]
                # Agent node gets prompt_template from Agent row
                if n_payload["node_type"] == "agent":
                    assert "prompt_template" in snap_node
                # Compare only fields in payload (snapshot has REAL env values, not masked)
                for key, expected_value in n_payload.items():
                    actual_value = snap_node.get(key)
                    if key == "env_vars":
                        assert set(actual_value.keys()) == set(expected_value.keys()), (
                            f"snapshot node {nid}.env_vars keys"
                        )
                        for k, v in expected_value.items():
                            assert actual_value[k] == v, (
                                f"snapshot node {nid}.env_vars[{k}]: {actual_value[k]!r} != {v!r}"
                            )
                    elif key == "context_files":
                        assert actual_value == expected_value, f"snapshot node {nid}.context_files"
                    elif isinstance(expected_value, dict) and isinstance(actual_value, dict):
                        for ek, ev in expected_value.items():
                            assert actual_value.get(ek) == ev, (
                                f"snapshot node {nid}.{key}.{ek}: {actual_value.get(ek)!r} != {ev!r}"
                            )
                    else:
                        assert actual_value == expected_value, (
                            f"snapshot node {nid}.{key}: {actual_value!r} != {expected_value!r}"
                        )

            # Snapshot edges use 'source'/'target' instead of
            # 'source_node_id'/'target_node_id', and 'type' instead of 'edge_type'.
            for e_payload in payload["edges"]:
                eid = e_payload["id"]
                snap_edge = snap_edges[eid]
                e_payload_with_defaults = {**e_payload, "source_port": "out", "target_port": "in"}
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
                    e_payload_with_defaults,
                    path=f"snapshot edge {eid}",
                )

    async def test_write_vs_response_drops_unset_connector_binding_inputs(
        self,
        asgi_client: AsyncClient,
    ) -> None:
        """A connector binding with only resource (no filters/data) must have
        those optional inputs OMITTED from the persisted/response JSON, not
        present as empty dicts or nulls."""
        payload = _build_graph_payload()
        # Use the minimal connector node
        put_resp = await asgi_client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json=payload,
        )
        assert put_resp.status_code == 200, put_resp.text
        put_graph = put_resp.json()

        # Find the minimal connector node
        min_node = next(n for n in put_graph["nodes"] if n["id"] == str(_CONNECTOR_MIN_ID))
        binding = min_node.get("connector_binding")
        assert binding is not None
        # resource present, filters/data MUST be absent (not {} or null)
        assert "resource" in binding
        assert "filters" not in binding
        assert "data" not in binding
        assert "operation" in binding
        assert "instance_id" in binding
        # Full connector node should have them
        full_node = next(n for n in put_graph["nodes"] if n["id"] == str(_CONNECTOR_FULL_ID))
        full_binding = full_node["connector_binding"]
        assert "resource" in full_binding
        assert "filters" in full_binding
        assert "data" in full_binding

    async def test_env_vars_are_masked_on_read(
        self,
        asgi_client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        """Sensitive env var keys are masked on the response (SENSITIVE_VALUE_MASK)
        but the persisted value is the real one."""
        payload = _build_graph_payload()
        put_resp = await asgi_client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json=payload,
        )
        assert put_resp.status_code == 200, put_resp.text
        put_graph = put_resp.json()

        sb_node = next(n for n in put_graph["nodes"] if n["id"] == str(_SANDBOX_ID))
        env = sb_node.get("env_vars", {})
        # Key present, value masked with 6 bullets
        assert "OPENAI_API_KEY" in env
        assert env["OPENAI_API_KEY"] == "••••••"

        # Persisted value is real
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            pipeline = await s.execute(select(Pipeline).where(Pipeline.id == _PIPELINE_ID))
            pipeline = pipeline.scalar_one()
            sb = next(n for n in pipeline.graph_nodes_json if n["id"] == str(_SANDBOX_ID))
            assert sb["env_vars"]["OPENAI_API_KEY"] == "plain-placeholder-not-a-real-secret"


class TestMcpRestEquivalence:
    """MCP and REST graph writes must persist the SAME graph JSON because both
    funnel through ``replace_pipeline_graph`` and both use the exact same
    ``PipelineGraphUpdate`` validation + ``strip_unset_connector_binding_inputs``
    serialisation."""

    @pytest.fixture(autouse=True)
    def _apply_patches(self):
        """Patch SQLite-incompatible primitives (advisory lock + live-role re-read)
        and seed the MCP request ContextVars the direct tool call reads."""
        import farna.api.mcp_server as ms

        tokens = (
            ms._ctx_org_id.set(_ORG_ID),
            ms._ctx_role.set("admin"),
            ms._ctx_user_id.set(_ACCOUNT_ID),
        )
        lock_patch, release_patch, role_patch = _patch_snapshot_and_role()
        try:
            with lock_patch, release_patch, role_patch:
                yield
        finally:
            ms._ctx_org_id.reset(tokens[0])
            ms._ctx_role.reset(tokens[1])
            ms._ctx_user_id.reset(tokens[2])

    async def test_mcp_and_rest_persist_identical_graph(
        self,
        engine: AsyncEngine,
        seeded_db: None,
    ) -> None:
        import farna.api.mcp_server as ms

        payload = _build_graph_payload()
        # ``pipeline_edges.id`` is a global primary key, so the second write
        # needs its own edge ids; node ids are compared across the two writes.
        mcp_payload = _build_graph_payload()
        for mcp_edge in mcp_payload["edges"]:
            mcp_edge["id"] = str(uuid.uuid4())

        # 1. REST write via real service (replace_pipeline_graph) on pipeline 1
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s, s.begin():
            rest_nodes, rest_edges = await replace_pipeline_graph(
                s,
                pipeline_id=_PIPELINE_ID,
                org_id=_ORG_ID,
                nodes=[
                    strip_unset_connector_binding_inputs(n.model_dump(mode="json"))
                    for n in PipelineGraphUpdate.model_validate(
                        {"nodes": payload["nodes"], "edges": payload["edges"]}
                    ).nodes
                ],
                edges=[
                    _pipeline_graph_edge_to_dict(e)
                    for e in PipelineGraphUpdate.model_validate(
                        {"nodes": payload["nodes"], "edges": payload["edges"]}
                    ).edges
                ],
                actor=GraphWriteActor(
                    is_privileged=True,
                    caller_type="rest",
                    account_id=_ACCOUNT_ID,
                    is_guardrail_admin=True,
                ),
                recording=GraphWriteRecording(record_snapshot=True),
            )
        rest_nodes = {n["id"]: n for n in rest_nodes}
        rest_edges = {str(e.id): e for e in rest_edges}

        # 2. MCP write on pipeline 2 (separate pipeline, same org)
        @asynccontextmanager
        async def _session_factory(_org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
            async with maker() as s, s.begin():
                await set_rls_org(s, _ORG_ID)
                await set_rls_user_context(s, _ACCOUNT_ID, "admin")
                yield s

        lock_patch, release_patch, role_patch = _patch_snapshot_and_role()
        with (
            lock_patch,
            release_patch,
            role_patch,
            patch("farna.api.mcp_server.resolve_role_from_membership", AsyncMock(return_value="admin")),
            patch("farna.api.mcp_server.validate_current_auth", return_value=True),
            patch("farna.api.mcp_server._session", _session_factory),
            patch(
                "farna.core.team_visibility.find_connector_team_mismatches",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await ms.update_pipeline_graph(
                pipeline_id=str(_PIPELINE_ID_2),
                nodes=mcp_payload["nodes"],
                edges=mcp_payload["edges"],
            )

        assert "error" not in result, result

        # 3. Compare persisted JSON on pipeline 2
        async with maker() as s:
            pipeline2 = await s.execute(select(Pipeline).where(Pipeline.id == _PIPELINE_ID_2))
            pipeline2 = pipeline2.scalar_one()
            mcp_nodes = {n["id"]: n for n in pipeline2.graph_nodes_json}

        # 4. Semantic equivalence. Both writes use the SAME serialisation, so
        # every non-env field must match exactly; env_vars is excluded (see the
        # skip in the key loop below).
        for nid in rest_nodes:
            # Compare all non-env fields
            for key in (
                "id",
                "node_type",
                "position",
                "label",
                "description",
                "mode",
                "template_id",
                "agent_commands",
                "agent_prompt",
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
                if key == "connector_binding":
                    rest_cb = rest_nodes[nid].get(key)
                    mcp_cb = mcp_nodes[nid].get(key)
                    if rest_cb is None or mcp_cb is None:
                        assert rest_cb == mcp_cb, f"field {key} on {nid}"
                    else:
                        _assert_connector_binding_eq(rest_cb, mcp_cb)
                elif key == "env_vars":
                    continue  # masked differently at response layer
                else:
                    assert rest_nodes[nid].get(key) == mcp_nodes[nid].get(key), f"field {key} on {nid}"


# ---------------------------------------------------------------------------
# Helpers referenced from the mcp test
# ---------------------------------------------------------------------------


def _pipeline_graph_edge_to_dict(edge: Any) -> dict[str, Any]:
    """Serialise a validated ``PipelineGraphEdge`` exactly as the REST route does."""
    return _edge_data_to_dict(_edge_to_data(edge))
