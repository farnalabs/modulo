"""FAR-1471: every pipeline graph mutation emits ``pipeline.graph_updated``.

``replace_pipeline_graph`` previously appended an audit event ONLY for
HITL-gate removals (``hitl_review_removed``), so a plain graph write —
including changing a node's ``agent_commands`` — recorded no actor and no
timestamp (the 2026-10-03 PR Reviewer outage could not be attributed).

These tests run the REAL CRUD function against a real (in-memory SQLite)
session and read the resulting ``audit_events`` row back. They deliberately
do NOT mock ``append_audit_event`` — an event-shaped mock call is not
evidence that an event is written — so they fail when the emit is removed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline import replace_pipeline_graph
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge

# Scoped create_all: unrelated models use Postgres-only column types that
# SQLite cannot render (same rationale as tests/unit/db/test_seed_demo.py).
_TABLE_NAMES = {
    "accounts",
    "org_memberships",
    "pipelines",
    "pipeline_edges",
    "audit_events",
    "audit_chain_heads",
}

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")

_NODE_A = "00000000-0000-0000-0000-0000000000a1"
_NODE_B = "00000000-0000-0000-0000-0000000000a2"
_NODE_C = "00000000-0000-0000-0000-0000000000a3"

_EDGE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e1")

_SECRET_VALUE = "sk-live-DO-NOT-LEAK-12345"


def _node(node_id: str, *, agent_commands: list[str], **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "node_type": "agent",
        "position": {"x": 0, "y": 0},
        "label": node_id[-2:],
        "agent_commands": list(agent_commands),
    }
    node.update(extra)
    return node


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
    """Seed the actor, an admin membership, the pipeline and (optionally) one edge."""
    session.add(Account(id=_ACCOUNT_ID, email="actor@example.com", display_name="Graph Actor"))
    session.add(OrgMembership(account_id=_ACCOUNT_ID, organisation_id=_ORG_ID, role="admin"))
    session.add(
        Pipeline(
            id=_PIPELINE_ID,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            name="Graph Audit Pipeline",
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


async def _graph_events(session: AsyncSession) -> list[AuditEvent]:
    result = await session.execute(select(AuditEvent).where(AuditEvent.event_type == "pipeline.graph_updated"))
    return list(result.scalars())


async def _patch_graph(
    session: AsyncSession,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    caller_type: Literal["rest", "mcp"] = "mcp",
    account_id: uuid.UUID | None = _ACCOUNT_ID,
) -> Any:
    """Drive the REAL ``replace_pipeline_graph`` CRUD function (shared by the
    REST ``PATCH /{id}/graph`` route and the MCP ``update_pipeline_graph`` tool).

    The default mirrors the MCP call: it carries a real actor id but skips the
    live-role re-read, which ``resolve_role_from_membership`` performs with
    *stringified* UUIDs — a bind SQLite's non-native ``Uuid`` column type
    rejects (``'str' object has no attribute 'hex'``). That pre-existing
    SQLite-only limitation is unrelated to the audit event, so the REST shape
    (live-role path exercised via ``caller_type="rest"`` with no account id)
    is covered separately in ``test_rest_caller_shape_emits_the_event``.
    """
    return await replace_pipeline_graph(
        session,
        pipeline_id=_PIPELINE_ID,
        org_id=_ORG_ID,
        nodes=nodes,
        edges=edges,
        is_privileged=True,
        caller_type=caller_type,
        account_id=account_id,
        is_guardrail_admin=True,
    )


class TestGraphUpdatedAuditEvent:
    async def test_graph_patch_writes_graph_updated_event(self, session: AsyncSession) -> None:
        """A real graph write through the CRUD path appends the audit event."""
        await _seed_graph(
            session,
            [_node(_NODE_A, agent_commands=["run --v1"]), _node(_NODE_B, agent_commands=["keep"])],
            with_edge=True,
        )
        new_nodes = [
            _node(_NODE_A, agent_commands=["run --v2"]),
            _node(_NODE_B, agent_commands=["keep"]),
            _node(_NODE_C, agent_commands=["brand new"]),
        ]
        new_edges = [
            {
                "id": str(_EDGE_ID),
                "source_node_id": _NODE_A,
                "target_node_id": _NODE_B,
                "edge_type": "normal",
            }
        ]

        result = await _patch_graph(session, new_nodes, new_edges)
        assert result is not None, "the graph write must succeed for an audit event to exist"
        await session.flush()

        events = await _graph_events(session)
        assert len(events) == 1, f"expected exactly one pipeline.graph_updated event, got {len(events)}"

        event = events[0]
        assert event.resource_type == "pipeline"
        assert event.resource_id == _PIPELINE_ID
        assert event.account_id == _ACCOUNT_ID, "the actor must be attributable"

        payload = event.payload_json
        assert payload["pipeline_id"] == str(_PIPELINE_ID)
        assert payload["changed_by"] == str(_ACCOUNT_ID)
        assert payload["caller_type"] == "mcp"
        assert payload["previous_node_count"] == 2
        assert payload["new_node_count"] == 3
        assert payload["previous_edge_count"] == 1
        assert payload["new_edge_count"] == 1
        assert payload["added_node_ids"] == [_NODE_C]
        assert not payload["removed_node_ids"]
        assert payload["agent_commands_changed_node_ids"] == [_NODE_A]

    async def test_agent_commands_change_is_reported_even_without_other_changes(self, session: AsyncSession) -> None:
        """The outage case: a write that only changes agent_commands is attributed."""
        await _seed_graph(session, [_node(_NODE_A, agent_commands=["run --v1"])], with_edge=False)
        await _patch_graph(session, [_node(_NODE_A, agent_commands=["run --v2"])], [])

        events = await _graph_events(session)
        assert len(events) == 1
        payload = events[0].payload_json
        assert payload["agent_commands_changed_node_ids"] == [_NODE_A]
        assert not payload["added_node_ids"]
        assert not payload["removed_node_ids"]
        assert payload["previous_node_count"] == 1
        assert payload["new_node_count"] == 1

    async def test_node_removal_is_reported(self, session: AsyncSession) -> None:
        await _seed_graph(
            session,
            [_node(_NODE_A, agent_commands=["a"]), _node(_NODE_B, agent_commands=["b"])],
            with_edge=False,
        )
        await _patch_graph(session, [_node(_NODE_A, agent_commands=["a"])], [])

        events = await _graph_events(session)
        assert len(events) == 1
        payload = events[0].payload_json
        assert payload["removed_node_ids"] == [_NODE_B]
        assert not payload["added_node_ids"]
        assert not payload["agent_commands_changed_node_ids"]

    async def test_payload_carries_no_node_bodies_or_secrets(self, session: AsyncSession) -> None:
        """IDs and counts only: no env_vars / masked values may enter the audit chain."""
        await _seed_graph(
            session,
            [
                _node(
                    _NODE_A,
                    agent_commands=["run"],
                    env_vars={"API_TOKEN": _SECRET_VALUE},
                    context_files=[{"path": ".env", "content": _SECRET_VALUE}],
                )
            ],
            with_edge=False,
        )
        await _patch_graph(
            session,
            [
                _node(
                    _NODE_A,
                    agent_commands=["run --changed"],
                    env_vars={"API_TOKEN": _SECRET_VALUE},
                    context_files=[{"path": ".env", "content": _SECRET_VALUE}],
                )
            ],
            [],
        )

        events = await _graph_events(session)
        assert len(events) == 1
        serialised = json.dumps(events[0].payload_json)
        assert _SECRET_VALUE not in serialised, "the audit payload must never carry secret material"
        assert "env_vars" not in events[0].payload_json
        assert "context_files" not in events[0].payload_json

    async def test_unchanged_graph_write_still_emits_the_event(self, session: AsyncSession) -> None:
        """Attribution: even a byte-identical write records who wrote and when."""
        nodes = [_node(_NODE_A, agent_commands=["run"])]
        await _seed_graph(session, nodes, with_edge=False)
        await _patch_graph(session, [dict(n) for n in nodes], [])

        events = await _graph_events(session)
        assert len(events) == 1
        payload = events[0].payload_json
        assert not payload["agent_commands_changed_node_ids"]
        assert not payload["added_node_ids"]
        assert not payload["removed_node_ids"]

    async def test_rest_caller_shape_emits_the_event(self, session: AsyncSession) -> None:
        """The REST ``PATCH /{id}/graph`` shape (no live-role re-read) emits too."""
        await _seed_graph(session, [_node(_NODE_A, agent_commands=["run --v1"])], with_edge=False)
        result = await _patch_graph(
            session,
            [_node(_NODE_A, agent_commands=["run --v2"])],
            [],
            caller_type="rest",
            account_id=None,
        )
        assert result is not None
        await session.flush()

        events = await _graph_events(session)
        assert len(events) == 1
        payload = events[0].payload_json
        assert payload["caller_type"] == "rest"
        assert payload["changed_by"] is None, "no principal was available on this call path"
        assert payload["agent_commands_changed_node_ids"] == [_NODE_A]

    async def test_unknown_pipeline_writes_no_event(self, session: AsyncSession) -> None:
        """No graph was mutated, so no event may be fabricated."""
        await _seed_graph(session, [], with_edge=False)
        result = await replace_pipeline_graph(
            session,
            pipeline_id=uuid.uuid4(),
            org_id=_ORG_ID,
            nodes=[],
            edges=[],
            is_privileged=True,
            caller_type="rest",
            account_id=None,
        )
        assert result is None
        assert not await _graph_events(session), "no graph mutation must produce no event"
