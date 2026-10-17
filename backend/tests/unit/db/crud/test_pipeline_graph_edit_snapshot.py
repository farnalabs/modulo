"""FAR-1810: an explicit pipeline graph save records an immutable edit snapshot.

Before this change ``PATCH /{id}/graph`` (and every other graph write routed
through ``replace_pipeline_graph``) wrote the live graph and recorded NOTHING —
snapshots were created only at run start, by rollback, or by the (unwired)
``POST /{id}/snapshots`` endpoint. The pipeline version history was therefore
~100% run snapshots and ``snapshot_version`` was effectively a run counter.

These tests run the REAL ``replace_pipeline_graph`` CRUD function against a
real (in-memory SQLite) session and assert the edit-snapshot hook — that it
fires on an explicit save, is suppressed on a no-op save, fails closed on a
snapshot error, and stays off for scaffolding callers — by asserting on the
``create_snapshot_edit`` call itself. They deliberately do NOT mock the CRUD
function: an event-shaped mock call is not evidence a snapshot is written, and
the hook's decision logic (no-op comparison, fail-closed raise) only exists in
the real function. They fail when the hook is removed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.routes.pipelines import _sync_agent_row_commands
from modulo.db.crud.pipeline import GraphWriteActor, GraphWriteRecording, replace_pipeline_graph
from modulo.db.crud.pipeline_snapshot import _materialize_agent_fields
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge

# Scoped create_all: unrelated models use Postgres-only column types that
# SQLite cannot render (same rationale as test_pipeline_graph_updated_audit).
_TABLE_NAMES = {
    "accounts",
    "agents",
    "org_memberships",
    "pipelines",
    "pipeline_edges",
    "pipeline_snapshots",
    "audit_events",
    "audit_chain_heads",
}

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")

_NODE_A = "00000000-0000-0000-0000-0000000000a1"
_NODE_B = "00000000-0000-0000-0000-0000000000a2"

_EDGE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


def _node(node_id: str, *, agent_commands: list[str], agent_id: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "node_type": "agent",
        "position": {"x": 0, "y": 0},
        "label": node_id[-2:],
        "agent_commands": list(agent_commands),
    }
    if agent_id is not None:
        node["agent_id"] = agent_id
    return node


def _edge(edge_id: str, source: str, target: str) -> dict[str, Any]:
    return {
        "id": edge_id,
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": "normal",
    }


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_graph(session: AsyncSession, nodes: list[dict[str, Any]], *, with_edge: bool) -> None:
    session.add(Account(id=_ACCOUNT_ID, email="actor@example.com", display_name="Snapshot Actor"))
    session.add(OrgMembership(account_id=_ACCOUNT_ID, organisation_id=_ORG_ID, role="admin"))
    session.add(
        Pipeline(
            id=_PIPELINE_ID,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            name="Edit Snapshot Pipeline",
            graph_nodes_json=nodes,
        )
    )
    if with_edge:
        session.add(
            PipelineEdge(
                id=_EDGE_ID,
                organisation_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                source_node_id=uuid.UUID(_NODE_A),
                target_node_id=uuid.UUID(_NODE_B),
                edge_type="normal",
            )
        )
    await session.flush()


def _mock_snapshot(version: int = 1) -> Any:
    snap = AsyncMock()
    snap.snapshot_version = version
    return snap


async def _patch_graph(
    session: AsyncSession,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    record_snapshot: bool = True,
) -> Any:
    """Drive the REAL ``replace_pipeline_graph`` (shared by the REST
    ``PUT /{id}/graph`` route and the MCP ``update_pipeline_graph`` tool).

    The MCP call shape is used (real actor id, ``GraphWriteActor`` carrying
    ``is_privileged=True`` / ``is_guardrail_admin=True``) so the
    guardrail-strip / live-role paths short-circuit exactly as the audit tests
    do on SQLite.
    """
    return await replace_pipeline_graph(
        session,
        pipeline_id=_PIPELINE_ID,
        org_id=_ORG_ID,
        nodes=nodes,
        edges=edges,
        actor=GraphWriteActor(
            is_privileged=True,
            caller_type="mcp",
            account_id=_ACCOUNT_ID,
            is_guardrail_admin=True,
        ),
        recording=GraphWriteRecording(record_snapshot=record_snapshot),
    )


class TestExplicitSaveRecordsEditSnapshot:
    async def test_changed_graph_save_creates_edit_snapshot(self, session: AsyncSession) -> None:
        """An explicit save whose graph CHANGED records one edit snapshot.

        This is the load-bearing assertion: without the FAR-1810 hook
        ``create_snapshot_edit`` is never called by ``replace_pipeline_graph``
        regardless of the flag, so this test fails on the pre-change code.
        """
        await _seed_graph(session, [_node(_NODE_A, agent_commands=["run --v1"])], with_edge=False)
        new_nodes = [_node(_NODE_A, agent_commands=["run --v2"])]

        with patch(
            "modulo.db.crud.pipeline.create_snapshot_edit",
            new=AsyncMock(return_value=_mock_snapshot()),
        ) as snap_mock:
            result = await _patch_graph(session, new_nodes, [])

        assert result is not None, "the graph write must succeed"
        snap_mock.assert_awaited_once()
        assert snap_mock.await_args.kwargs["pipeline_id"] == _PIPELINE_ID
        assert snap_mock.await_args.kwargs["account_id"] == _ACCOUNT_ID

    async def test_snapshot_created_after_write_is_in_same_transaction(self, session: AsyncSession) -> None:
        """The snapshot is created inside the CRUD call, before it returns — so
        the caller's enclosing transaction commits graph + snapshot together."""
        await _seed_graph(session, [_node(_NODE_A, agent_commands=["v1"])], with_edge=False)

        # ``create_snapshot_edit`` returns None -> the hook must RAISE (a save
        # that does not record history is a broken save). Asserting the raise
        # proves the hook runs INSIDE the function, not after it returns.
        with (
            patch(
                "modulo.db.crud.pipeline.create_snapshot_edit",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(RuntimeError, match="could not record its edit snapshot"),
        ):
            await _patch_graph(session, [_node(_NODE_A, agent_commands=["v2"])], [])


class TestEditSnapshotMaterializesPostSyncAgentRow:
    """FAR-1810 review (MAJOR): the edit snapshot's bound-node payload must be
    materialized from the Agent row AFTER the FAR-488a command sync, so the
    snapshot recorded for the version just saved carries the command the
    operator saved — not the pre-sync Agent-row value.

    ``create_snapshot_from_live_graph`` -> ``_materialize_agent_fields``
    OVERWRITES a bound node's ``agent_commands`` with the Agent-row value at
    snapshot time (``pipeline_snapshot.py`` ``_apply_agent_fields``). The routes
    therefore run ``_sync_agent_row_commands`` BEFORE the write that mints the
    snapshot; this test drives that exact order against a real session and
    captures the payload the snapshot copy would materialize. Under the pre-fix
    order (sync AFTER the write) the captured node carried the stale Agent-row
    command, so this fails when the ordering regresses.
    """

    async def test_edit_snapshot_payload_pins_the_post_sync_agent_command(self, session: AsyncSession) -> None:
        agent_id = uuid.UUID("00000000-0000-0000-0000-0000000000a7")
        session.add(
            Agent(
                id=agent_id,
                organisation_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                name="bound-agent",
                prompt_template="p",
                agent_commands=["run --old"],
            )
        )
        # The live node mirrors the pre-save Agent row, so saving --new is a real
        # change and the snapshot is NOT suppressed.
        await _seed_graph(
            session,
            [_node(_NODE_A, agent_commands=["run --old"], agent_id=str(agent_id))],
            with_edge=False,
        )

        saved = [_node(_NODE_A, agent_commands=["run --new"], agent_id=str(agent_id))]
        captured: dict[str, Any] = {}

        async def _capture(session_arg: AsyncSession, *, pipeline_id: uuid.UUID, **_kwargs: Any) -> Any:
            pipeline = (await session_arg.execute(select(Pipeline).where(Pipeline.id == pipeline_id))).scalar_one()
            nodes = [dict(n) for n in (pipeline.graph_nodes_json or [])]
            await _materialize_agent_fields(session_arg, nodes)
            captured["nodes"] = nodes
            return _mock_snapshot()

        with patch("modulo.db.crud.pipeline.create_snapshot_edit", new=_capture):
            # Route order: FAR-488a sync FIRST, then the write mints the snapshot.
            await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=saved)
            await _patch_graph(session, saved, [])

        assert captured["nodes"][0]["agent_commands"] == ["run --new"]


class TestNoOpSuppression:
    async def test_identical_graph_save_suppresses_edit_snapshot(self, session: AsyncSession) -> None:
        """A save that changes nothing records no snapshot (volume cap)."""
        nodes = [_node(_NODE_A, agent_commands=["run"])]
        await _seed_graph(session, nodes, with_edge=False)

        with patch(
            "modulo.db.crud.pipeline.create_snapshot_edit",
            new=AsyncMock(return_value=_mock_snapshot()),
        ) as snap_mock:
            result = await _patch_graph(session, [dict(n) for n in nodes], [])

        assert result is not None
        snap_mock.assert_not_awaited()

    async def test_edge_id_regeneration_alone_suppresses_edit_snapshot(self, session: AsyncSession) -> None:
        """Edge ``id`` is not stable identity (``PipelineGraphEdge`` regenerates
        a UUID per save when the client omits it), so the no-op comparison
        excludes it: identical edge content with a NEW id still suppresses."""
        await _seed_graph(
            session,
            [_node(_NODE_A, agent_commands=["run"]), _node(_NODE_B, agent_commands=["keep"])],
            with_edge=True,
        )
        same_content_new_id = [
            _edge(str(uuid.uuid4()), _NODE_A, _NODE_B),
        ]

        with patch(
            "modulo.db.crud.pipeline.create_snapshot_edit",
            new=AsyncMock(return_value=_mock_snapshot()),
        ) as snap_mock:
            result = await _patch_graph(
                session,
                [_node(_NODE_A, agent_commands=["run"]), _node(_NODE_B, agent_commands=["keep"])],
                same_content_new_id,
            )

        assert result is not None
        snap_mock.assert_not_awaited()

    async def test_edge_content_change_is_not_suppressed(self, session: AsyncSession) -> None:
        """A real edge change (new edge) records a snapshot — the suppression is
        content-based, not a blanket skip."""
        await _seed_graph(
            session,
            [_node(_NODE_A, agent_commands=["run"]), _node(_NODE_B, agent_commands=["keep"])],
            with_edge=True,
        )
        added = [
            _edge(str(_EDGE_ID), _NODE_A, _NODE_B),
            _edge(str(uuid.uuid4()), _NODE_B, _NODE_A),
        ]

        with patch(
            "modulo.db.crud.pipeline.create_snapshot_edit",
            new=AsyncMock(return_value=_mock_snapshot()),
        ) as snap_mock:
            result = await _patch_graph(
                session,
                [_node(_NODE_A, agent_commands=["run"]), _node(_NODE_B, agent_commands=["keep"])],
                added,
            )

        assert result is not None
        snap_mock.assert_awaited_once()


class TestScaffoldingDefaultStaysOff:
    async def test_default_flag_does_not_create_edit_snapshot(self, session: AsyncSession) -> None:
        """Scaffolding writers (onboarding, demo-seed) omit the flag and must not
        push a genesis graph into the primary edit history."""
        await _seed_graph(session, [_node(_NODE_A, agent_commands=["run --v1"])], with_edge=False)

        with patch(
            "modulo.db.crud.pipeline.create_snapshot_edit",
            new=AsyncMock(return_value=_mock_snapshot()),
        ) as snap_mock:
            result = await _patch_graph(
                session,
                [_node(_NODE_A, agent_commands=["run --v2"])],
                [],
                record_snapshot=False,
            )

        assert result is not None
        snap_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fail closed: a snapshot error rolls the whole explicit save back.
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_snapshot_error_propagates_and_rolls_back_the_save(self, session: AsyncSession) -> None:
        """A snapshot failure must surface (not be swallowed) so the enclosing
        transaction rolls the graph write back with it."""
        await _seed_graph(session, [_node(_NODE_A, agent_commands=["run --v1"])], with_edge=False)

        with (
            patch(
                "modulo.db.crud.pipeline.create_snapshot_edit",
                new=AsyncMock(side_effect=RuntimeError("snapshot allocation exploded")),
            ),
            pytest.raises(RuntimeError, match="snapshot allocation exploded"),
        ):
            await _patch_graph(session, [_node(_NODE_A, agent_commands=["run --v2"])], [])


# ---------------------------------------------------------------------------
# Endpoint / tool wiring: the explicit-save surfaces pass the flag.
# ---------------------------------------------------------------------------


_MCP_CTX_NAMES = (
    "_ctx_org_id",
    "_ctx_user_id",
    "_ctx_role",
    "_ctx_team_id",
    "_ctx_auth_type",
    "_ctx_key_scope",
)


class TestSaveSurfaceWiring:
    """The REST graph endpoints and the MCP tool are the explicit-save surfaces;
    each must pass ``recording=GraphWriteRecording(record_snapshot=True)`` into
    the CRUD write."""

    async def test_mcp_update_pipeline_graph_records_edit_snapshot(self, session: AsyncSession) -> None:
        """Drive the REAL MCP ``update_pipeline_graph`` tool through to the REAL
        CRUD (same pattern as the audit tests) and assert the hook fires — a
        call-arg assertion against a mocked CRUD would prove only the wiring."""
        import modulo.api.mcp_server as ms

        agent_id = uuid.UUID("00000000-0000-0000-0000-0000000000a9")

        def _mcp_node(node_id: str, *, label: str) -> dict[str, Any]:
            # The MCP tool validates nodes with the API model, which requires an
            # ``agent`` node to carry an ``agent_id`` (unlike the pure-CRUD tests
            # above, which bypass that layer).
            return {
                "id": node_id,
                "node_type": "agent",
                "agent_id": str(agent_id),
                "position": {"x": 0, "y": 0},
                "label": label,
            }

        await _seed_graph(session, [_mcp_node(_NODE_A, label="a")], with_edge=False)

        @asynccontextmanager
        async def _session_factory(_org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
            yield session

        previous = {name: getattr(ms, name).get(None) for name in _MCP_CTX_NAMES}
        ms._ctx_org_id.set(_ORG_ID)
        ms._ctx_user_id.set(_ACCOUNT_ID)
        ms._ctx_role.set("admin")
        ms._ctx_team_id.set(None)
        ms._ctx_auth_type.set("api_key")
        ms._ctx_key_scope.set("org")
        try:
            with (
                patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
                patch("modulo.api.mcp_server._session", _session_factory),
                patch(
                    "modulo.core.team_visibility.find_connector_team_mismatches",
                    new=AsyncMock(return_value=[]),
                ),
                patch(
                    "modulo.db.crud.pipeline.create_snapshot_edit",
                    new=AsyncMock(return_value=_mock_snapshot()),
                ) as snap_mock,
            ):
                result = await ms.update_pipeline_graph(
                    pipeline_id=str(_PIPELINE_ID),
                    nodes=[_mcp_node(_NODE_A, label="a - edited")],
                    edges=[],
                )
        finally:
            for name, value in previous.items():
                getattr(ms, name).set(value)

        assert "error" not in result, result
        snap_mock.assert_awaited_once()
        assert snap_mock.await_args.kwargs["pipeline_id"] == _PIPELINE_ID

    async def test_mcp_update_pipeline_graph_pins_post_sync_agent_command(self, session: AsyncSession) -> None:
        """FAR-1810 review (MAJOR): the MCP ``update_pipeline_graph`` surface must
        run the FAR-488a agent-command sync BEFORE the write mints the edit
        snapshot.

        The MCP tool is an explicit-save surface
        (``recording=GraphWriteRecording(record_snapshot=True)``), but the sync
        used to live only in the REST routes. The snapshot copy's
        ``_materialize_agent_fields`` OVERWRITES a bound node's ``agent_commands``
        with the Agent-row value, which was still the PRE-sync value — so a save
        that changed a bound node's commands recorded a snapshot disagreeing with
        the live graph (live ``run --new``, snapshot ``run --old``), and a rollback
        replaying it would restore the stale command. Driving the REAL MCP tool
        through to the REAL CRUD (no mocked write) and capturing the materialized
        snapshot payload, this fails when the MCP sync is removed.
        """
        import modulo.api.mcp_server as ms

        agent_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        session.add(
            Agent(
                id=agent_id,
                organisation_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                name="mcp-bound-agent",
                prompt_template="p",
                agent_commands=["run --old"],
            )
        )

        def _sandbox_node(*, commands: list[str]) -> dict[str, Any]:
            # A sandbox_agent node is the only type the API model lets carry a
            # node-level ``agent_commands``; it is also agent-bindable, which is
            # exactly the shape ``_materialize_agent_fields`` overwrites.
            return {
                "id": _NODE_A,
                "node_type": "sandbox_agent",
                "agent_id": str(agent_id),
                "position": {"x": 0, "y": 0},
                "label": "a",
                "template_id": "opencode",
                "agent_prompt": "do the thing",
                "agent_commands": commands,
            }

        await _seed_graph(session, [_sandbox_node(commands=["run --old"])], with_edge=False)

        @asynccontextmanager
        async def _session_factory(_org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
            yield session

        captured: dict[str, Any] = {}

        async def _capture(session_arg: AsyncSession, *, pipeline_id: uuid.UUID, **_kwargs: Any) -> Any:
            pipeline = (await session_arg.execute(select(Pipeline).where(Pipeline.id == pipeline_id))).scalar_one()
            nodes = [dict(n) for n in (pipeline.graph_nodes_json or [])]
            await _materialize_agent_fields(session_arg, nodes)
            captured["nodes"] = nodes
            return _mock_snapshot()

        previous = {name: getattr(ms, name).get(None) for name in _MCP_CTX_NAMES}
        ms._ctx_org_id.set(_ORG_ID)
        ms._ctx_user_id.set(_ACCOUNT_ID)
        ms._ctx_role.set("admin")
        ms._ctx_team_id.set(None)
        ms._ctx_auth_type.set("api_key")
        ms._ctx_key_scope.set("org")
        try:
            with (
                patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
                patch("modulo.api.mcp_server._session", _session_factory),
                patch(
                    "modulo.core.team_visibility.find_connector_team_mismatches",
                    new=AsyncMock(return_value=[]),
                ),
                patch("modulo.db.crud.pipeline.create_snapshot_edit", new=_capture),
            ):
                result = await ms.update_pipeline_graph(
                    pipeline_id=str(_PIPELINE_ID),
                    nodes=[_sandbox_node(commands=["run --new"])],
                    edges=[],
                )
        finally:
            for name, value in previous.items():
                getattr(ms, name).set(value)

        assert "error" not in result, result
        # The edit snapshot materializes the POST-sync Agent-row command...
        assert captured["nodes"][0]["agent_commands"] == ["run --new"]
        # ...and it agrees with the live graph the MCP write persisted.
        live = (await session.execute(select(Pipeline).where(Pipeline.id == _PIPELINE_ID))).scalar_one()
        assert live.graph_nodes_json[0]["agent_commands"] == ["run --new"]


# ---------------------------------------------------------------------------
# REST wiring: PUT /{id}/graph is the primary explicit-save surface and must
# pass recording=GraphWriteRecording(record_snapshot=True) into the CRUD write
# (mocked at the route layer to assert the call argument — the CRUD behaviour
# itself is covered above).
# ---------------------------------------------------------------------------

_VALID_32 = "a" * 32


def _make_settings() -> Any:
    from modulo.settings import Settings

    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def rest_client() -> Any:
    from fastapi.testclient import TestClient

    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from tests.unit.api.mock_session import configure_mock_session

    mock_session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncMock:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)  # type: ignore[misc]
    app.dependency_overrides.clear()


def test_put_graph_endpoint_records_edit_snapshot(rest_client: Any) -> None:
    """PUT /{id}/graph must call the CRUD write with
    ``recording=GraphWriteRecording(record_snapshot=True)`` — the request that
    makes an explicit save enter the primary version history. Without it the
    endpoint's save would record nothing (the pre-FAR-1810 bug) and this
    assertion fails."""
    # The REST model requires a sandbox_agent node to carry template/prompt
    # fields (an ``agent`` node instead needs an agent_id), so this wiring test
    # uses the sandbox_agent shape the API accepts.
    nodes = [
        {
            "id": str(_NODE_A),
            "node_type": "sandbox_agent",
            "position": {"x": 0, "y": 0},
            "connector_binding": None,
            "template_id": "opencode",
            "agent_prompt": "do the thing",
            "agent_commands": ["opencode run"],
        }
    ]
    validation = MagicMock()
    validation.issues = []

    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, [])) as crud_mock,
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(return_value=0)),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=validation),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=MagicMock(owner_team_id=None)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = rest_client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200, resp.text
    crud_mock.assert_awaited_once()
    recording = crud_mock.await_args.kwargs.get("recording")
    assert recording is not None, "PUT /{id}/graph must declare how the write is recorded"
    assert recording.record_snapshot is True
