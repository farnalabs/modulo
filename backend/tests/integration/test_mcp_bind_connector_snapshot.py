"""FAR-1810: the MCP connector bind records a real, restorable edit snapshot.

The pre-fix capture was ``previous_nodes = list(pipeline.graph_nodes_json)`` —
a SHALLOW copy — while ``_apply_node_connector_binding`` builds its working list
the same shallow way and then mutates each target node dict IN PLACE
(``target["connector_binding"] = binding``). The elements of the two lists were
therefore the SAME dict objects, so the no-op comparison compared the mutated
graph against **itself**, always read "unchanged", and suppressed EVERY bind's
snapshot: a first bind and a rebind that genuinely changed the operation or
instance alike recorded no row, no log and no error. The fail-closed
``snapshot is None -> RuntimeError`` guard underneath was unreachable on this
surface, and ``rollback_to_snapshot`` could not restore a bind.

These tests drive the REAL ``bind_connector_to_node`` tool end-to-end against
real Postgres with the REAL ``create_snapshot_edit`` — nothing on the snapshot
path is mocked, because the whole point is that a row appears — and assert:

1. a first bind creates a ``version_kind='edit'`` snapshot whose
   ``snapshot_version`` increments 0 -> 1, attributed to the caller;
2. a genuinely identical re-bind IS suppressed (still exactly one row);
3. the pre/post payloads the comparison sees are NOT the same objects (the
   deep copy), and the captured pre-state really predates the mutation.

Without the deep copy, (1) never happens (suppressed), (3) fails on identity,
and (2) passes vacuously — so the suite discriminates the fix.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api import mcp_server as ms
from modulo.db.crud.pipeline import _graph_content_unchanged
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_MANUAL = "manual_approval"
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def _node() -> dict[str, Any]:
    """A ``connector`` node: the shape ``_apply_node_connector_binding`` routes
    to a connector (so the bind is a genuine graph mutation, not a no-op)."""
    return {
        "id": str(_NODE_ID),
        "node_type": "connector",
        "position": {"x": 0, "y": 0},
        "label": "bind-target",
    }


def _as_json(value: Any) -> Any:
    """Normalise a raw ``jsonb`` read (asyncpg may hand back text) to objects."""
    if isinstance(value, (bytes, memoryview, str)):
        return json.loads(value)
    return value


async def _insert_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> uuid.UUID:
    """Minimal committed pipelines row carrying the bind target node."""
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, CAST(:graph AS jsonb), :default)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "graph": json.dumps([_node()]),
                "default": _MANUAL,
                "name": f"mcp-bind-snapshot-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _insert_connector(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> uuid.UUID:
    """Minimal committed connector_instances row (org visibility, so the bind's
    team-mismatch gate short-circuits)."""
    connector_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO connector_instances (id, organisation_id, name, connector_type_id, "
                "account_id, credentials_ciphertext, config_json, allowed_operations) "
                "VALUES (:id, :oid, :name, 'github', :aid, :cred, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(connector_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "cred": b"not-a-real-secret",
                "name": f"bind-test-{connector_id.hex[:8]}",
            },
        )
    return connector_id


async def _snapshot_rows(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> list[tuple[int, str, uuid.UUID | None]]:
    """``(snapshot_version, version_kind, account_id)`` for the pipeline, oldest first."""
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT snapshot_version, version_kind, account_id FROM pipeline_snapshots "
                    "WHERE pipeline_id = :pid ORDER BY snapshot_version"
                ),
                {"pid": str(pipeline_id)},
            )
        ).all()
    return [(int(r[0]), str(r[1]), r[2]) for r in rows]


async def _stored_graph(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> Any:
    """The COMMITTED ``graph_nodes_json`` — the row, not any in-memory object."""
    async with db_engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT graph_nodes_json FROM pipelines WHERE id = :id"),
                {"id": str(pipeline_id)},
            )
        ).scalar_one()
    return _as_json(value)


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID, connector_id: uuid.UUID) -> None:
    # ``pipeline_snapshots.pipeline_id`` is ON DELETE RESTRICT: the snapshot
    # rows must go first, or the pipeline delete below is refused.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        await conn.execute(
            text("DELETE FROM connector_instances WHERE id = :id"),
            {"id": str(connector_id)},
        )


def _real_session_factory(db_engine: AsyncEngine):
    """A ``ms._session`` stand-in that yields a REAL transactional session.

    Mirrors ``mcp_server._session``: RLS org context, then a transaction that
    COMMITS on a clean exit — so the bind's graph write and its edit snapshot
    commit together, exactly as in production.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

    @asynccontextmanager
    async def _session(org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            yield session

    return _session


def _bind_kwargs(pipeline_id: uuid.UUID, connector_id: uuid.UUID) -> dict[str, str]:
    return {
        "pipeline_id": str(pipeline_id),
        "node_id": str(_NODE_ID),
        "connector_type": "github",
        "connector_instance_id": str(connector_id),
    }


async def test_bind_records_edit_snapshot_then_suppresses_identical_rebind(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A real bind writes a real ``version_kind='edit'`` snapshot; an identical
    re-bind writes none — and the two payloads compared are distinct objects."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    connector_id = await _insert_connector(db_engine, test_org, test_user)
    kwargs = _bind_kwargs(pipeline_id, connector_id)

    # Spy on the no-op comparison itself: it records BOTH payloads and the
    # verdict it returned, so the test can prove (a) the pre-state is a DEEP
    # copy of the live graph taken before the mutation, and (b) suppression on
    # the re-bind comes from a genuine "unchanged" verdict rather than from the
    # pre-state being the mutated graph aliased under another name.
    observed: list[tuple[list[dict[str, Any]], list[dict[str, Any]], bool]] = []
    real_compare = _graph_content_unchanged

    def _spy(
        previous_nodes: list[dict[str, Any]],
        old_edges: list[dict[str, Any]],
        new_nodes: list[dict[str, Any]],
        new_edges: list[dict[str, Any]],
    ) -> bool:
        unchanged = real_compare(previous_nodes, old_edges, new_nodes, new_edges)
        observed.append((previous_nodes, new_nodes, unchanged))
        return unchanged

    tokens = (
        ms._ctx_org_id.set(test_org),
        ms._ctx_user_id.set(test_user),
        ms._ctx_role.set("admin"),
        ms._ctx_team_id.set(None),
    )
    try:
        with (
            patch.object(ms, "_session", _real_session_factory(db_engine)),
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_check_agent_tool_scope"),
            patch("modulo.db.crud.pipeline._graph_content_unchanged", new=_spy),
        ):
            first = await ms.bind_connector_to_node(**kwargs)
            second = await ms.bind_connector_to_node(**kwargs)
    finally:
        ms._ctx_org_id.reset(tokens[0])
        ms._ctx_user_id.reset(tokens[1])
        ms._ctx_role.reset(tokens[2])
        ms._ctx_team_id.reset(tokens[3])

    try:
        # --- the tool call itself succeeded on both attempts ---------------
        assert "error" not in first, first
        assert first["status"] == "bound", first
        assert "error" not in second, second
        assert second["status"] == "bound", second

        # --- (3) the compared payloads are DISTINCT objects ----------------
        assert len(observed) == 2, "each bind must run exactly one no-op comparison"
        prev_first, post_first, unchanged_first = observed[0]
        prev_second, post_second, unchanged_second = observed[1]
        assert len(prev_first) == 1, prev_first
        assert len(post_first) == 1, post_first
        assert prev_first[0] is not post_first[0], (
            "the pre-bind capture must be a deep copy — an alias of the mutated node makes "
            "the comparison self-referential and suppresses every snapshot"
        )
        # The captured pre-state really PREDATES the mutation: the first bind
        # starts from a node with no binding at all.
        assert "connector_binding" not in prev_first[0], prev_first[0]
        assert post_first[0]["connector_binding"]["instance_id"] == str(connector_id)
        # ...and the second capture sees the FIRST bind's result, i.e. it is a
        # fresh copy of the state that bind left behind.
        assert prev_second[0]["connector_binding"]["instance_id"] == str(connector_id)
        assert prev_second[0] is not post_second[0]

        # --- (0) the bind's own graph WRITE landed -------------------------
        # Pre-fix the helper mutated the ORM-loaded value in place and handed
        # SQLAlchemy an equal-content list, so history came back unchanged and
        # the flush issued NO UPDATE: the tool answered ``status: "bound"``
        # while the committed row never changed — which also made the snapshot
        # (minted from the in-memory object) disagree with the row.
        stored = await _stored_graph(db_engine, pipeline_id)
        assert len(stored) == 1, stored
        assert stored[0]["connector_binding"]["instance_id"] == str(connector_id), stored

        # --- (1) the first bind created a real snapshot --------------------
        assert unchanged_first is False, "a first bind changes the graph and must not be suppressed"
        rows = await _snapshot_rows(db_engine, pipeline_id)
        assert [(v, k) for v, k, _ in rows] == [(1, "edit")], rows
        assert rows[0][2] == test_user, "the snapshot must be attributed to the MCP caller"

        # --- (2) an identical re-bind IS suppressed ------------------------
        assert unchanged_second is True, "an identical re-bind must compare unchanged"
        rows_after = await _snapshot_rows(db_engine, pipeline_id)
        assert rows_after == rows, f"the identical re-bind must not push a snapshot: {rows_after}"
    finally:
        await _cleanup(db_engine, pipeline_id, connector_id)


async def test_bind_snapshot_failure_is_surfaced_not_swallowed(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The fail-closed guard on the bind surface is reachable: a snapshot that
    cannot be allocated turns into a loud tool error, never a silent success."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    connector_id = await _insert_connector(db_engine, test_org, test_user)
    kwargs = _bind_kwargs(pipeline_id, connector_id)

    tokens = (
        ms._ctx_org_id.set(test_org),
        ms._ctx_user_id.set(test_user),
        ms._ctx_role.set("admin"),
        ms._ctx_team_id.set(None),
    )
    try:
        with (
            patch.object(ms, "_session", _real_session_factory(db_engine)),
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_check_agent_tool_scope"),
            # ``None`` is exactly what the fail-closed branch refuses to commit.
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_edit", new=AsyncMock(return_value=None)),
        ):
            result = await ms.bind_connector_to_node(**kwargs)
    finally:
        ms._ctx_org_id.reset(tokens[0])
        ms._ctx_user_id.reset(tokens[1])
        ms._ctx_role.reset(tokens[2])
        ms._ctx_team_id.reset(tokens[3])

    try:
        # The RuntimeError is caught by the tool's generic envelope — what
        # matters is that the bind FAILED loudly instead of returning "bound".
        assert "error" in result, result
        assert result.get("status") != "bound"
        # The bind rolled back with the failed snapshot: no snapshot row...
        assert not await _snapshot_rows(db_engine, pipeline_id), "a failed snapshot must leave no row"
        # ...and the graph was never mutated either.
        assert await _stored_graph(db_engine, pipeline_id) == [_node()]
    finally:
        await _cleanup(db_engine, pipeline_id, connector_id)


async def test_bind_snapshot_actor_ladder_records_honestly_unattributed_write(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1471 ladder: an MCP session with no user context resolves through
    ``_ctx_user_id_val()`` — the snapshot is written with a NULL actor (honest)
    and a warning is logged — instead of a bare ``.get(None)`` that recorded
    nothing about the unattributed write."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    connector_id = await _insert_connector(db_engine, test_org, test_user)
    kwargs = _bind_kwargs(pipeline_id, connector_id)

    tokens = (
        ms._ctx_org_id.set(test_org),
        ms._ctx_user_id.set(None),
        ms._ctx_role.set("admin"),
        ms._ctx_team_id.set(None),
    )
    try:
        with (
            patch.object(ms, "_session", _real_session_factory(db_engine)),
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_check_agent_tool_scope"),
            caplog.at_level("WARNING", logger="modulo.api.mcp_server"),
        ):
            result = await ms.bind_connector_to_node(**kwargs)
    finally:
        ms._ctx_org_id.reset(tokens[0])
        ms._ctx_user_id.reset(tokens[1])
        ms._ctx_role.reset(tokens[2])
        ms._ctx_team_id.reset(tokens[3])

    try:
        assert "error" not in result, result
        rows = await _snapshot_rows(db_engine, pipeline_id)
        assert [(v, k) for v, k, _ in rows] == [(1, "edit")], rows
        assert rows[0][2] is None, "an unresolvable actor must be recorded as NULL, never a bogus id"
        assert any("unattributed_write" in r.getMessage() for r in caplog.records), (
            f"the ladder must LOG the unattributed write; records={[r.getMessage() for r in caplog.records]}"
        )
    finally:
        await _cleanup(db_engine, pipeline_id, connector_id)


async def _snapshot_graph(db_engine: AsyncEngine, pipeline_id: uuid.UUID, version: int) -> Any:
    """The frozen ``graph_json`` of one snapshot row."""
    async with db_engine.connect() as conn:
        value = (
            await conn.execute(
                text(
                    "SELECT graph_json FROM pipeline_snapshots WHERE pipeline_id = :pid AND snapshot_version = :version"
                ),
                {"pid": str(pipeline_id), "version": version},
            )
        ).scalar_one()
    return _as_json(value)


def _connector_query_node(connector_id: uuid.UUID) -> dict[str, Any]:
    """A ``connector`` node carrying a real GitHub query (FAR-2089)."""
    return {
        "id": str(_NODE_ID),
        "node_type": "connector",
        "position": {"x": 0, "y": 0},
        "label": "github-pulls",
        "connector_binding": {
            "type": "github",
            "instance_id": str(connector_id),
            "operation": "query",
            "resource": "pull",
            "filters": {"state": "open", "labels": ["agent", "backend"], "limit": 25},
            "data": {"note": "round-trip"},
        },
    }


async def test_public_api_writes_a_connector_node_and_the_snapshot_carries_its_query(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-2089 end-to-end: write -> snapshot through the REAL public API.

    ``update_pipeline_graph`` is the MCP surface's public graph write. Before
    the ``node_type`` Literal accepted ``connector`` the call returned
    ``validation_failed`` and nothing was written, so a GitHub query node could
    not be authored through the API at all. This drives the real tool against
    real Postgres and then reads BOTH committed artefacts back - the pipeline
    row a later edit sees, and the immutable snapshot a run executes - because
    the binding's ``resource`` / ``filters`` / ``data`` must survive both.
    """
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    connector_id = await _insert_connector(db_engine, test_org, test_user)
    node = _connector_query_node(connector_id)

    tokens = (
        ms._ctx_org_id.set(test_org),
        ms._ctx_user_id.set(test_user),
        ms._ctx_role.set("admin"),
        ms._ctx_team_id.set(None),
    )
    try:
        with (
            patch.object(ms, "_session", _real_session_factory(db_engine)),
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_check_agent_tool_scope"),
        ):
            result = await ms.update_pipeline_graph(
                pipeline_id=str(pipeline_id),
                nodes=[node],
                edges=[],
            )
    finally:
        ms._ctx_org_id.reset(tokens[0])
        ms._ctx_user_id.reset(tokens[1])
        ms._ctx_role.reset(tokens[2])
        ms._ctx_team_id.reset(tokens[3])

    try:
        assert "error" not in result, result

        stored = await _stored_graph(db_engine, pipeline_id)
        assert len(stored) == 1, stored
        assert stored[0]["node_type"] == "connector", stored
        stored_binding = stored[0]["connector_binding"]
        assert stored_binding["resource"] == "pull", stored_binding
        assert stored_binding["filters"] == {"state": "open", "labels": ["agent", "backend"], "limit": 25}
        assert stored_binding["data"] == {"note": "round-trip"}

        rows = await _snapshot_rows(db_engine, pipeline_id)
        assert [(v, k) for v, k, _ in rows] == [(1, "edit")], rows
        snapshot_graph = await _snapshot_graph(db_engine, pipeline_id, 1)
        snapshot_binding = snapshot_graph["nodes"][0]["connector_binding"]
        assert snapshot_graph["nodes"][0]["node_type"] == "connector", snapshot_graph
        assert snapshot_binding["resource"] == "pull", snapshot_binding
        assert snapshot_binding["filters"] == {"state": "open", "labels": ["agent", "backend"], "limit": 25}
        assert snapshot_binding["data"] == {"note": "round-trip"}
    finally:
        await _cleanup(db_engine, pipeline_id, connector_id)
